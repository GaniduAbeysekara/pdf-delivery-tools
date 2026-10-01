"""Document layer of the engine: file-type detection and per-format metadata (page count, language, date)."""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from ..language_detector import detect_language
from ..pdf_analyzer import PdfInfo, inspect_pdf
from ..utils import clean_text

DIRECT_TYPES = ("PDF", "DOC", "DOCX")


def human_size(n: int | None) -> str:
    if n is None:
        return "Unknown"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def parse_http_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def detect_content_type(mime: str, url: str, head: bytes) -> str:
    """PDF / DOC / DOCX / HTML / Other, from the server's MIME type first, then the file signature."""
    ct = (mime or "").split(";")[0].strip().lower()
    path = urlparse(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    if "pdf" in ct:
        return "PDF"
    if ct == "application/msword":
        return "DOC"
    if "wordprocessingml" in ct:
        return "DOCX"
    if ct in ("text/html", "application/xhtml+xml"):
        return "HTML"
    generic = ct in ("", "application/octet-stream", "binary/octet-stream", "application/force-download",
                     "application/download", "application/x-download")
    if head.startswith(b"%PDF") and (generic or ct.startswith("text/plain")):
        return "PDF"
    if generic:
        if head.startswith(b"PK\x03\x04") and ext == "docx":
            return "DOCX"
        if head.startswith(b"\xd0\xcf\x11\xe0") and ext == "doc":
            return "DOC"
        if re.match(rb"\s*(<!doctype html|<html)", head[:512], re.I):
            return "HTML"
    return "Other"


def inspect_docx(data: bytes) -> PdfInfo:
    """Page count (from docProps/app.xml, if the editor recorded it) and language of a .docx."""
    info = PdfInfo()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "docProps/app.xml" in names:
                m = re.search(rb"<Pages>(\d+)</Pages>", zf.open("docProps/app.xml").read(100_000))
                info.pages = int(m.group(1)) if m else None
            if "word/document.xml" in names:
                xml = zf.open("word/document.xml").read(5_000_000).decode("utf-8", "ignore")
                text = clean_text(re.sub(r"<[^>]+>", " ", xml))
                info.language = detect_language(text) if text.strip() else "Unknown"
                info.text_status = "OK" if text.strip() else "NO_TEXT"
    except Exception as e:
        info.problem = f"Unreadable DOCX: {str(e)[:80]}"
    return info


def inspect_document(content_type: str, data: bytes) -> PdfInfo:
    """Format-specific metadata. Only PDF and DOCX can be opened; others return an empty result."""
    if content_type == "PDF":
        return inspect_pdf(data)
    if content_type == "DOCX":
        return inspect_docx(data)
    return PdfInfo()
