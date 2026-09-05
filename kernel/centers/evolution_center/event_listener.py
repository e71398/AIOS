"""Hermes Event Listener — 监听Event Bus，主动触发监督"""
import time, json
from hermes_api import HermesAPI

class HermesEventListener:
    def __init__(self):
        self.api = HermesAPI()

    def start(self):
        try:
            import redis
            r = redis.Redis(host='localhost', port=6379, socket_connect_timeout=2)
            ps = r.pubsub()
            ps.subscribe("aios:event:task_completed", "aios:event:violation_detected")
            print("[Hermes] 开始监听 Event Bus...")
            for msg in ps.listen():
                if msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"])
                        channel = msg["channel"].decode() if isinstance(msg["channel"], bytes) else msg["channel"]
                        if "task_completed" in channel:
                            self.api.task_completed(**{k: data.get(k, "unknown") for k in ["task_id","agent_id"]})
                        elif "violation" in channel:
                            self.api.violation(**{k: data.get(k, "") for k in ["violation_type","agent_id","task_id"]})
                    except Exception as e:
                        print(f"[Hermes] 事件处理错误: {e}")
        except Exception as e:
            print(f"[Hermes] Redis连接失败: {e}")
