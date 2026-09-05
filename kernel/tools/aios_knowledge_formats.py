#!/usr/bin/env python3
"""AIOS Knowledge Format Extractors — additional format support.

Closure for P1-KNW-001 (knowledge ingestion weak — only text/code formats).

Provides:
  * extract_pdf(path)         — pypdf-backed PDF text extraction
  * extract_docx(path)        — python-docx-backed DOCX text extraction
  * extract_xlsx(path)        — openpyxl-backed XLSX text extraction
  * extract_html(path)        — BeautifulSoup-lite HTML-to-text

Each extractor returns ``{"title": str, "content": str, "meta": dict, "format": str}``
or ``None`` when the file is unsupported / unreadable.  All extractors
preserve file-level provenance (``source_path``, ``mtime_ns``, ``size_bytes``,
``sha256``) so a downstream caller can trace any indexed chunk back to the
original artefact and delete it on retraction.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _provenance(path: Path) -> Dict[str, Any]:
    """Compute file-level provenance that all extractors share."""
    try:
        st = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "source_path": str(path),
            "mtime_ns": int(st.st_mtime_ns),
            "size_bytes": int(st.st_size),
            "sha256": digest.hexdigest(),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return {"source_path": str(path), "error": "stat_failed"}


def extract_pdf(path: Path) -> Optional[Dict[str, Any]]:
    """Extract text from a PDF file via pypdf."""
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                parts.append(f"--- Page {i + 1} ---\n{text}")
        if not parts:
            return None
        content = "\n\n".join(parts)[:8000]
        meta = _provenance(path)
        meta.update({
            "format": "pdf",
            "pages": len(reader.pages),
            "title": (reader.metadata.title if reader.metadata else None) or path.stem,
        })
        return {
            "title": meta.get("title") or path.stem,
            "content": content,
            "meta": meta,
            "format": "pdf",
        }
    except Exception as exc:
        return {"title": path.stem, "content": "", "meta": _provenance(path) | {"error": str(exc)[:200]}, "format": "pdf"}


def extract_docx(path: Path) -> Optional[Dict[str, Any]]:
    """Extract text from a DOCX file via python-docx."""
    try:
        import docx
    except Exception:
        return None
    try:
        document = docx.Document(str(path))
        parts = []
        for para in document.paragraphs:
            text = (para.text or "").strip()
            if text:
                parts.append(text)
        # Also pull text from tables
        for tbl in document.tables:
            for row in tbl.rows:
                for cell in row.cells:
                    text = (cell.text or "").strip()
                    if text:
                        parts.append(text)
        if not parts:
            return None
        content = "\n".join(parts)[:8000]
        meta = _provenance(path)
        meta.update({
            "format": "docx",
            "paragraphs": len(document.paragraphs),
            "tables": len(document.tables),
        })
        return {
            "title": (document.core_properties.title if document.core_properties else None) or path.stem,
            "content": content,
            "meta": meta,
            "format": "docx",
        }
    except Exception as exc:
        return {"title": path.stem, "content": "", "meta": _provenance(path) | {"error": str(exc)[:200]}, "format": "docx"}


def extract_xlsx(path: Path) -> Optional[Dict[str, Any]]:
    """Extract cell text from an XLSX file via openpyxl."""
    try:
        from openpyxl import load_workbook
    except Exception:
        return None
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_rows = []
            for row in ws.iter_rows(values_only=True, max_row=200):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    sheet_rows.append(" | ".join(cells))
            if sheet_rows:
                parts.append(f"--- Sheet: {sheet_name} ---\n" + "\n".join(sheet_rows))
        wb.close()
        if not parts:
            return None
        content = "\n\n".join(parts)[:8000]
        meta = _provenance(path)
        meta.update({"format": "xlsx", "sheets": len(wb.sheetnames)})
        return {
            "title": path.stem,
            "content": content,
            "meta": meta,
            "format": "xlsx",
        }
    except Exception as exc:
        return {"title": path.stem, "content": "", "meta": _provenance(path) | {"error": str(exc)[:200]}, "format": "xlsx"}


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_BLANK_RE = re.compile(r"\s+")


def extract_html(path: Path) -> Optional[Dict[str, Any]]:
    """Extract text from an HTML file (regex-based, no external deps)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    # Drop scripts/styles
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.DOTALL | re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else path.stem
    # Replace block-level tags with newlines, then strip all other tags
    text = re.sub(r"<(p|div|br|h[1-6]|li|tr|td|th|article|section)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_BLANK_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) < 20:
        return None
    content = text[:8000]
    meta = _provenance(path)
    meta.update({"format": "html"})
    return {"title": title, "content": content, "meta": meta, "format": "html"}


# Format dispatch table — single source of truth
FORMAT_EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".xlsx": extract_xlsx,
    ".html": extract_html,
    ".htm": extract_html,
}


def extract_any(path: Path) -> Optional[Dict[str, Any]]:
    """Dispatch by extension.  Returns ``None`` for unsupported formats."""
    fn = FORMAT_EXTRACTORS.get(path.suffix.lower())
    if fn is None:
        return None
    return fn(path)


__all__ = [
    "extract_pdf",
    "extract_docx",
    "extract_xlsx",
    "extract_html",
    "extract_any",
    "FORMAT_EXTRACTORS",
]
