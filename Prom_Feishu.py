import httpx, json, logging, time, os, config, re, yaml
from requests_toolbelt import MultipartEncoder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('pass')


class HttpClient:
    def __init__(self):
        self.max_retries = 3  # 最大重试次数

    def http_get(self, url, params=None, headers=None):
        """封装GET请求，含重试机制"""
        retries = 0  # 每次调用独立计数

        while retries < self.max_retries:
            try:
                with httpx.Client(verify=False) as client:  # 正确变量名
                    response = client.get(
                        url=url,
                        params=params,
                        headers=headers,
                        timeout=10
                    )
                    response.raise_for_status()
                    return response
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                logger.error(f"请求失败: {str(e)}，正在进行第 {retries + 1} 次重试")
                retries += 1
                if retries < self.max_retries:
                    time.sleep(1 * retries)
        raise httpx.RequestError(f"请求 {url} 失败，已达最大重试次数 {self.max_retries}")


class local_config:
    def default_config(self):
        config_path = os.path.join(os.path.dirname(__file__), 'default.conf')

        try:
            with open(config_path, 'r') as f:
                conf = yaml.safe_load(f)
            return conf
        except FileNotFoundError:
            raise Exception(f"配置文件:{config_path}不存在!")
        except yaml.YAMLError as y:
            raise Exception(f"配置文件书写有误!")


class Prome_Data:

    def Get_Data(self):
        conf = local_config()
        client = HttpClient()
        try:
            pro_url = conf.default_config()['Prometheus']['url'] + '/api/v1/rules'
            response = client.http_get(
                url=pro_url,
                params={},
                headers={'Content-Type': 'application/json'}
            )
            # print(f"请求成功, 状态码: {response.status_code}")
            if response.status_code == httpx.codes.OK:
                config = {
                    'query': json.dumps(response.json()['data']['groups'][0]['rules'][0]['query'], indent=2),
                    'alert_name': json.dumps(response.json()['data']['groups'][0]['rules'][0]['name'], indent=2),
                    'duration': json.dumps(response.json()['data']['groups'][0]['rules'][0]['duration'], indent=2)
                }
                # print(config)
                return config
        except httpx.RequestError as e:
            logger.error(str(e))


class Grafana_Data:

    def __init__(self):
        conf = local_config()
        self.headers = {
            'Accept': 'application/json',
            'Authorization': conf.default_config()['Grafana']['token']
        }
        self.panel_id = ""
        self.dashboard_id = conf.default_config()['Grafana']['id']
        self.address = conf.default_config()['Grafana']['url']
        self.url = f'{self.address}/api/dashboards/uid/{self.dashboard_id}'

        self.base_path = conf.default_config()['Grafana']['pict_path']

    def Get_Dashboard(self, panel_id):
        client = HttpClient()
        try:
            self.panel_id = panel_id
            if self.panel_id is not None:
                timestamp = int(time.time())
                filename = f"alert_{self.panel_id}_{timestamp}.png"
                # 构建完整路径
                self.out_path = os.path.join(os.path.dirname(self.base_path), filename)

                render_url = (
                    f"{self.address}/render/d-solo/{self.dashboard_id}/?orgId=1&panelId={self.panel_id}&width=1000&height=500"
                )
                response = client.http_get(
                    url=self.url,
                    headers=self.headers
                )

                img = client.http_get(
                    url=render_url,
                    headers=self.headers
                )
                if img.status_code == httpx.codes.OK:
                    os.makedirs(os.path.dirname(self.out_path), exist_ok=True)
                    with open(self.out_path, 'wb') as f:
                        f.write(img.content)
                        logger.info(f"告警图片下载成功, 保存到位置: {self.out_path}")
                return self.out_path
        except httpx.RequestError as e:
            logger.error(f"地址请求失败: {e}")


class Feishu_Alert:
    def __init__(self):
        conf = local_config()
        self.app_id = conf.default_config()['Feishu']['app_id']
        self.app_secret = conf.default_config()['Feishu']['app_secret']
        self.file_path = None
        # self.headers = {'Authorization': conf.default_config()['Feishu']['token']}
        self.webhook = 'https://open.feishu.cn/open-apis/bot/v2/hook/3342b8fa-446f-413b-ad46-1719b75106ad'

    def set_file_path(self, path):
        self.file_path = path

    def get_tenant_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }
        try:
            with httpx.Client(verify=False) as client:  # 使用上下文管理器
                # 正确使用POST方法和json参数
                response = client.post(
                    url=url,
                    json=payload,  # 自动设置Content-Type和序列化
                    timeout=10  # 添加超时控制
                )
                response.raise_for_status()  # 自动检查HTTP状态码

                # 解析JSON响应
                token_data = response.json()
                if token_data.get("code") != 0:
                    raise ValueError(f"飞书API返回错误: {token_data.get('msg')}")

                self.token = token_data["tenant_access_token"]
                logger.info("成功获取租户访问令牌")
                return self.token

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP错误: {e.response.status_code} - {e.response.text}")
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.error(f"网络连接错误: {str(e)}")
            raise
        except ValueError as e:
            logger.error(f"业务逻辑错误: {str(e)}")
            raise

    def get_ImageKey(self):
        token = self.get_tenant_token()
        image_key_headers = {
            'Authorization': 'Bearer ' + token,
        }
        get_image_key_url = "https://open.feishu.cn/open-apis/im/v1/images"
        filename = os.path.basename(self.file_path)
        with open(self.file_path, 'rb') as file:
            # 修正 files 参数：指定文件名 + MIME 类型
            files = {'image': (filename, file, 'image/png')}
            data = {'image_type': 'message'}

            client = httpx.Client()
            # 注意：data 和 files 参数需分开传递
            response = client.post(url=get_image_key_url, headers=image_key_headers, data=data,
                                   files=files)  # 相应内容: {'code': 0, 'data': {'image_key': 'img_v3_02j2_1ec9ed69-730c-42a1-91e4-4e3e5cf867cg'}, 'msg': 'success'}

        return response.json()['data']['image_key']

    def push_Img(self):
        feishu_url = self.webhook
        image_key = self.get_ImageKey()
        # print(image_key)
        token = self.get_tenant_token()
        headers = {
            'Authorization': 'Bearer ' + token,
        }
        form = {
            'msg_type': 'image',
            'content':
                {"image_key": image_key}
        }

        with httpx.Client(verify=False) as Client:
            response = Client.post(url=feishu_url, json=form, headers=headers)

