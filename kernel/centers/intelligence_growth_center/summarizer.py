"""AI摘要生成器"""
class Summarizer:
    def summarize(self, item: dict) -> dict:
        title = item.get("title","")
        summary = item.get("summary","")
        score = float(item.get("score",0))
        category = item.get("category","")
        source = item.get("source","")

        key_points = []
        if "agent" in (title+summary).lower(): key_points.append("AI Agent相关")
        if "framework" in (title+summary).lower(): key_points.append("框架/工具")
        if "api" in (title+summary).lower(): key_points.append("提供API接口")
        if "open source" in (title+summary).lower(): key_points.append("开源项目")
        if not key_points: key_points.append("技术内容")

        worth = "推荐关注" if score >= 5 else ("可参考" if score >= 3 else "低优先级")
        action = "集成评估" if score >= 6 else ("试用观察" if score >= 4 else ("归档" if score < 2 else "跟踪"))

        return {
            "key_points": key_points,
            "worth": worth,
            "action": action,
            "summary_text": summary[:200] if summary else title[:200]
        }
