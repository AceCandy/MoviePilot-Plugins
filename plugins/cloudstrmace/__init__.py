import os
import shutil
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from typing import Any, List, Dict, Tuple, Optional

from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings
from app.utils.system import SystemUtils


class CloudStrmAce(_PluginBase):
    # 插件名称
    plugin_name = "生成云盘Strm"
    # 插件描述
    plugin_desc = "扫描本地文件，转移到云盘并生成Strm文件"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "0.7"
    # 插件作者
    plugin_author = "AceCandy"
    # 作者主页
    author_url = "https://github.com/AceCandy"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudstrmace_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _cron = None
    _monitor_confs = None
    _onlyonce = False
    _ismove = False
    _copy_files = False
    _no_del_dirs = None
    _rmt_mediaext = ".mp4, .mkv, .ts, .iso, .rmvb, .avi, .mov, .mpeg, .mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .tp, .f4v"
    _rmt_nomediaext = ".nfo, .jpg, .jpeg, .png, .svg, .ass, .srt, .sup, .mp3, .flac, .wav, .aac"
    _observer = []

    # 公开属性
    _increment_dir = {}
    _dirconf = {}
    _libraryconf = {}
    _cloudtypeconf = {}
    _cloudurlconf = {}
    _cloudpathconf = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._libraryconf = {}
        self._cloudtypeconf = {}
        self._cloudurlconf = {}
        self._cloudpathconf = {}
        self._increment_dir = {}

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._ismove = config.get("ismove")
            self._copy_files = config.get("copy_files")
            self._monitor_confs = config.get("monitor_confs")
            self._no_del_dirs = config.get("no_del_dirs")
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso, .rmvb, .avi, .mov, .mpeg, .mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .tp, .f4v"
            self._rmt_nomediaext = config.get(
                "rmt_nomediaext") or ".nfo, .jpg, .jpeg, .png, .svg, .ass, .srt, .sup, .mp3, .flac, .wav, .aac"

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 读取目录配置
            monitor_confs = self._monitor_confs.split("\n")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 源目录:目的目录:媒体库内网盘路径:监控模式
                if not monitor_conf:
                    continue
                # 注释
                if str(monitor_conf).startswith("#"):
                    continue

                if str(monitor_conf).count("#") == 3:
                    increment_dir = str(monitor_conf).split("#")[0]
                    source_dir = str(monitor_conf).split("#")[1]
                    target_dir = str(monitor_conf).split("#")[2]
                    library_dir = str(monitor_conf).split("#")[3]
                    self._libraryconf[source_dir] = library_dir
                elif str(monitor_conf).count("#") == 5:
                    increment_dir = str(monitor_conf).split("#")[0]
                    source_dir = str(monitor_conf).split("#")[1]
                    target_dir = str(monitor_conf).split("#")[2]
                    cloud_type = str(monitor_conf).split("#")[3]
                    cloud_path = str(monitor_conf).split("#")[4]
                    cloud_url = str(monitor_conf).split("#")[5]
                    self._cloudtypeconf[source_dir] = cloud_type
                    self._cloudpathconf[source_dir] = cloud_path
                    self._cloudurlconf[source_dir] = cloud_url
                else:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue

                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir

                # 增量配置
                self._increment_dir[increment_dir] = source_dir

                # 检查媒体库目录是不是下载目录的子目录
                try:
                    if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                        logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                        self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                        continue
                except Exception as e:
                    logger.debug(str(e))
                    pass

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("云盘增量监控执行服务启动，立即运行一次")
                self._scheduler.add_job(func=self.scan, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="云盘增量监控")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 周期运行
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.scan,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="云盘增量监控")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @eventmanager.register(EventType.PluginAction)
    def scan(self, event: Event = None):
        """
        扫描
        """
        if not self._enabled:
            logger.error("插件未开启")
            return
        if not self._dirconf or not self._dirconf.keys():
            logger.error("未获取到可用目录监控配置，请检查")
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloud_strm_increment":
                return
            logger.info("收到命令，开始云盘strm生成 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始云盘strm生成 ...",
                              userid=event.event_data.get("user"))

        logger.info("云盘strm生成任务开始")
        for increment_dir in self._increment_dir.keys():
            logger.info(f"正在扫描增量目录 {increment_dir}")
            for root, dirs, files in os.walk(increment_dir):
                # 如果遇到名为'extrafanart'的文件夹，则跳过处理该文件夹，继续处理其他文件夹
                if "extrafanart" in dirs:
                    dirs.remove("extrafanart")

                # 处理文件
                for file in files:
                    increment_file = os.path.join(root, file)
                    if not Path(increment_file).exists():
                        continue
            
                    # 回收站及隐藏的文件不处理
                    if any(marker in increment_file for marker in ["/@Recycle", "/#recycle", "/.", "/@eaDir"]):
                        logger.info(f"{increment_file} 是回收站或隐藏的文件，跳过处理")
                        continue

                    # 非保留文件直接跳过
                    file_suffix = Path(file).suffix
                    if not self._is_valid_file(file_suffix):
                        continue

                    # 移动到目标目录
                    source_dir = self._increment_dir.get(increment_dir)
                    # 移动后文件
                    source_file = increment_file.replace(increment_dir, source_dir)

                    # 判断目标文件是否存在
                    target_dir = Path(source_file).parent
                    target_dir.mkdir(parents=True, exist_ok=True)

                    # 扫描云盘文件，判断是否有对应strm
                    self.__strm(source_file, increment_file)
                    logger.info(f"增量文件 {increment_file} 处理完成")

                    # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
                    self._clean_empty_parent_dirs(increment_file)

        logger.info("云盘strm生成任务完成")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘strm生成任务完成！",
                              userid=event.event_data.get("user"))
            
    def _is_valid_file(self, file_suffix):
        nomedia_exts = [ext.strip() for ext in self._rmt_nomediaext.split(",")]
        media_exts = [ext.strip() for ext in self._rmt_mediaext.split(",")]
    
        if file_suffix not in nomedia_exts and file_suffix not in media_exts:
            return False
        if not self._copy_files and file_suffix not in media_exts:
            return False
        return True

    def _clean_empty_parent_dirs(self, increment_file):
        parent_paths = list(Path(increment_file).parents)
        for parent_path in parent_paths:
            if parent_path.name in self._no_del_dirs:
                break
            if parent_path == Path(increment_file).parent:
                break
            if parent_path.parent != Path(increment_file).root:
                if not any(parent_path.iterdir()):
                    shutil.rmtree(parent_path)
                    logger.warning(f"增量非保留目录 {parent_path} 已删除")

    def __strm(self, source_file, increment_file):
        """
        判断文件是否有对应strm
        """
        try:
            # 获取文件的转移路径
            for source_dir in self._dirconf.keys():
                if str(source_file).startswith(source_dir):
                    # 转移路径
                    dest_dir = self._dirconf.get(source_dir)
                    # 媒体库容器内挂载路径
                    library_dir = self._libraryconf.get(source_dir)
                    # 云服务类型
                    cloud_type = self._cloudtypeconf.get(source_dir)
                    # 云服务挂载本地跟路径
                    cloud_path = self._cloudpathconf.get(source_dir)
                    # 云服务地址
                    cloud_url = self._cloudurlconf.get(source_dir)

                    # 转移后文件
                    dest_file = source_file.replace(source_dir, dest_dir)
                    # 如果是文件夹
                    if Path(dest_file).is_dir():
                        if not Path(dest_file).exists():
                            logger.info(f"创建目标文件夹 {dest_file}")
                            os.makedirs(dest_file)
                            continue
                    else:
                        # 非媒体文件
                        if Path(dest_file).exists():
                            logger.info(f"目标文件 {dest_file} 已存在")
                            continue

                        # 文件
                        if not Path(dest_file).parent.exists():
                            logger.info(f"创建目标文件夹 {Path(dest_file).parent}")
                            os.makedirs(Path(dest_file).parent)

                        # 视频文件创建.strm文件
                        if Path(dest_file).suffix in [ext.strip() for ext in self._rmt_mediaext.split(",")]:
                            # 创建.strm文件
                            self.__create_strm_file(dest_file=dest_file,
                                                    dest_dir=dest_dir,
                                                    source_file=source_file,
                                                    library_dir=library_dir,
                                                    cloud_type=cloud_type,
                                                    cloud_path=cloud_path,
                                                    cloud_url=cloud_url,
                                                    increment_file=increment_file)
                        else:
                            if self._copy_files and Path(dest_file).suffix in [ext.strip() for ext in self._rmt_nomediaext.split(",")]:
                                # 其他nfo、jpg等复制文件
                                shutil.copy2(dest_file, source_file)
                                logger.info(f"复制增量文件 {dest_file} 到 {source_file}")
        except Exception as e:
            logger.error(f"create strm file error: {e}")
            print(str(e))

    @staticmethod
    def __create_strm_file(dest_file: str, dest_dir: str, source_file: str, library_dir: str = None,
                           cloud_type: str = None, cloud_path: str = None, cloud_url: str = None,
                           increment_file:str = None):
        """
        生成strm文件
        :param library_dir:
        :param dest_dir:
        :param dest_file:
        """
        try:
            # 获取视频文件名和目录
            video_name = Path(dest_file).name
            # 获取视频目录
            dest_path = Path(dest_file).parent

            if not dest_path.exists():
                logger.info(f"创建目标文件夹 {dest_path}")
                os.makedirs(str(dest_path))

            # 构造.strm文件路径
            strm_path = os.path.join(dest_path, f"{os.path.splitext(video_name)[0]}.strm")
            # strm已存在跳过处理
            if Path(strm_path).exists():
                logger.info(f"strm文件已存在 {strm_path}")
                return

            logger.info(f"替换前本地路径:::{dest_file}")

            # 云盘模式
            if cloud_type:
                # 替换路径中的\为/
                dest_file = source_file.replace("\\", "/")
                #dest_file = dest_file.replace(cloud_path, "")
                dest_file = os.path.relpath(dest_file, cloud_path)
                # 对盘符之后的所有内容进行url转码
                dest_file = urllib.parse.quote(dest_file, safe='')
                if str(cloud_type) == "alist":
                    dest_file = f"{cloud_url}/{dest_file}"
                    logger.info(f"strm文件已生成，写入路径:::{dest_file}")
                else:
                    logger.error(f"云盘类型仅支持alist")
                    return
            else:
                # 本地挂载路径转为emby路径
                dest_file = dest_file.replace(dest_dir, library_dir)
                logger.info(f"替换后emby容器内路径:::{dest_file}")

            # 写入.strm文件
            with open(strm_path, 'w') as f:
                f.write(dest_file)

            logger.info(f"创建strm文件 {strm_path}")
        except Exception as e:
            logger.error(f"创建strm文件失败")
            print(str(e))

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "ismove": self._ismove,
            "copy_files": self._copy_files,
            "cron": self._cron,
            "monitor_confs": self._monitor_confs,
            "no_del_dirs": self._no_del_dirs,
            "rmt_mediaext": self._rmt_mediaext,
            "rmt_nomediaext": self._rmt_nomediaext
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cloud_strm_increment",
            "event": EventType.PluginAction,
            "desc": "云盘strm文件生成(增量版)",
            "category": "",
            "data": {
                "action": "cloud_strm_increment"
            }
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "CloudStrmAce",
                "name": "云盘增量strm增强",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.scan,
                "kwargs": {}
            }]
        return []

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
                                    'md': 2
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
                                    'md': 2
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '运行一次',
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
                                            'model': 'ismove',
                                            'label': '是否移动（原文件会删除）',
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
                                            'model': 'copy_files',
                                            'label': '复制非媒体文件',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '生成周期',
                                            'placeholder': '0 0 * * *'
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
                                            'model': 'no_del_dirs',
                                            'label': '保留路径',
                                            'placeholder': 'series、movies、downloads、others'
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
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '增量目录#监控目录#目的目录#媒体服务器内源文件路径'
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
                                            'model': 'rmt_mediaext',
                                            'label': '视频格式',
                                            'rows': 2,
                                            'placeholder': ".mp4, .mkv, .ts, .iso, .rmvb, .avi, .mov, .mpeg, .mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .tp, .f4v"
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
                                            'model': 'rmt_nomediaext',
                                            'label': '非媒体格式',
                                            'rows': 2,
                                            'placeholder': ".nfo, .jpg, .jpeg, .png, .svg, .ass, .srt, .sup, .mp3, .flac, .wav, .aac"
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
                                            'text': '目录监控格式：'
                                                    '1.增量目录#监控目录#目的目录#媒体服务器内源文件路径；'
                                                    '2.增量目录#监控目录#目的目录#alist#alist挂载本地跟路径#alist服务地址。'
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
                                            'text': '媒体服务器内源文件路径：源文件目录即云盘挂载到媒体服务器的路径。'
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
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': '配置教程请参考：'
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/CloudStrm.md',
                                                    'target': '_blank'
                                                },
                                                'text': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/CloudStrm.md'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "cron": "",
            "onlyonce": False,
            "ismove": True,
            "copy_files": False,
            "monitor_confs": "",
            "no_del_dirs": "",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso, .rmvb, .avi, .mov, .mpeg, .mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .tp, .f4v",
            "rmt_nomediaext": ".nfo, .jpg, .jpeg, .png, .svg, .ass, .srt, .sup, .mp3, .flac, .wav, .aac"
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
            logger.error("退出插件失败：%s" % str(e))
