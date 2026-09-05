"""
Dedup Engine — 三层去重: URL + 标题相似 + 内容指纹
"""
import hashlib, re

class DedupEngine:
    def __init__(self):
        self.url_set = set()
        self.title_fp_set = set()
        self.content_fp_set = set()

    def _url_key(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _title_fp(self, title: str) -> str:
        t = re.sub(r'[^a-zA-Z\u4e00-\u9fff0-9]', '', title.lower())[:50]
        return hashlib.md5(t.encode()).hexdigest()[:10]

    def _content_fp(self, text: str) -> str:
        t = re.sub(r'\s+', '', text.lower())[:100]
        return hashlib.md5(t.encode()).hexdigest()[:10]

    def load_existing(self, items: list):
        """从已有数据加载去重集合."""
        for i in items:
            u = i.get('url','')
            t = i.get('title','')
            s = i.get('summary','')
            if u: self.url_set.add(self._url_key(u))
            if t: self.title_fp_set.add(self._title_fp(t))
            if s: self.content_fp_set.add(self._content_fp(s))

    def is_duplicate(self, url: str = "", title: str = "", summary: str = "") -> bool:
        if url and self._url_key(url) in self.url_set: return True
        if title and self._title_fp(title) in self.title_fp_set: return True
        if summary and self._content_fp(summary) in self.content_fp_set: return True
        return False

    def mark_seen(self, url: str = "", title: str = "", summary: str = ""):
        if url: self.url_set.add(self._url_key(url))
        if title: self.title_fp_set.add(self._title_fp(title))
        if summary: self.content_fp_set.add(self._content_fp(summary))

    def stats(self) -> dict:
        return {"url_dedup": len(self.url_set), "title_dedup": len(self.title_fp_set), "content_dedup": len(self.content_fp_set)}
