"""PDF metadata extraction — page count, last modified, language (used by the shared analysis engine)."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from .language_detector import detect_language
from .logger import log
from .utils import clean_text


def _parse_pdf_date(raw: str) -> str:
    """Convert PDF date string D:YYYYMMDDHHmmSS to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip().lstrip("D:").replace("'", "")
    m = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return raw[:10] if len(raw) >= 10 else raw


@dataclass
class PdfInfo:
    """Metadata of an opened document (also used for DOCX)."""
    pages: int | None = None
    language: str = "Unknown"
    modified: str = ""
    problem: str = ""     # why the document could not be fully read (empty when fine)
    text_status: str = ""  # OK / NO_TEXT / ERROR


def inspect_pdf(data: bytes) -> PdfInfo:
    info = PdfInfo()
    if not data.startswith(b"%PDF"):
        info.problem = "Invalid PDF (file signature not found)"
        return info
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                info.problem = "Protected document (password required)"
                return info
        info.pages = len(reader.pages)
        try:
            meta = reader.metadata
            raw = (meta.get("/ModDate") or meta.get("/CreationDate") or "") if meta else ""
            info.modified = _parse_pdf_date(str(raw)) if raw else ""
        except Exception as e:
            log.debug("PDF metadata error: %s", e)
        try:
            text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(5, info.pages)))
            text = clean_text(text)
            info.language = detect_language(text) if text.strip() else "Unknown"
            info.text_status = "OK" if text.strip() else "NO_TEXT"
        except Exception as e:
            log.debug("PDF text extraction error: %s", e)
            info.language, info.text_status = "Unknown", "ERROR"
    except Exception as e:
        info.problem = f"Invalid PDF: {str(e)[:100]}"
        log.debug("PDF parse error: %s", e)
    return info
