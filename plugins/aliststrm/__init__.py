import os
import urllib.parse
from time import sleep
from pathlib import Path
import requests
import json

from app.plugins import _PluginBase
from app.log import logger
from typing import List, Dict, Any, Tuple

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

    _root_path = None
    _site_url = None
    _target_directory = None
    _username = None
    _password = None
    _api_base_url = None
    _user_agent = None
    _login_path = None
    _url_login = None
    _traversed_paths = []
    _token = None

    def __init__(self, config_data: dict):
        self._root_path = config_data.get('root_path', "/path/to/root")
        self._site_url = config_data.get('site_url', 'www.tefuir0829.cn')
        self._target_directory = config_data.get('target_directory', 'E:\\cloud\\')
        self._username = config_data.get('username', 'admin')
        self._password = config_data.get('password', 'password')
        self._api_base_url = self._site_url + "/api"
        self._user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
        self._login_path = "/auth/login"
        self._url_login = self._api_base_url + self._login_path
        self._token = self.get_token()

    def get_token(self):
        payload_login = json.dumps({
            "username": self._username,
            "password": self._password
        })
        headers_login = {
            'User-Agent': self._user_agent,
            'Content-Type': 'application/json'
        }
        response_login = requests.post(self._url_login, headers=headers_login, data=payload_login)
        return json.loads(response_login.text)['data']['token']

    def list_directory(self, path):
        url_list = self._api_base_url + "/fs/list"
        payload_list = json.dumps({
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": False
        })
        headers_list = {
            'Authorization': self._token,
            'User-Agent': self._user_agent,
            'Content-Type': 'application/json'
        }
        try:
            response_list = self.requests_retry_session().post(url_list, headers=headers_list, data=payload_list)
            return json.loads(response_list.text)

        except Exception as x:
            logger.error(f"遇到错误: {x.__class__.__name__}")
            logger.info("正在重试...")
            sleep(5)
        response_list = requests.post(url_list, headers=headers_list, data=payload_list)
        return json.loads(response_list.text)

    def traverse_directory(self, path, json_structure):
        logger.info(f"正在遍历文件夹: {path}")
        directory_info = self.list_directory(path)
        if directory_info.get('data') and directory_info['data'].get('content'):
            for item in directory_info['data']['content']:
                if item['is_dir']:
                    new_path = os.path.join(path, item['name'])
                    sleep(1)
                    if new_path in self._traversed_paths:
                        continue
                    self._traversed_paths.append(new_path)
                    new_json_object = {}
                    json_structure[item['name']] = new_json_object
                    self.traverse_directory(new_path, new_json_object)
                elif self.is_video_file(item['name']):
                    json_structure[item['name']] = {
                        'type': 'file',
                        'size': item['size'],
                        'modified': item['modified']
                    }

    def is_video_file(self, filename):
        video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv']
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    def create_strm_files(self, json_structure, target_directory, base_url, current_path=''):
        for name, item in json_structure.items():
            if isinstance(item, dict) and item.get('type') == 'file' and self.is_video_file(name):
                strm_filename = name.rsplit('.', 1)[0] + '.strm'
                strm_path = os.path.join(target_directory, current_path, strm_filename)
                encoded_file_path = urllib.parse.quote(os.path.join(current_path.replace('\\', '/'), name))
                video_url = base_url + encoded_file_path
                with open(strm_path, 'w', encoding='utf-8') as strm_file:
                    strm_file.write(video_url)
            elif isinstance(item, dict):
                new_directory = os.path.join(target_directory, current_path, name)
                os.makedirs(new_directory, exist_ok=True)
                self.create_strm_files(item, target_directory, base_url, os.path.join(current_path, name))

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

    def run(self):
        logger.info('脚本运行中...')
        json_structure = {}
        self.traverse_directory(self._root_path, json_structure)
        os.makedirs(self._target_directory, exist_ok=True)
        base_url = self._site_url + '/d' + self._root_path + '/'
        sleep(10)
        logger.info('所有strm文件创建完成')
        self.create_strm_files(json_structure, self._target_directory, base_url)
        with open('directory_structure.json', 'w') as f:
            json.dump(json_structure, f, indent=4)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VTextField',
                        'props': {
                            'model': 'root_path',
                            'label': 'Alist 根目录路径',
                        }
                    },
                    {
                        'component': 'VTextField',
                        'props': {
                            'model': 'site_url',
                            'label': 'Alist 网站地址',
                        }
                    },
                    {
                        'component': 'VTextField',
                        'props': {
                            'model': 'target_directory',
                            'label': 'Strm 文件输出目录',
                        }
                    },
                    {
                        'component': 'VTextField',
                        'props': {
                            'model': 'username',
                            'label': 'Alist 用户名',
                        }
                    },
                    {
                        'component': 'VTextField',
                        'props': {
                            'model': 'password',
                            'label': 'Alist 密码',
                            'type': 'password'
                        }
                    },
                ]
            }
        ], {
            "root_path": "/path/to/root",
            "site_url": "www.tefuir0829.cn",
            "target_directory": "E:\\cloud\\",
            "username": "admin",
            "password": "password"
        }

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/generate_strm",
                "event": EventType.PluginAction,
                "desc": "生成 Alist 云盘 Strm 文件",
                "category": "",
                "data": {
                    "action": "generate_strm"
                }
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_generate_strm_event(self, event: Event = None):
        if event and event.event_data.get("action") == "generate_strm":
            self.run()

