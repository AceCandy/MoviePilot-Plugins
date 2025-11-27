import datetime
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

class ShortPlayMonitorCompensate(_PluginBase):
    plugin_name = "短剧刮削(补偿)"
    plugin_desc = "原地补偿未刮削数据"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    plugin_version = "1.0.5"
    plugin_author = "AceCandy"
    author_url = "https://github.com/AceCandy"
    plugin_config_prefix = "shortplaymonitorcompensate_"
    plugin_order = 27
    auth_level = 2

    # 私有属性
    _enabled = False
    _monitor_confs = None
    _observer = []
    _timeline = "00:00:10"
    _medias = {}
    # 补偿目录
    _compensation_floder = []
    # 站点缓存
    _site_image_cache = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._compensation_floder = []
        if config:
            self._enabled = config.get("enabled")
            self._monitor_confs = config.get("monitor_confs")

        # 停止现有任务
        self.stop_service()

        if not self._enabled:
            return

        # 定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        # 读取目录配置
        for monitor_path in (self._monitor_confs or "").split("\n"):
            # 格式 补偿目录
            monitor_path = monitor_path.strip()
            if not monitor_path:
                continue
            # 存储目录监控配置
            path_obj = Path(monitor_path)
            # 检查路径是否为存在的目录
            if path_obj.is_dir():
                # 将有效的目录路径添加到补偿目录列表
                self._compensation_floder.append(path_obj)

        # 运行一次定时服务
        logger.info("短剧补偿服务启动")
        self._scheduler.add_job(func=self.sync_all, trigger='date',
                                run_date=datetime.datetime.now(
                                    tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                name="短剧补偿")
        # 关闭一次性开关
        self._enabled = False
        # 保存配置
        self.update_config({
            "enabled": self._enabled,
            "monitor_confs": self._monitor_confs
        })

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步短剧监控目录 ...")
        # 遍历所有监控目录
        for monitor_path in self._compensation_floder:
            for file_path in SystemUtils.list_sub_directory(monitor_path):
                if not SystemUtils.list_sub_directory(file_path):
                    self._generate_nfo_and_thumb(file_path)
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

    def _is_skip_file(self, event_path):
        skip_keywords = ["/@Recycle", "/#recycle", "/.", "/@eaDir"]
        if any(kw in event_path for kw in skip_keywords):
            logger.info(f"{event_path} 是回收站或隐藏的文件，跳过处理")
            return True

        return False

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

    def _rename_media_file(self, target_path):
        pattern = r'S\d+E\d+'
        match = re.search(pattern, Path(target_path).name)
        if match:
            return Path(target_path).parent / f"{match.group()}{Path(target_path).suffix}"
        logger.info("未找到匹配的季数和集数")
        return target_path

    def _generate_nfo_and_thumb(self, target_path):
        parent_dir = target_path
        nfo_path = parent_dir / "tvshow.nfo"
        poster_path = parent_dir / "poster.jpg"
        if nfo_path.exists() and poster_path.exists():
            return

        # 获取页面源代码
        title = target_path.name
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
                file_path=parent_dir
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
                    directory=target_path,
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
            ("pt.agsvpt.cn", 419),
#             ("ilolicon.com", 402)
        ]
        for domain, cat in sites:
            site = SiteOper().get_by_domain(domain)
            index = SitesHelper().get_indexer(domain)
            if site:
                req_url = f"https://{domain}/torrents.php?search_mode=0&search_area=0&page=0&notnewword=1&cat={cat}&search={title}"
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
                                            'label': '立即运行一次',
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '补偿目录',
                                            'rows': 5,
                                            'placeholder': '填写docker中绝对地址目录（实际短剧的父级目录），一行一个'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "image": False,
            "monitor_confs": ""
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
