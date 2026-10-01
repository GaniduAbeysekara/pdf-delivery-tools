"""HTML landing page analyzer — finds document links."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .logger import log
from .models import DocumentLink
from .utils import normalize_url, is_document_extension, content_type_to_doc_type

# Anchor text / URL keywords that suggest a document
DOCUMENT_KEYWORDS = re.compile(
    r"\b(download|document|pdf|report|publication|file|attachment|annex|circular|guideline|regulation|act|law|decree|order|notice|bulletin|manual)\b",
    re.IGNORECASE,
)

SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx"}


def _doc_type_from_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "PDF"
    if path.endswith(".docx"):
        return "DOCX"
    if path.endswith(".doc"):
        return "DOC"
    return ""


def _is_document_url(url: str) -> bool:
    return is_document_extension(url, list(SUPPORTED_EXTENSIONS))


def _extract_links_from_html(html: str, base_url: str) -> list[DocumentLink]:
    """Parse HTML and return all candidate document links (deduplicated)."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[DocumentLink] = []

    def add_link(raw_href: str, text: str = "", tag: str = "a") -> None:
        if not raw_href or raw_href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        full = normalize_url(raw_href, base_url)
        if not full or full in seen:
            return
        doc_type = _doc_type_from_ext(full)
        if doc_type:
            seen.add(full)
            links.append(DocumentLink(url=full, link_text=text.strip(), document_type=doc_type))

    # <a href>
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        text = tag.get_text(" ", strip=True)
        add_link(href, text, "a")

    # <iframe src>, <embed src>, <object data>
    for attr_tag, attr in [("iframe", "src"), ("embed", "src"), ("object", "data")]:
        for tag in soup.find_all(attr_tag):
            src = tag.get(attr, "").strip()
            add_link(src, tag.get("title", ""), attr_tag)

    return links


def find_document_links(html: str, base_url: str) -> list[DocumentLink]:
    """
    Extract and deduplicate all document links from an HTML page.
    Returns list of DocumentLink objects with extension-confirmed types.
    """
    links = _extract_links_from_html(html, base_url)
    log.info("Found %d document link(s) on %s", len(links), base_url)
    return links


# Links WITHOUT a document extension that might still deliver one (e.g. /download?id=123). The anchor text alone
# never makes a link a document - the engine verifies each candidate's real Content-Type before counting it.
_CANDIDATE_TEXT = re.compile(r"(download|pdf|attachment|full text|get file|view document)", re.I)
_CANDIDATE_URL = re.compile(r"(download|attachment|getfile|get_file|fileid|file_id|docid|document_id|/files?/|\.ashx|/api/.*(file|document|pdf))", re.I)
_NOT_DOCUMENT_EXT = {".html", ".htm", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico", ".zip",
                     ".mp4", ".mp3", ".xml", ".json", ".txt", ".rss"}


def find_candidate_links(html: str, base_url: str, known: set[str] | None = None, limit: int = 20) -> list[str]:
    """Unique http(s) links, in page order, that look like downloads but have no recognised document extension."""
    known = known or set()
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set(known)
    out: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = normalize_url(href, base_url)
        if not full or full in seen or not full.lower().startswith(("http://", "https://")):
            continue
        from .utils import url_extension
        ext = url_extension(full)
        if ext in SUPPORTED_EXTENSIONS or ext in _NOT_DOCUMENT_EXT:
            continue
        parsed = urlparse(full)
        if _CANDIDATE_TEXT.search(tag.get_text(" ", strip=True)) or _CANDIDATE_URL.search(parsed.path + "?" + parsed.query):
            seen.add(full)
            out.append(full)
            if len(out) >= limit:
                break
    return out
