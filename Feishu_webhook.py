import queue
from threading import Thread
import threading
from flask import Flask, request
import httpx
import json
import logging
import time
import os
from Prom_Feishu import HttpClient, Grafana_Data, Feishu_Alert, local_config
from typing import Dict, Any
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('alertmanager_webhook')

app = Flask(__name__)


# 定义配置类
class Config:
    def __init__(self):
        self.alert_name = ""
        self.instance = ""
        self.query = ""
        self.panel_id = None


# 创建全局配置实例
config = Config()

# 创建消息队列
alert_queue = queue.Queue()
# 用于控制处理线程的标志
processing_thread_active = True  # 修正拼写错误


class AlertProcessor(Thread):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue
        self.alert_manager = AlertManager()

    def run(self):
        while processing_thread_active:  # 修正变量名
            try:
                # 从队列中获取告警数据, 设置超时以便能够正常退出
                alert_data = self.queue.get(timeout=1)
                try:
                    # 处理告警
                    self.alert_manager.process_alert(alert_data)
                    time.sleep(5)
                except Exception as e:
                    logger.error(f"处理告警失败: {str(e)}")
                finally:
                    # 标记该告警已处理完成
                    self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"处理队列出错: {str(e)}")
                continue


class AlertManager:
    def __init__(self):
        self.http_client = HttpClient()
        self.grafana = Grafana_Data()
        self.feishu = Feishu_Alert()
        self.processing_alerts = set()  # 用于追踪正在处理的告警
        self.processing_lock = threading.Lock()

    def send_text_message(self, alert: Dict[str, Any]):
        """发送文本告警消息到飞书"""
        status = alert.get('status', 'firing')
        labels = alert.get('labels', {})
        annotations = alert.get('annotations', {})
        start_time = datetime.fromisoformat(alert.get('startsAt', '').replace('Z', '+00:00'))

        # 构建消息文本
        message = {
            "msg_type": "text",
            "content": {
                "text": f"""告警状态: {'🚨告警触发' if status == 'firing' else '✅告警恢复'}
    告警名称: {labels.get('alertname', 'Unknown')}
    告警级别: {labels.get('severity', 'Unknown')}
    告警实例: {labels.get('instance', 'Unknown')}
    告警时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}
    告警详情: {annotations.get('description', 'No description')}
    {f"当前值: {annotations.get('value', 'Unknown')}" if annotations.get('value') else ''}"""
            }
        }

        # 发送消息
        token = self.feishu.get_tenant_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }

        with httpx.Client(verify=False) as client:
            response = client.post(
                url=self.feishu.webhook,
                json=message,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            logger.info("文本告警消息发送成功")

    def process_alert(self, alert_data: Dict[str, Any]):
        """处理来自 Alertmanager 的告警数据"""
        try:
            # 从告警数据中提取必要信息
            for alert in alert_data.get('alerts', []):
                # 生成告警的唯一表示
                alert_id = self._generate_alert_id(alert)

                with self.processing_lock:
                    # 检查是否已经在处理这个告警
                    if alert_id in self.processing_alerts:  # 修正比较语句
                        logger.info(f"告警 {alert_id} 已在处理, 跳过")
                        continue
                    self.processing_alerts.add(alert_id)

                try:
                    labels = alert.get('labels', {})
                    annotations = alert.get('annotations', {})

                    # 发送文本消息
                    self.send_text_message(alert)

                    # 设置配置信息供其他类使用
                    config.alert_name = labels.get('alertname', 'Unknown Alert')
                    config.instance = labels.get('instance', 'none')
                    config.query = annotations.get('query', '')
                    config.panel_id = labels.get('panel_id')
                    logger.info(f"处理告警面板ID: {config.panel_id}")

                    # 获取 Grafana 面板截图并发送到飞书
                    if config.panel_id is not None:
                        image_path = self.grafana.Get_Dashboard(config.panel_id)
                        logger.info(f"获取到图片路径: {image_path}")
                        if image_path:
                            self.feishu.set_file_path(image_path)
                            self.feishu.push_Img()
                            # 等待确保图片发送完成
                            time.sleep(10)
                            # 删除临时文件
                            if os.path.exists(image_path):
                                os.remove(image_path)

                    logger.info(f"Successfully processed alert: {config.alert_name}")
                finally:
                    # 处理完成后从集合中移除
                    with self.processing_lock:
                        self.processing_alerts.remove(alert_id)

        except Exception as e:
            logger.error(f"Error processing alert: {str(e)}")
            raise

    # send_text_message 方法保持不变...

    def _generate_alert_id(self, alert):
        """生成告警的唯一标识"""
        labels = alert.get('labels', {})
        key_fields = [
            labels.get('alertname', ''),
            labels.get('instance', ''),
            labels.get('panel_id', ''),
            alert.get('startsAt', '')
        ]
        return '_'.join(str(field) for field in key_fields)


@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        alert_data = request.json
        logger.info("收到新的告警")

        # 将告警数据放入队列
        alert_queue.put(alert_data)
        return {"status": "success", "message": "告警已加入处理队列"}, 200
    except Exception as e:
        logger.error(f"处理webhook请求失败: {str(e)}")
        return {"status": "error", "message": str(e)}, 500


if __name__ == '__main__':
    # 创建并启动处理线程
    processor = AlertProcessor(alert_queue)
    processor.daemon = True
    processor.start()

    try:
        conf = local_config()
        svr_host = conf.default_config()['Server']['listen']
        svr_port = conf.default_config()['Server']['port']
        # 启动 Flask 服务
        app.run(host=svr_host, port=svr_port)
    finally:
        # 确保程序退出时能够正常关闭处理线程
        processing_thread_active = False
        processor.join()
