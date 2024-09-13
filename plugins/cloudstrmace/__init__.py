import os
import shutil
import threading
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings
from app.utils.system import SystemUtils


class CloudStrmAce(_PluginBase):
    # 插件基础信息
    plugin_name = "增量生成云盘Strm"
    plugin_desc = "监控本地增量目录，转移到媒体目录，并生成Strm文件上传到云盘目录"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    plugin_version = "1.2"
    plugin_author = "AceCandy"
    author_url = "https://github.com/AceCandy"
    plugin_config_prefix = "cloudstrmace_"
    plugin_order = 26
    auth_level = 2

    # 退出事件
    _event = threading.Event()

    # 默认属性
    default_mediaext = ".mp4, .mkv, .ts, .iso, .rmvb, .avi, .mov, .mpeg, .mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .tp, .f4v"
    default_nomediaext = ".nfo, .jpg, .jpeg, .png, .svg, .ass, .srt, .sup, .mp3, .flac, .wav, .aac"
    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _copy_files = False
    _monitor_confs = None
    _no_del_dirs = None
    _rmt_mediaext = default_mediaext
    _rmt_nomediaext = default_nomediaext

    # 公开属性
    _monitor_items = []
    nomedia_exts = []
    media_exts = []
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._monitor_items = []

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._copy_files = config.get("copy_files")
            self._monitor_confs = config.get("monitor_confs")
            self._no_del_dirs = config.get("no_del_dirs")
            self._rmt_mediaext = config.get("rmt_mediaext") or self.default_mediaext
            self._rmt_nomediaext = config.get("rmt_nomediaext") or self.default_nomediaext

            self.nomedia_exts = [ext.strip() for ext in self._rmt_nomediaext.split(",")]
            self.media_exts = [ext.strip() for ext in self._rmt_mediaext.split(",")]

            # 读取目录配置
            monitor_confs = self._monitor_confs.strip().split("\n")
            if not monitor_confs:
                return

            for monitor_conf in monitor_confs:
                # 去除注释和空行 格式:增量目录:媒体库目录:云盘目录:云盘strm前缀链接
                monitor_conf = monitor_conf.strip()
                if not monitor_conf or monitor_conf.startswith("#"):
                    continue

                parts = monitor_conf.split("#")
                if len(parts) == 4:
                    self._monitor_items.append(MonitorItem(parts[0], parts[1], parts[2], parts[3]))
                else:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue

        # 停止现有任务
        self.stop_service()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(func=self.scan, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name=self.plugin_name)
            logger.info(f"{self.plugin_name}服务启动，立即运行一次")

            # 关闭一次性开关
            self._onlyonce = False
            # 保存配置
            self.__update_config()
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    # 更新配置
    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "copy_files": self._copy_files,
            "monitor_confs": self._monitor_confs,
            "no_del_dirs": self._no_del_dirs,
            "rmt_mediaext": self._rmt_mediaext,
            "rmt_nomediaext": self._rmt_nomediaext
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    # 注册插件公共服务
    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "CloudStrmAce",
                "name": self.plugin_name,
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.scan,
                "kwargs": {}
            }]

    # 拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
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
                                    'md': 4
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
                                            'placeholder': '增量目录#媒体库目录#云盘目录#Strm前缀路径'
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
                                            'text': '目录监控格式：增量目录#媒体库目录#云盘目录#Strm前缀路径\n'
                                                    '通过监控增量目录的文件，转移到媒体库目录，然后将媒体库目录中的文件上传到云盘目录，生成的strm文件以Strm前缀路径开头\n'
                                                    '如果增量目录和媒体库目录一致，则不用进行转移，不过每次会全量扫，建议配置不同的目录\n'
                                                    '媒体文件默认是移动到云盘目录中，原文件会消失并生成strm文件\n'
                                                    '非媒体文件默认是复制到云盘目录中，原文件不受影响\n'

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
            "onlyonce": False,
            "cron": "",
            "copy_files": False,
            "monitor_confs": "",
            "no_del_dirs": "",
            "rmt_mediaext": self.default_mediaext,
            "rmt_nomediaext": self.default_nomediaext
        }

    def get_page(self) -> List[dict]:
        pass

    # 停止服务
    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"服务停止失败: {e}")

    # 主要执行扫描逻辑
    def scan(self):
        if not self._enabled:
            logger.warning("插件未开启")
            return

        if not self._monitor_items:
            logger.warning("有效监控目录为空,请检查配置")
            return

        logger.info(f"{self.plugin_name}任务开始>>>>>>>>>>>>>>>")
        for monitor_item in self._monitor_items:
            increment_dir = monitor_item.increment_dir
            media_dir = monitor_item.media_dir
            cloud_dir = monitor_item.cloud_dir
            cloud_url = monitor_item.cloud_url
            logger.info(f"开始扫描增量目录 "
                        f"增量目录:{increment_dir} 媒体库目录:{media_dir} "
                        f"云盘目录:{cloud_dir} Strm前缀路径:{cloud_url}")
            for root, dirs, files in os.walk(increment_dir):
                for file in files:
                    increment_file = os.path.join(root, file)
                    if not Path(increment_file).exists():
                        continue

                    # 回收站及隐藏的文件不处理
                    if any(marker in increment_file for marker in ["/@Recycle", "/#recycle", "/.", "/@eaDir"]):
                        logger.info(f"{increment_file} 是回收站或隐藏的文件，跳过处理")
                        continue

                    if increment_dir == media_dir:
                        #logger.info(f"{increment_dir} 增量目录和媒体目录相同，不进行移动")
                        media_file = increment_file
                    else:
                        # 移动后文件路径
                        media_file = increment_file.replace(increment_dir, media_dir)
                        # 判断目标路径的文件夹是否存在
                        Path(media_file).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(increment_file, media_file, copy_function=shutil.copy2)
                    # 扫描云盘文件生成strm，需要先判断是否有对应strm
                    self.__strm(media_file, media_dir, cloud_dir, cloud_url)
                    #logger.info(f"增量文件 {increment_file} 处理完成")
                    # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
                    self._clean_empty_parent_dirs(increment_file)
        logger.info(f"{self.plugin_name}任务完成>>>>>>>>>>>>>>>\n\n\n\n")

    def _is_valid_file(self, file_suffix):
        if file_suffix not in self.nomedia_exts and file_suffix not in self.media_exts:
            return False
        if not self._copy_files and file_suffix not in self.media_exts:
            return False
        return True

    def _clean_empty_parent_dirs(self, increment_file):
        increment_file_path = Path(increment_file)
        if not SystemUtils.exits_files(increment_file_path).parent, []):
            for parent_path in list(increment_file_path.parents):
                if parent_path.name in self._no_del_dirs:
                    break
                if parent_path.name == increment_dir:
                    break
                if parent_path.parent != Path(increment_file).root:
                    # 父目录非根目录，才删除父目录
                    if not SystemUtils.exits_files(parent_path, []):
                        # 当前路径下没有媒体文件则删除
                        shutil.rmtree(parent_path)
                        logger.warn(f"增量非保留目录 {parent_path} 已删除")

    # 扫描云盘文件生成strm，需要先判断是否有对应strm
    def __strm(self, media_file, media_dir, cloud_dir, cloud_url):
        # 非保留文件（视频+非媒体）直接跳过
        file_suffix = Path(media_file).suffix
        if not self._is_valid_file(file_suffix):
            return

        try:
            cloud_file = media_file.replace(media_dir, cloud_dir)
            cloud_file_path = Path(cloud_file)
            # 如果是文件夹进行创建
            if cloud_file_path.is_dir():
                cloud_file_path.mkdir(parents=True, exist_ok=True)
                return
            elif cloud_file_path.exists():
                return

            # 创建对应文件父目录
            cloud_file_path.parent.mkdir(parents=True, exist_ok=True)
            # 视频文件创建.strm文件
            if file_suffix in self.media_exts:
                # 移动文件到云盘目录
                shutil.move(media_file, cloud_file, copy_function=shutil.copy2)
                # 创建.strm文件
                self.__create_strm_file(media_file, cloud_file, cloud_url)
            elif self._copy_files and file_suffix in self.nomedia_exts:
                # 其他nfo、jpg等复制文件
                shutil.copy2(media_file, cloud_file)
                logger.info(f"复制增量文件 {media_file} 到 {cloud_file}")

        except Exception as e:
            logger.error(f"文件处理异常: {e}")

    # 生成strm文件
    @staticmethod
    def __create_strm_file(media_file, cloud_file, cloud_url):
        try:
            # 获取视频文件名和父目录
            media_file_path = Path(media_file)
            file_parent_path = media_file_path.parent

            # 构造.strm文件路径
            strm_path = file_parent_path / f"{media_file_path.stem}.strm"
            # strm已存在跳过处理
            if strm_path.exists():
                logger.info(f"strm文件已存在 {strm_path}")
                return

            #logger.info(f"替换前本地路径 >> {media_file}")
            # 云盘模式
            if cloud_url.startswith("http"):
                # 替换路径中的\为/
                cloud_file = urllib.parse.quote(cloud_file.replace("\\", "/"), safe='')
                cloud_url = f"{cloud_url}/{cloud_file}"
                logger.info(f"[云盘]strm文件中路径 >> {cloud_url}")
            else:
                # 本地挂载路径转为emby路径
                cloud_url = f"{cloud_url}/{cloud_file}"
                logger.info(f"[本地]strm文件中路径 >> {cloud_url}")

            # 写入.strm文件
            with strm_path.open('w') as f:
                f.write(cloud_url)
            logger.info(f"创建strm文件 >> {strm_path}")
        except (OSError, IOError, Exception) as e:
            logger.error(f"创建strm文件失败: {e}")


class MonitorItem:
    def __init__(self, increment_dir, media_dir, cloud_dir, cloud_url):
        self.increment_dir = increment_dir
        self.media_dir = media_dir
        self.cloud_dir = cloud_dir
        self.cloud_url = cloud_url
