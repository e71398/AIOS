#!/usr/bin/env python3
"""Telegram Bot入口 — 轮询接收Telegram消息，转发到AIOS调度器"""
import sys, os, json, time, urllib.request, urllib.error
from pathlib import Path

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))
from aios_bus import call_pin, init_registry, publish_event, generate_task_id, register_callback, check_emergency_mode
from aios_enforcer import protocol_check

VERSION = "1.1.0"
POLL_INTERVAL = 2
MAX_MSG_LEN = 1000
CONFIG_FILE = Path("${AIOS_HOME}/config/telegram.json")


class TelegramBot:
    def __init__(self, token: str = ""):
        loaded = token or self._load_token_from_config() or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        # [SECURITY] 校验 token 格式. Telegram bot token 格式固定:
        #   <bot_id>:<47字符 base64-ish>; 总长度 35+35 共 ~45 字符.
        # 任何含 shell metachar 的"token"直接拒绝, 防 path injection.
        if loaded and (
            ":" not in loaded or any(c in loaded for c in "\n\r\t ;|`$()<>")
        ):
            print(f"⚠️  Telegram token 含拒绝字符, 已忽略", file=sys.stderr)
            loaded = ""
        self.token = loaded
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self._load_offset()

    @staticmethod
    def _load_token_from_config() -> str:
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text())
                return cfg.get("bot_token", "")
            except Exception:
                return ""
        return ""

    def _offset_path(self):
        return Path("/tmp/aios_telegram_offset.txt")

    def _load_offset(self):
        p = self._offset_path()
        if p.exists():
            self.last_update_id = int(p.read_text().strip())

    def _save_offset(self):
        self._offset_path().write_text(str(self.last_update_id))

    def _api_call(self, method: str, data: dict = None) -> dict | None:
        url = f"{self.api_base}/{method}"
        body = json.dumps(data or {}).encode() if data else None
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"} if body else {},
                                     method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"  ⚠️ telegram api error ({method}): {e}")
            return None

    def get_updates(self) -> list:
        resp = self._api_call("getUpdates", {
            "offset": self.last_update_id + 1,
            "timeout": 10,
            "allowed_updates": ["message"],
        })
        return resp.get("result", []) if resp else []

    def send_message(self, chat_id: int, text: str):
        for chunk in [text[i:i+MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]:
            self._api_call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                            "parse_mode": "Markdown"})

    def handle_message(self, msg: dict):
        chat_id = msg.get("chat", {}).get("id", 0)
        text = msg.get("text", "").strip()
        sender = msg.get("from", {}).get("id", chat_id)

        if not text:
            return

        init_registry()
        ok, reason = protocol_check(text, str(sender))
        if not ok:
            self.send_message(chat_id, f"⛔ 协议拒绝: {reason}")
            return

        # 紧急入口检查: OpenClaw 宕机时拒绝新任务
        emergency = check_emergency_mode()
        if emergency.get("emergency"):
            msg_text = emergency.get("message", "OpenClaw不可用")
            self.send_message(chat_id, f"🚨 {msg_text}\n💡 请通过 OpenCode CLI 直接操作")
            print(f"  🚨 telegram emergency mode: {msg_text}")
            return

        ok, result = call_pin("openclaw.dispatch", text, source="telegram", sender_id=str(sender))
        task_ids = result if ok else []

        if task_ids:
            publish_event("task.created", {"count": len(task_ids), "source": "telegram",
                                           "sender": str(sender), "task_ids": task_ids}, "telegram_entry")
            for tid in task_ids:
                register_callback(tid, source="telegram", reply_key=str(chat_id),
                                  sender_id=str(sender))
            self.send_message(chat_id, f"✅ 任务已提交 ({len(task_ids)}个子任务)")
            print(f"  ✅ telegram -> dispatch: {text[:50]}... ({len(task_ids)} tasks)")
        else:
            self.send_message(chat_id, f"❌ 提交失败: {result}")

    def poll_loop(self):
        print(f"\n{'='*50}")
        print(f"  AIOS Telegram Entry v{VERSION}")
        print(f"  Mode: polling (interval={POLL_INTERVAL}s)")
        print(f"  Bot: @{self._bot_username()}")
        print(f"{'='*50}\n")
        while True:
            try:
                updates = self.get_updates()
                for upd in updates:
                    self.last_update_id = upd["update_id"]
                    msg = upd.get("message", {})
                    self.handle_message(msg)
                self._save_offset()
            except KeyboardInterrupt:
                print("\nShutting down...")
                break
            except Exception as e:
                print(f"  ⚠️ poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def _bot_username(self) -> str:
        info = self._api_call("getMe")
        return info.get("result", {}).get("username", "?") if info else "?"


if __name__ == "__main__":
    bot = TelegramBot(token=sys.argv[1] if len(sys.argv) > 1 else "")
    if not bot.token:
        print(f"\n{'='*50}")
        print(f"  AIOS Telegram Entry v{VERSION}")
        print(f"  ⏸  Telegram Bot Token 未配置，等待中...")
        print(f"  {'='*50}")
        print(f"  配置方式:")
        print(f"    1. 在 @BotFather 创建 Bot, 获取 token")
        print(f"    2. 写入 {CONFIG_FILE}:")
        print(f"       {{\"bot_token\": \"你的token\"}}")
        print(f"    3. 或设置环境变量 TELEGRAM_BOT_TOKEN")
        print(f"    4. 或重启本服务")
        print(f"  {'='*50}\n")
        # 不退出，等待 token 配置
        while not bot.token:
            time.sleep(60)
            bot = TelegramBot()
        # token 配好了，继续启动
    bot.poll_loop()


try:
    from aios_bus import register_pin
    register_pin("telegram.start", lambda: TelegramBot().poll_loop(), "电报入口: 启动轮询Bot")
except Exception:
    pass
