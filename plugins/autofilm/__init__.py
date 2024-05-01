import os
from datetime import datetime, timedelta
from webdav3.client import Client
import time
import requests

import pytz
from typing import Any, List, Dict, Tuple, Optional

from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings


class AutoFilm(_PluginBase):
    # 插件名称
    plugin_name = "AutoFilm—MoviePilot插件版"
    # 插件描述
    plugin_desc = "定时扫描Alist云盘，自动生成Strm文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "0.5"
    # 插件作者
    plugin_author = "Akimio521"
    # 作者主页
    author_url = "https://github.com/Akimio521"
    # 插件配置项ID前缀
    plugin_config_prefix = "autofilm_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _monitor_confs = None
    _onlyonce = False
    _download_subtitle = False

    _autofilm_confs = None

    _try_max = 15

    _video_formats = ('.mp4', '.avi', '.rmvb', '.wmv', '.mov', '.mkv', '.flv', '.ts', '.webm', '.iso', '.mpg', '.m2ts')
    _subtitle_formats = ('.ass', '.srt', '.ssa', '.sub')

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
  
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._download_subtitle = config.get("download_subtitle")
            self._autofilm_confs = config.get("autofilm_confs").split("\n")

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("AutoFilm执行服务启动，立即运行一次")
                self._scheduler.add_job(func=self.scan, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="AutoFilm单次执行")
                # 关闭一次性开关
                self._onlyonce = False

            # 周期运行
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.scan,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="云盘监控生成")
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
            logger.error("AutoFilm插件未开启")
            return
        if not self._autofilm_confs:
            logger.error("未获取到可用目录监控配置，请检查")
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "auto_film":
                return
            logger.info("AutoFilm收到命令，开始生成Alist云盘Strm文件 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="AutoFilm开始生成strm ...",
                              userid=event.event_data.get("user"))

        logger.info("AutoFilm生成Strm任务开始")
        
        # 生成strm文件
        for autofilm_conf in self._autofilm_confs:
            # 格式 Webdav服务器地址:账号:密码:本地目录
            if not autofilm_conf:
                continue
            if str(autofilm_conf).count("#") == 3:
                webdav_url = str(autofilm_conf).split("#")[0]
                webdav_account = str(autofilm_conf).split("#")[1]
                webdav_password = str(autofilm_conf).split("#")[2]
                local_path = str(autofilm_conf).split("#")[3]
            else:
                logger.error(f"{autofilm_conf} 格式错误")
                continue

            # 生成strm文件
            self.__generate_strm(webdav_url, webdav_account, webdav_password, local_path)

        logger.info("云盘strm生成任务完成")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘strm生成任务完成！",
                              userid=event.event_data.get("user"))

    def __generate_strm(self, webdav_url:str, webdav_account:str, webdav_password:str, local_path:str):
        """
        生成Strm文件
        """
        dir_url_list = []
        files_list = []
        dir_url_list.append(webdav_url)

        # 获取目录下所有文件
        while dir_url_list:
            url = dir_url_list.pop(0)
            # 连接该Webdav服务器
            client = Client(options={"webdav_hostname": url,"webdav_login": webdav_account,"webdav_password": webdav_password})
            try_number = 1
            while try_number <= self._try_max:
                try:
                    items = client.list()
                except Exception as e:
                    logger.warning(f"AutoFilm连接{url}遇到错误，第{try_number}尝试失败；错误信息：{str(e)}，传入URL：{url}")
                    time.sleep(try_number)
                    try_number += 1
                else:
                    if try_number > 1:
                        logger.info(f"{url}重连成功")
                    break
            for item in items[1:]:
                if item.endswith("/"):
                    dir_url_list.append(url + item)
                else:
                    files_list.append(url + item)
        
        logger.info(f"AutoFilm获取到{len(files_list)}个文件，开始生成strm文件")

        for file_url in files_list:
            if file_url.lower().endswith(tuple(self._video_formats)):
                strm_file_path = os.path.join(local_path, file_url.replace(webdav_url, '').rsplit(".", 1)[0] + ".strm")
                os.makedirs(os.path.dirname(strm_file_path), exist_ok=True) # 创建递归目录
                with open(strm_file_path, "w") as f:
                    url_string = file_url.replace("/dav", "/d")
                    f.write(url_string)
            elif file_url.lower().endswith(tuple(self._subtitle_formats)):
                try_number = 1
                while try_number <= self._try_max:
                    try:
                        response = requests.get(file_url.replace("/dav", "/d"))
                    except Exception as e:
                        logger.warning(f"AutoFilm下载{file_url}遇到错误，第{try_number}尝试失败；错误信息：{str(e)}，传入URL：{file_url}")
                        time.sleep(try_number)
                        try_number += 1
                    else:
                        if try_number > 1:
                            logger.info(f"{file_url}下载成功")
                        break
                    
                subtitile_file_path = os.path.join(local_path, file_url.replace(webdav_url, ''))
                os.makedirs(os.path.dirname(subtitile_file_path), exist_ok=True) # 创建递归目录
                with open(subtitile_file_path, "w") as f:
                    f.write(response.content)
        
    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "rebuild": self._rebuild,
            "copy_files": self._copy_files,
            "cron": self._cron,
            "monitor_confs": self._monitor_confs
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
            "cmd": "/auto_film",
            "event": EventType.PluginAction,
            "desc": "Alist云盘Strm文件生成",
            "category": "",
            "data": {
                "action": "auto_film"
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
                "id": "AutoFilm",
                "name": "Alist云盘strm文件生成服务",
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
                                    'md': 4
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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'download_subtitle',
                                            'label': '下载字幕',
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
                                            'model': 'autofilm_confs',
                                            'label': 'AutoFilm配置文件',
                                            'rows': 5,
                                            'placeholder': 'Webdav服务器地址#账号#密码#本地目录'
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
            "cron": "",
            "onlyonce": False,
            "download_subttile": False,
            "autofilm_confs": ""
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
