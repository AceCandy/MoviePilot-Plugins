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

class AutoStrm(_PluginBase):
    # 插件名称
    plugin_name = "AutoStrm"
    # 插件描述
    plugin_desc = "定时扫描Alist云盘，自动生成Strm文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "kufei326"
    # 作者主页
    author_url = "https://github.com/kufei326"
    # 插件配置项ID前缀
    plugin_config_prefix = "autostrm_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _root_path = None
    _site_url = None
    _target_directory = None
    _username = None
    _password = None
    _try_max = 3

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._root_path = config.get("RootPath")
            self._site_url = config.get("SiteUrl")
            self._target_directory = config.get("TargetDirectory")
            self._username = config.get("Username")
            self._password = config.get("Password")

        # 停止现有任务
        self.stop_service()

        if self._enabled:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 周期运行
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.create_strm_files,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="云盘监控生成")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @eventmanager.register(EventType.PluginAction)
    def create_strm_files(self, event: Event = None):
        """
        创建Strm文件
        """
        if not self._enabled:
            logger.error("AutoStrm插件未开启")
            return

        logger.info("AutoStrm生成Strm任务开始")

        json_structure = {}
        self.traverse_directory(self._root_path, json_structure)

        os.makedirs(self._target_directory, exist_ok=True)  # 确保目标文件夹存在

        base_url = self._site_url + '/d' + self._root_path + '/'

        self.__create_strm_files(json_structure, self._target_directory, base_url)

        logger.info("云盘Strm生成任务完成")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘Strm生成任务完成！",
                              userid=event.event_data.get("user"))

    def traverse_directory(self, path, json_structure):
        """
        遍历目录
        """
        logger.info(f"AutoStrm正在遍历文件夹: {path}")

        directory_info = self.list_directory(path)

        if directory_info.get('data') and directory_info['data'].get('content'):
            for item in directory_info['data']['content']:
                if item['is_dir']:  # 如果是文件夹
                    new_path = os.path.join(path, item['name'])
                    time.sleep(1)
                    new_json_object = {}
                    json_structure[item['name']] = new_json_object
                    self.traverse_directory(new_path, new_json_object)  # 递归调用以遍历子文件夹
                elif self.is_video_file(item['name']):  # 如果是视频文件
                    json_structure[item['name']] = {
                        'type': 'file',
                        'size': item['size'],
                        'modified': item['modified']
                    }

    def list_directory(self, path):
        """
        列出目录
        """
        url_list = self._site_url + "/api/fs/list"
        payload_list = json.dumps({
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": False
        })
        headers_list = {
            'Authorization': token,
            'User-Agent': UserAgent,
            'Content-Type': 'application/json'
        }
        try:
            response_list = self.requests_retry_session().post(url_list, headers=headers_list, data=payload_list)
            return json.loads(response_list.text)

        except Exception as x:
            logger.error(f"遇到错误: {x.__class__.__name__}")
            logger.info("正在重试...")
            time.sleep(5)
        response_list = requests.post(url_list, headers=headers_list, data=payload_list)
        return json.loads(response_list.text)

    def is_video_file(self, filename):
        """
        检查是否为视频文件
        """
        video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv']
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    def __create_strm_files(self, json_structure, target_directory, base_url, current_path=''):
        """
        创建Strm文件
        """
        for name, item in json_structure.items():
            if isinstance(item, dict) and item.get('type') == 'file' and self.is_video_file(name):
                strm_filename = name.rsplit('.', 1)[0] + '.strm'
                strm_path = os.path.join(target_directory, current_path, strm_filename)
                encoded_file_path = urllib.parse.quote(os.path.join(current_path.replace('\\', '/'), name))
                video_url = base_url + encoded_file_path

                with open(strm_path, 'w', encoding='utf-8') as strm_file:
                    strm_file.write(video_url)
            elif isinstance(item, dict):  # 如果是一个目录，递归处理
                new_directory = os.path.join(target_directory, current_path, name)
                os.makedirs(new_directory, exist_ok=True)
                self.__create_strm_files(item, target_directory, base_url, os.path.join(current_path, name))

    def requests_retry_session(self, retries=3, backoff_factor=0.3, status_forcelist=(500, 502, 504), session=None):
        session = session or requests.Session()
        retry = Retry(
            total=retries,
            read=retries,
            connect=retries,
            backoff_factor=backoff_factor,
            status_forcelist=status_forcelist,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "RootPath": self._root_path,
            "SiteUrl": self._site_url,
            "TargetDirectory": self._target_directory,
            "Username": self._username,
            "Password": self._password
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
            "cmd": "/auto_strm",
            "event": EventType.PluginAction,
            "desc": "Alist云盘Strm文件生成",
            "category": "",
            "data": {
                "action": "auto_strm"
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
                "id": "AutoStrm",
                "name": "Alist云盘Strm文件生成服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.create_strm_files,
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
                    }
                ]
            }
        ], {
            "enabled": False,
            "cron": "",
            "RootPath": "",
            "SiteUrl": "",
            "TargetDirectory": "",
            "Username": "",
            "Password": ""
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
