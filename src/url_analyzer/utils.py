"""URL normalization and general utilities."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse, unquote


DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx"}

PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "binary/octet-stream",   # some servers serve PDFs this way
}

DOC_CONTENT_TYPES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def normalize_url(url: str, base: str = "") -> str:
    """Normalize a URL: resolve relative refs, strip fragments, decode percent-encoding."""
    url = url.strip()
    if not url:
        return ""

    # Resolve relative URLs
    if base:
        url = urljoin(base, url)

    parsed = urlparse(url)

    # Drop fragment
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        "",   # no fragment
    ))
    return normalized


def url_extension(url: str) -> str:
    """Return lowercase file extension from URL path, e.g. '.pdf'."""
    path = urlparse(url).path
    # Strip query / trailing slash
    path = path.rstrip("/")
    dot = path.rfind(".")
    if dot == -1:
        return ""
    ext = path[dot:].lower()
    # Only return if looks like a real extension (no slashes after dot)
    if "/" in ext:
        return ""
    # Strip any extra chars after extension (e.g. %20)
    ext = re.split(r"[^a-z0-9]", ext)[0]
    return ext


def is_document_extension(url: str, extensions: list[str] | None = None) -> bool:
    ext_set = set(extensions) if extensions else DOCUMENT_EXTENSIONS
    return url_extension(url) in ext_set


def content_type_to_doc_type(content_type: str) -> str:
    """Return 'PDF', 'DOC', 'DOCX', or '' based on content-type."""
    ct = content_type.lower().split(";")[0].strip()
    if ct in PDF_CONTENT_TYPES or "pdf" in ct:
        return "PDF"
    if ct == "application/msword":
        return "DOC"
    if "wordprocessingml" in ct or ct in DOC_CONTENT_TYPES:
        return "DOCX"
    return ""


def clean_text(text: str, max_chars: int = 2000) -> str:
    """Clean and truncate text for language detection."""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def safe_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
