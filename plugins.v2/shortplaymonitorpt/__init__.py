import datetime
import os
import re
import threading
from pathlib import Path
from threading import Lock
from typing import Any, List, Dict, Tuple, Optional
from xml.dom import minidom

import chardet
import pytz
from PIL import Image
from apscheduler.schedulers.background import BackgroundScheduler
from lxml import etree
from requests import RequestException
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from app.helper.sites import SitesHelper
from app.modules.indexer.spider import SiteSpider

from app.core.config import settings
from app.core.meta.words import WordsMatcher
from app.core.metainfo import MetaInfoPath
from app.db.site_oper import SiteOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo, TransferInfo, TransferDirectoryConf
from app.schemas.types import NotificationType
from app.utils.common import retry
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils

ffmpeg_lock = threading.Lock()
lock = Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, watching_path: str, file_change: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = watching_path
        self.file_change = file_change

    def on_created(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.dest_path)


class ShortPlayMonitorPt(_PluginBase):
    # 插件名称
    plugin_name = "短剧刮削(PT)"
    # 插件描述
    plugin_desc = "纯粹通过pt站刮削的短剧插件"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "1.1.2"
    # 插件作者
    plugin_author = "AceCandy"
    # 作者主页
    author_url = "https://github.com/AceCandy"
    # 插件配置项ID前缀
    plugin_config_prefix = "shortplaymonitorpt_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _monitor_confs = None
    _onlyonce = False
    _exclude_keywords = ""
    _transfer_type = "link"
    _observer = []
    _timeline = "00:00:10"
    _dirconf = {}
    _interval = 10
    _notify = False
    _medias = {}
    # 站点缓存
    _site_image_cache = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._interval = config.get("interval")
            self._notify = config.get("notify")
            self._monitor_confs = config.get("monitor_confs")
            self._exclude_keywords = config.get("exclude_keywords", "")
            self._transfer_type = config.get("transfer_type", "link")

        # 停止现有任务
        self.stop_service()

        if not (self._enabled or self._onlyonce):
            return

        # 定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if self._notify:
            # 追加入库消息统一发送服务
            self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

        # 读取目录配置
        for monitor_conf in (self._monitor_confs or "").split("\n"):
            # 格式 监控方式#监控目录#目的目录
            if not monitor_conf:
                continue
            conf_parts = str(monitor_conf).split("#")
            if len(conf_parts) != 3:
                logger.error(f"{monitor_conf} 格式错误")
                continue
            mode, source_dir, target_dir = conf_parts
            # 存储目录监控配置
            self._dirconf[source_dir] = target_dir

            # 启用目录监控
            if self._enabled:
                if self._is_sub_dir(target_dir, source_dir):
                    continue
                observer = self._create_observer(mode, source_dir)
                self._observer.append(observer)
                observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
                self._start_observer(observer, source_dir)

        # 运行一次定时服务
        if self._onlyonce:
            logger.info("短剧监控服务启动，立即运行一次")
            self._scheduler.add_job(func=self.sync_all, trigger='date',
                                    run_date=datetime.datetime.now(
                                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                    name="短剧监控全量执行")
            # 关闭一次性开关
            self._onlyonce = False
            # 保存配置
            self.__update_config()

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def _is_sub_dir(self, target_dir, source_dir):
        try:
            if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                return True
        except Exception as e:
            logger.debug(str(e))
        return False

    def _create_observer(self, mode, source_dir):
        if mode == "compatibility":
            return PollingObserver(timeout=10)
        return Observer(timeout=10)

    def _start_observer(self, observer, source_dir):
        try:
            observer.daemon = True
            observer.start()
            logger.info(f"{source_dir} 的目录监控服务启动")
        except Exception as e:
            err_msg = str(e)
            if "inotify" in err_msg and "reached" in err_msg:
                logger.warn(
                    f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                    + """
                         echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                         echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                         sudo sysctl -p
                         """
                )
            else:
                logger.error(f"{source_dir} 启动目录监控失败：{err_msg}")
            self.systemmessage.put(f"{source_dir} 启动目录监控失败：{err_msg}")

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步短剧监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf:
            for file_path in SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT):
                self.__handle_file(
                    is_directory=Path(file_path).is_dir(),
                    event_path=str(file_path),
                    source_dir=mon_path
                )
        logger.info("全量同步短剧监控目录完成！")

    def __handle_image(self):
        """
        立即运行一次，裁剪封面
        """
        if not self._dirconf:
            logger.error("未正确配置，停止裁剪 ...")
            return

        logger.info("开始全量裁剪封面 ...")
        for mon_path in self._dirconf:
            target_path = self._dirconf[mon_path]
            for file_path in SystemUtils.list_files(Path(target_path), ["poster.jpg"]):
                if Path(file_path).name != "poster.jpg":
                    continue
                try:
                    image = Image.open(file_path)
                    if image.width / image.height != 3 / 2:
                        self.__save_poster(
                            input_path=file_path,
                            poster_path=file_path
                        )
                        logger.info(f"封面 {file_path} 已裁剪 比例为 3:2")
                except Exception:
                    continue
        logger.info("全量裁剪封面完成！")

    def event_handler(self, event, source_dir: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param source_dir: 监控目录
        :param event_path: 事件文件路径
        """
        if self._is_skip_file(event_path):
            return

        if Path(event_path).suffix not in settings.RMT_MEDIAEXT:
            logger.debug(f"{event_path} 不是媒体文件")
            return

        logger.debug(f"变动类型 {event.event_type} 变动路径 {event_path}")
        self.__handle_file(
            is_directory=event.is_directory,
            event_path=event_path,
            source_dir=source_dir
        )

    def _is_skip_file(self, event_path):
        skip_keywords = ["/@Recycle", "/#recycle", "/.", "/@eaDir"]
        if any(kw in event_path for kw in skip_keywords):
            logger.info(f"{event_path} 是回收站或隐藏的文件，跳过处理")
            return True

        if self._exclude_keywords:
            for keyword in self._exclude_keywords.split("\n"):
                if keyword and re.findall(keyword, event_path):
                    logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                    return True
        return False

    def __handle_file(self, is_directory: bool, event_path: str, source_dir: str):
        try:
            dest_dir = self._dirconf[source_dir]
            file_meta = MetaInfoPath(Path(event_path))
            if not file_meta.name:
                logger.error(f"{Path(event_path).name} 无法识别有效信息")
                return

            target_path = event_path.replace(source_dir, dest_dir)
            title, target_path = self._rename_path(target_path, dest_dir)
            self._process_path(is_directory, target_path, event_path, title)

            if self._notify and title:
                self._update_media_list(title, event_path)
        except Exception as e:
            logger.error(f"event_handler_created error: {e}")

    def _rename_path(self, target_path, dest_dir):
        """
        对目标路径进行重命名，提取路径中父目录名的首个部分作为新的标题，然后组合成新的路径。

        :param target_path: 目标路径
        :param dest_dir: 目标目录
        :return: 重命名后的路径
        """
        relative_path = Path(target_path).relative_to(dest_dir)
        parent_dir = relative_path.parent
        title = parent_dir.name.split(".")[0]
        new_relative_path = Path(title) / relative_path.name
        return title, Path(dest_dir) / new_relative_path

    def _create_directory_if_not_exists(self, dir_path: Path):
        """
        如果目录不存在，则创建目录
        """
        if not dir_path.exists():
            try:
                logger.info(f"创建目标文件夹 {dir_path}")
                dir_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"创建目录 {dir_path} 失败: {str(e)}")
                return False
        return True

    def _process_path(self, is_directory, target_path, event_path, title):
        target_path = Path(target_path)
        event_path = Path(event_path)
        if is_directory:
            self._create_directory_if_not_exists(target_path)
        else:
            target_path = self._rename_media_file(target_path)
            if not self._create_directory_if_not_exists(target_path.parent):
                return

            if Path(target_path).exists():
                logger.debug(f"目标文件 {target_path} 已存在")
                return

            retcode = self.__transfer_command(
                file_item=event_path,
                target_file=target_path,
                transfer_type=self._transfer_type
            )
            if retcode != 0:
                return

            transfer_type = self._transfer_type
            if transfer_type == 'link':
                transfer_type = '硬链接'
            elif transfer_type == 'softlink':
                transfer_type = '软链接'
            elif transfer_type == 'move':
                transfer_type = '移动'
            else:
                transfer_type = '复制'
            logger.info(f"文件 {event_path} {transfer_type} 完成")
            self._generate_nfo_and_thumb(target_path, title)

    def _rename_media_file(self, target_path):
        pattern = r'S\d+E\d+'
        match = re.search(pattern, Path(target_path).name)
        if match:
            return Path(target_path).parent / f"{match.group()}{Path(target_path).suffix}"
        logger.info("未找到匹配的季数和集数")
        return target_path

    def _generate_nfo_and_thumb(self, target_path, title):
        parent_dir = target_path.parent
        nfo_path = parent_dir / "tvshow.nfo"
        poster_path = parent_dir / "poster.jpg"

        # 获取页面源代码
        site_info = self._get_site_info(title=title)
        if not site_info:
            logger.error(f"未找到 {title} 的刮削信息")
            return
        # 解析站点信息
        poster_url = site_info["poster_url"]
        new_title = site_info["new_title"]
        year = site_info["year"]
        description = site_info["description"]

        if new_title:
            title = new_title

        # 生成 NFO 文件
        if not nfo_path.exists():
            try:
                self.__gen_tv_nfo_file(dir_path=parent_dir, title=title, originaltitle=new_title, year=year, description=description)
                logger.info(f"{nfo_path} NFO 文件已生成")
            except Exception as e:
                logger.error(f"生成 {nfo_path} NFO 文件失败: {str(e)}")

        # 生成海报
        if poster_path.exists():
            return

        if poster_url:
            try:
                if self.__save_image(url=poster_url, file_path=poster_path):
                    logger.info(f"{poster_path} 海报已使用站点图片生成")
            except Exception as e:
                logger.error(f"保存 {poster_path} 海报失败: {str(e)}")
        else:
            # 尝试生成缩略图
            thumb_path = self.gen_file_thumb(
                title=title,
                file_path=target_path
            )
            if thumb_path and thumb_path.exists():
                try:
                    self.__save_poster(
                        input_path=thumb_path,
                        poster_path=poster_path
                    )
                    if poster_path.exists():
                        logger.info(f"{poster_path} 缩略图已生成")
                except Exception as e:
                    logger.error(f"保存 {poster_path} 海报失败: {str(e)}")
                finally:
                    try:
                        thumb_path.unlink()
                    except Exception as e:
                        logger.error(f"删除临时缩略图 {thumb_path} 失败: {str(e)}")
            else:
                # 查找已有的缩略图文件
                thumb_files = SystemUtils.list_files(
                    directory=parent_dir,
                    extensions=[".jpg"]
                )
                if thumb_files:
                    try:
                        self.__save_poster(
                            input_path=thumb_files[0],
                            poster_path=poster_path
                        )
                        if poster_path.exists():
                            logger.info(f"{poster_path} 海报已使用现有缩略图生成")
                    except Exception as e:
                        logger.error(f"使用现有缩略图生成 {poster_path} 海报失败: {str(e)}")
                    finally:
                        for thumb in thumb_files:
                            try:
                                Path(thumb).unlink()
                            except Exception as e:
                                logger.error(f"删除现有缩略图 {thumb} 失败: {str(e)}")

    def _update_media_list(self, title, event_path):
        media_key = title
        media_list = self._medias.get(media_key, {"files": [], "time": datetime.datetime.now()})
        if event_path not in media_list["files"]:
            media_list["files"].append(event_path)
        self._medias[media_key] = media_list

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._notify or not self._medias:
            return

        for medis_title_year in list(self._medias):
            media_list = self._medias[medis_title_year]
            logger.info(f"开始处理媒体 {medis_title_year} 消息")

            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            if (datetime.datetime.now() - last_update_time).total_seconds() > self._interval:
                self.post_message(
                    mtype=NotificationType.Organize,
                    title=f"{medis_title_year} 共{len(media_files)}集已入库",
                    text="类别：短剧"
                )
                del self._medias[medis_title_year]

    @staticmethod
    def __transfer_command(file_item: Path, target_file: Path, transfer_type: str) -> int:
        """
        使用系统命令处理单个文件
        :param file_item: 文件路径
        :param target_file: 目标文件路径
        :param transfer_type: RmtMode转移方式
        """
        with lock:

            # 转移
            if transfer_type == 'link':
                # 硬链接
                retcode, retmsg = SystemUtils.link(file_item, target_file)
            elif transfer_type == 'softlink':
                # 软链接
                retcode, retmsg = SystemUtils.softlink(file_item, target_file)
            elif transfer_type == 'move':
                # 移动
                retcode, retmsg = SystemUtils.move(file_item, target_file)
            else:
                # 复制
                retcode, retmsg = SystemUtils.copy(file_item, target_file)

        if retcode != 0:
            logger.error(retmsg)

        return retcode

    def __save_poster(self, input_path, poster_path):
        """
        截取图片做封面
        """
        try:
            image = Image.open(input_path)
            width, height = image.size
            target_ratio = 2 / 3
            original_ratio = width / height

            # 计算裁剪尺寸
            if original_ratio > target_ratio:
                new_height = height
                new_width = int(new_height * target_ratio)
            else:
                new_width = width
                new_height = int(new_width / target_ratio)

            # 计算裁剪区域
            left = (width - new_width) // 2
            top = (height - new_height) // 2
            right = left + new_width
            bottom = top + new_height

            # 裁剪保存图片
            cropped_image = image.crop((left, top, right, bottom))
            cropped_image.save(poster_path)
            logger.info(f"封面截取成功，已保存至 {poster_path}")
        except Exception as e:
            logger.error(f"截取封面失败: {str(e)}")

    def __gen_tv_nfo_file(self, dir_path: Path, title: str, originaltitle: str, year: str = "", description: str = ""):
        """
        生成电视剧的NFO描述文件
        :param dir_path: 电视剧根目录
        """
        logger.info(f"正在生成电视剧NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        DomUtils.add_node(doc, root, "title", title)
        DomUtils.add_node(doc, root, "originaltitle", originaltitle)
        DomUtils.add_node(doc, root, "year", year)
        DomUtils.add_node(doc, root, "plot", description)
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")

        # 保存NFO
        file_path = dir_path.joinpath("tvshow.nfo")
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        file_path.write_bytes(xml_str)
        logger.info(f"NFO文件已保存：{file_path}")

    def _parse_site_info(self, html):
        """
        从页面源代码中解析封面图片链接、片名、年代和简介
        :param page_source: 页面源代码
        :return: 封面图片链接、片名、年代、简介
        """
        try:
            # 获取所有图片链接
            poster_url_list = html.xpath('//div[@id="kdescr"]/img[1]/@src')
            poster_url = poster_url_list[0] if poster_url_list else ''
            # 获取片名
            title = html.xpath('string(//div[@id="kdescr"]/text()[contains(., "◎片　　名")])')
            title = title.replace("◎片　　名　", "").strip()
            # 获取年代
            year = html.xpath('string(//div[@id="kdescr"]/text()[contains(., "◎年　　代")])')
            year = year.replace("◎年　　代　", "").strip()
            # 获取简介
            description_elements = html.xpath('//div[@id="kdescr"]/text()[contains(., "◎简　　介")]/following-sibling::text()')
            description = ''.join(description_elements).strip()
            return {
                "poster_url": poster_url,
                "new_title": title,
                "year": year,
                "description": description
            }
        except Exception as e:
            logger.error(f"解析站点信息失败: {str(e)}")
            return {
                "poster_url": "",
                "new_title": "",
                "year": "",
                "description": ""
            }


    def gen_file_thumb_from_site(self, title: str, file_path: Path):
        """
        从agsv或者萝莉站查询封面
        """
        try:
            site_info = self._get_site_info(title)
            if not site_info or not site_info["poster_url"]:
                logger.error(f"检索站点 {title} 封面失败")
                return None

            if self.__save_image(url=site_info["poster_url"], file_path=file_path):
                return file_path
            return None
        except Exception as e:
            logger.error(f"检索站点 {title} 封面失败 {str(e)}")
            return None

    def _get_site_info(self, title):
        if title in self._site_image_cache:
            return self._site_image_cache[title]
        sites = [
            ("agsvpt.com", 419),
#             ("ilolicon.com", 402)
        ]
        for domain, cat in sites:
            site = SiteOper().get_by_domain(domain)
            index = SitesHelper().get_indexer(domain)
            if site:
                req_url = f"https://www.{domain}/torrents.php?search_mode=0&search_area=0&page=0&notnewword=1&cat={cat}&search={title}"
                logger.info(f"开始检索 {site.name} {title}")
                page_source = self.__get_site_torrents(url=req_url, site=site, index=index, title=title)
                if page_source:
                    site_info = self._parse_site_info(page_source)
                    if site_info["poster_url"]:
                        self._site_image_cache[title] = site_info
                        return site_info
        self._site_image_cache[title] = None
        return None

    @retry(RequestException, logger=logger)
    def __save_image(self, url: str, file_path: Path):
        """
        下载图片并保存
        """
        try:
            logger.info(f"正在下载{file_path.stem}图片：{url} ...")
            r = RequestUtils(proxies=settings.PROXY).get_res(url=url, raise_exception=True)
            if r:
                file_path.write_bytes(r.content)
                logger.info(f"图片已保存：{file_path}")
                return True
            else:
                logger.info(f"{file_path.stem}图片下载失败，请检查网络连通性")
                return False
        except RequestException as err:
            raise err
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{str(err)}")
            return False

    def __get_site_torrents(self, url: str, site, index, title):
        """
        查询站点资源
        """
        page_source = self.__get_page_source(url=url, site=site)
        if not page_source:
            logger.error(f"请求站点 {site.name} 失败，URL: {url}")
            return None

        _spider = SiteSpider(indexer=index, page=1)
        torrents = _spider.parse(page_source)
        if not torrents:
            logger.error(f"未检索到站点 {site.name} 资源，URL: {url}")
            return None

        # 初始化匹配的索引为 -1
        matched_index = -1
        # 遍历 torrents 列表
        for i, torrent in enumerate(torrents):
            description = torrent.get("description", "")
            # 提取 description 中以 | 分隔的第一个字符串
            first_part = description.split("|")[0].strip()
            if first_part == title:
                matched_index = i
                break

        # 如果找到匹配项
        if matched_index == -1:
            logger.error(f"未找到精确匹配 【{title}】 的种子，站点: {site.name}，URL: {url}")
            return None

        torrent_url = torrents[matched_index].get("page_url")
        torrent_detail_source = self.__get_page_source(url=torrent_url, site=site)
        if not torrent_detail_source:
            logger.error(f"请求种子详情页失败，URL: {torrent_url}，站点: {site.name}")
            return None

        try:
            html = etree.HTML(torrent_detail_source)
            if not html:
                logger.error(f"种子详情页 {torrent_url} 无有效 HTML 内容，站点: {site.name}")
                return None
        except Exception as e:
            logger.error(f"解析种子详情页 {torrent_url} 时出错，错误信息: {str(e)}，站点: {site.name}")
            return None

        return html

    def __get_page_source(self, url: str, site):
        """
        获取页面资源
        """
        ret = RequestUtils(
            cookies=site.cookie,
            proxies=settings.PROXY,
            timeout=30,
        ).get_res(url, allow_redirects=True)
        if not ret:
            return ""

        raw_data = ret.content
        if not raw_data:
            return ret.text

        try:
            result = chardet.detect(raw_data)
            encoding = result['encoding']
            return raw_data.decode(encoding)
        except Exception:
            ret.encoding = "utf-8" if re.search(r"charset=\"?utf-8\"?", ret.text, re.IGNORECASE) else ret.apparent_encoding
            return ret.text

    def gen_file_thumb(self, title: str, file_path: Path):
        """
        处理一个文件
        """
        thumb_path = file_path.with_name(file_path.stem + "-site.jpg")
        if thumb_path.exists():
            logger.info(f"缩略图已存在：{thumb_path}")
            return thumb_path
        thumb_path = self.gen_file_thumb_from_site(title=title, file_path=thumb_path)
        if thumb_path and thumb_path.exists():
            logger.info(f"{file_path} 缩略图已生成：{thumb_path}")
            return thumb_path

        with ffmpeg_lock:
            thumb_path = file_path.with_name(file_path.stem + "-thumb.jpg")
            if thumb_path.exists():
                logger.info(f"缩略图已存在：{thumb_path}")
                return thumb_path
            if self.get_thumb(video_path=str(file_path), image_path=str(thumb_path), frames=self._timeline):
                logger.info(f"{file_path} 缩略图已生成：{thumb_path}")
                return thumb_path
            logger.error(f"FFmpeg处理文件 {file_path} 时发生错误")
            return None

    @staticmethod
    def get_thumb(video_path: str, image_path: str, frames: str = "00:00:10"):
        """
        使用ffmpeg从视频文件中截取缩略图
        """
        if not video_path or not image_path:
            return False
        cmd = f'ffmpeg -y -i "{video_path}" -ss {frames} -frames 1 "{image_path}"'
        return SystemUtils.execute(cmd)

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "exclude_keywords": self._exclude_keywords,
            "transfer_type": self._transfer_type,
            "onlyonce": self._onlyonce,
            "interval": self._interval,
            "notify": self._notify,
            "monitor_confs": self._monitor_confs
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '入库消息延迟',
                                            'placeholder': '10'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '监控方式#监控目录#目的目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '配置说明：'
                                                    '监控方式#监控目录#目的目录,一行一个配置。封面比例默认3:2'
                                                    'fast:性能模式，内部处理系统操作类型选择最优解;compatibility:兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB （建议使用）'
                                                    '从agsv或者萝莉站获取封面，不走其他刮削机制。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "image": False,
            "notify": False,
            "interval": 10,
            "monitor_confs": "",
            "exclude_keywords": "",
            "transfer_type": "link"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

        for observer in self._observer:
            try:
                observer.stop()
                observer.join()
            except Exception as e:
                logger.error(f"停止观察者失败：{str(e)}")
        self._observer.clear()