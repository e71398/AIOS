"""Opportunity Radar — 机会发现 + 风控过滤 + Hermes飞书通知."""
import sys, os, re, json, urllib.request, urllib.error
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from intel_pipeline import run_pipeline

OPPORTUNITY_SOURCES = ["devto_startups", "producthunt"]

def _strip_html(text: str) -> str:
    """去除HTML标签, 提取纯文本"""
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()[:200]

def scan() -> dict:
    result = run_pipeline(OPPORTUNITY_SOURCES, module="opportunity")
    new_items = result.pop("_new_items", [])
    notification = _push_high_score_opportunities(new_items) if new_items else {
        "attempted": False, "sent": False, "reason": "no_new_items"}
    result["notification"] = notification
    return result

def _load_hermes_feishu_config() -> dict:
    """Read only the three notification values from Hermes' own environment."""
    keys = {"FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_HOME_CHANNEL"}
    values = {key: os.environ.get(key, "") for key in keys}
    env_file = Path.home() / ".hermes/.env"
    if env_file.is_file():
        for line in env_file.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            key, value = line.split("=", 1)
            if key.strip() in keys and not values.get(key.strip()):
                values[key.strip()] = value.strip().strip("'\"")
    return values

def _send_hermes_feishu(items: list, test_message: str = "") -> dict:
    cfg = _load_hermes_feishu_config()
    missing = [key for key, value in cfg.items() if not value]
    if missing:
        return {"attempted": False, "sent": False,
                "reason": "missing_hermes_config", "missing": missing}
    auth_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": cfg["FEISHU_APP_ID"],
                         "app_secret": cfg["FEISHU_APP_SECRET"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(auth_req, timeout=10) as response:
            auth = json.loads(response.read())
        token = auth.get("tenant_access_token", "")
        if not token: raise RuntimeError(auth.get("msg", "Hermes Feishu auth failed"))
        lines = [test_message] if test_message else ["🛰 **AIOS 机会发现**"]
        for item in items[:5]:
            summary = _strip_html(item.get("summary", ""))
            lines.append(f"🔥 [{item.get('score', 0)}分] **{item['title']}**")
            if summary: lines.append(summary[:100])
            if item.get("url"): lines.append(f"[查看]({item['url']})")
        channel = cfg["FEISHU_HOME_CHANNEL"]
        receive_type = "open_id" if channel.startswith("ou_") else "chat_id"
        send_req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            data=json.dumps({"receive_id": channel, "msg_type": "interactive",
                             "content": json.dumps({"config": {"wide_screen_mode": True},
                                "elements": [{"tag": "markdown", "content": "\n".join(lines)}]},
                                ensure_ascii=False)}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(send_req, timeout=10) as response:
            result = json.loads(response.read())
        return {"attempted": True, "sent": result.get("code") == 0,
                "reason": result.get("msg", "")}
    except Exception as exc:
        return {"attempted": True, "sent": False,
                "reason": f"{type(exc).__name__}: {exc}"[:300]}

def _push_high_score_opportunities(items: list, threshold: float = 2.0) -> dict:
    """Publish new high-score opportunities and notify through Hermes Feishu."""
    items = [item for item in items if float(item.get("score", 0)) >= threshold]
    if not items:
        return {"attempted": False, "sent": False, "reason": "no_high_score_items"}

    # 格式化并推送到AIOS总线
    try:
        AIOS_TOOLS = "${AIOS_HOME}/kernel/tools"
        sys.path.insert(0, AIOS_TOOLS)
        from aios_bus import publish_event
        for item in items:
            summary_clean = _strip_html(item.get("summary",""))
            publish_event("opportunity.discovered", {
                "title": item["title"],
                "url": item.get("url",""),
                "summary": summary_clean,
                "score": item.get("score", 0),
                "source": item.get("source",""),
            }, "opportunity_radar")
    except Exception as exc:
        print(f"opportunity event publish failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return _send_hermes_feishu(items)

def send_test_notification() -> dict:
    return _send_hermes_feishu([], "✅ AIOS 智能与成长中心：Hermes 机会通知通道验收成功")

def get_opportunities(limit: int = 15) -> list:
    from storage import get_items
    return get_items(module="opportunity", limit=limit, min_score=2)
