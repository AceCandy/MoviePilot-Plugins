import os
import urllib.parse
from time import sleep
import requests
import json
import configparser
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

import pytz
from typing import Any, List, Dict, Tuple, Optional

from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings

class AlistStrm(_PluginBase):
    plugin_name = "AlistStrm"
    plugin_desc = "生成 Alist 云盘视频的 Strm 文件"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    plugin_version = "1.0"
    plugin_author = "kufei326"
    author_url = "https://github.com/kufei326"
    plugin_config_prefix = "aliststrm_"
    plugin_order = 26
    auth_level = 1

    _enabled = False
    _cron = None
    _onlyonce = False
    _download_subtitle = False

    _liststrm_confs = None

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
            self._liststrm_confs = config.get("liststrm_confs").split("\n")

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
        if not self._enabled:
            logger.error("aliststrm插件未开启")
            return
        if not self._liststrm_confs:
            logger.error("未获取到可用目录监控配置，请检查")
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "alist_strm":
                return
            logger.info("aliststrm收到命令，开始生成Alist云盘Strm文件 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="aliststrm开始生成strm ...",
                              userid=event.event_data.get("user"))

        logger.info("AutoFilm生成Strm任务开始")
        
        # 生成strm文件
        for liststrm_conf in self._liststrm_confs:
            # 格式 Webdav服务器地址:账号:密码:本地目录
            if not liststrm_conf:
                continue
            if str(liststrm_conf).count("#") == 4:
                alist_url = str(liststrm_conf).split("#")[0]
                alist_user = str(liststrm_conf).split("#")[1]
                alist_password = str(liststrm_conf).split("#")[2]
                local_path = str(liststrm_conf).split("#")[3]
                root_path = str(liststrm_conf).split("#")[4]
            else:
                logger.error(f"{liststrm_conf} 格式错误")
                continue

            # 获取token
            token = self.get_token(alist_url, alist_user, alist_password)
            if not token:
                logger.error(f"无法获取token: {alist_url}")
                continue

            # 生成strm文件
            self.__generate_strm(alist_url, alist_user, alist_password, local_path, root_path, token)

        logger.info("云盘strm生成任务完成")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘strm生成任务完成！",
                              userid=event.event_data.get("user"))
                              
    def __generate_strm(self, webdav_url:str, webdav_account:str, webdav_password:str, local_path:str, root_path:str, token:str):
        for path in self.__traverse_directory(local_path):
            self.__create_strm_files(path, root_path, webdav_url, token)

    def __traverse_directory(self, path):
        traversed_paths = []
        json_structure = {}
        self.__traverse_directory_recursively(path, json_structure, traversed_paths)
        return traversed_paths

    def __traverse_directory_recursively(self, path, json_structure, traversed_paths):
        directory_info = self.__list_directory(path)
        if directory_info.get('data') and directory_info['data'].get('content'):
            for item in directory_info['data']['content']:
                if item['is_dir']:  # If it's a directory
                    new_path = os.path.join(path, item['name'])
                    sleep(1)
                    if new_path in traversed_paths:
                        continue
                    traversed_paths.append(new_path)
                    new_json_object = {}
                    json_structure[item['name']] = new_json_object
                    self.__traverse_directory_recursively(new_path, new_json_object, traversed_paths)  # Recursive call to traverse subdirectories
                elif self.__is_video_file(item['name']):  # If it's a video file
                    json_structure[item['name']] = {
                        'type': 'file',
                        'size': item['size'],
                        'modified': item['modified']
                    }

    def __list_directory(self, path, token):
        url_list = webdav_url + "/fs/list"
        payload_list = json.dumps({
            "path": path,
            "password": webdav_password,
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
            response_list = self.__requests_retry_session().post(url_list, headers=headers_list, data=payload_list)
            return json.loads(response_list.text)

        except Exception as x:
            print(f"Error encountered: {x.__class__.__name__}")
            print("Retrying...")
            sleep(5)
            response_list = self.__requests_retry_session().post(url_list, headers=headers_list, data=payload_list)
            return json.loads(response_list.text)

    def get_token(self, url:str, username:str, password:str) -> str:
        api_base_url = url + "/api"
        login_path = "/auth/login"
        url_login = api_base_url + login_path

        payload_login = json.dumps({
            "username": username,
            "password": password
        })

        headers_login = {
            'User-Agent': UserAgent,
            'Content-Type': 'application/json'
        }

        response_login = requests.post(url_login, headers=headers_login, data=payload_login)
        if response_login.status_code == 200:
            token = json.loads(response_login.text)['data']['token']
            return token
        else:
            return ""

    def __create_strm_files(self, local_path, target_directory, base_url, token):
        for name, item in local_path.items():
            if isinstance(item, dict) and item.get('type') == 'file' and self.__is_video_file(name):
                strm_filename = name.rsplit('.', 1)[0] + '.strm'
                strm_path = os.path.join(target_directory, strm_filename)

                # Encode the entire file path
                encoded_file_path = urllib.parse.quote(os.path.join(current_path.replace('\\', '/'), name))

                # Concatenate the complete video URL
                video_url = base_url + encoded_file_path

                with open(strm_path, 'w', encoding='utf-8') as strm_file:
                    strm_file.write(video_url)
            elif isinstance(item, dict):  # If it's a directory, recursively process it
                new_directory = os.path.join(target_directory, name)
                os.makedirs(new_directory, exist_ok=True)
                self.__create_strm_files(item, target_directory, base_url, token, os.path.join(current_path, name))

    def __is_video_file(self, filename):
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv')  # Add more video formats if necessary
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    def __requests_retry_session(self, retries=3, backoff_factor=0.3, status_forcelist=(500, 502, 504), session=None):
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
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "download_subtitle": self._download_subtitle,
            "liststrm_confs": self._liststrm_confs
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/alist_strm",
            "event": EventType.PluginAction,
            "desc": "Alist云盘Strm文件生成",
            "category": "",
            "data": {
                "action": "alist_strm"
            }
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "aliststrm",
                "name": "Alist云盘strm文件生成服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.scan,
                "kwargs": {}
            }]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
                                            'model': 'liststrm_confs',
                                            'label': 'liststrm配置文件',
                                            'rows': 5,
                                            'placeholder': 'Webdav服务器地址#账号#密码#本地目录#Webdav开始目录'
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
            "download_subtitle": False,
            "liststrm_confs": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"Exiting plugin failed: {str(e)}")
