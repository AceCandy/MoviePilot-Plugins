import os
from time import sleep
import requests
import json
import urllib
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from app.plugins import _PluginBase  # 假设这是 MoviePilot 的插件基类
from app.log import logger  # 假设这是 MoviePilot 的日志记录器
from typing import List, Dict, Any, Tuple

class AlistStrm(_PluginBase):
    plugin_name = "AlistStrm"
    plugin_desc = "生成 Alist 云盘视频的 Strm 文件"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "kufei326"
    # 作者主页
    author_url = "https://github.com/kufei326"
    # 插件配置项ID前缀
    plugin_config_prefix = "aliststrm_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    def __init__(self, config_data: dict):
        self.root_path = config_data.get('root_path', "/path/to/root")
        self.site_url = config_data.get('site_url', 'www.tefuir0829.cn')
        self.target_directory = config_data.get('target_directory', 'E:\\cloud\\')
        self.username = config_data.get('username', 'admin')
        self.password = config_data.get('password', 'password')
        self.api_base_url = self.site_url + "/api"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
        self.login_path = "/auth/login"
        self.url_login = self.api_base_url + self.login_path
        self.traversed_paths = []
        self.token = self.get_token()

    def get_token(self):
        payload_login = json.dumps({
            "username": self.username,
            "password": self.password
        })
        headers_login = {
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json'
        }
        response_login = requests.post(self.url_login, headers=headers_login, data=payload_login)
        return json.loads(response_login.text)['data']['token']

    def list_directory(self, path):
        url_list = self.api_base_url + "/fs/list"
        payload_list = json.dumps({
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": False
        })
        headers_list = {
            'Authorization': self.token,
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json'
        }
        try:
            response_list = self.requests_retry_session().post(url_list, headers=headers_list, data=payload_list)
            return json.loads(response_list.text)

        except Exception as x:
            logger.error(f"遇到错误: {x.__class__.__name__}")  # 使用 MoviePilot 的日志记录器
            logger.info("正在重试...")
            sleep(5)
        response_list = requests.post(url_list, headers=headers_list, data=payload_list)
        return json.loads(response_list.text)

    def traverse_directory(self, path, json_structure):
        logger.info(f"正在遍历文件夹: {path}")  # 使用 MoviePilot 的日志记录器
        directory_info = self.list_directory(path)
        if directory_info.get('data') and directory_info['data'].get('content'):
            for item in directory_info['data']['content']:
                if item['is_dir']:
                    new_path = os.path.join(path, item['name'])
                    sleep(1)
                    if new_path in self.traversed_paths:
                        continue
                    self.traversed_paths.append(new_path)
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

                # 对整个文件路径进行URL编码
                encoded_file_path = urllib.parse.quote(os.path.join(current_path.replace('\\', '/'), name))

                # 拼接完整的视频URL
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
        self.traverse_directory(self.root_path, json_structure)
        os.makedirs(self.target_directory, exist_ok=True)
        base_url = self.site_url + '/d' + self.root_path + '/'
        sleep(10)
        logger.info('所有strm文件创建完成') 
        self.create_strm_files(json_structure, self.target_directory, base_url)

        # 如果需要，可以将 json_structure 写入到 JSON 文件中
        with open('directory_structure.json', 'w') as f:
            json.dump(json_structure, f, indent=4)

    # MoviePilot 插件方法 
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
