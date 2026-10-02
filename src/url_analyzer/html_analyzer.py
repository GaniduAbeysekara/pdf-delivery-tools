"""HTML landing page analyzer — finds document links."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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


# A <button> (or any element) that opens a file usually carries the address in one of these, or in its click handler.
_BUTTON_ATTRS = ("data-href", "data-url", "data-file", "data-download", "data-pdf", "data-link", "formaction")
_ONCLICK_URL = re.compile(r"""(?:window\.open|location(?:\.href)?\s*=|location\.assign|location\.replace|open)\s*\(?\s*['"]([^'"]+)['"]""", re.I)


def _button_targets(soup) -> list[tuple[object, str]]:
    """(element, raw address) for buttons and other non-<a> elements that open a file."""
    out = []
    for el in soup.find_all(["button", "div", "span", "input", "li", "img"]):
        for attr in _BUTTON_ATTRS:
            val = (el.get(attr) or "").strip()
            if val:
                out.append((el, val))
        m = _ONCLICK_URL.search(el.get("onclick", "") or "")
        if m:
            out.append((el, m.group(1).strip()))
    return out


def _extract_links_from_html(html: str, base_url: str, include_frames: bool = True) -> list[DocumentLink]:
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

    for el, raw in _button_targets(soup):
        add_link(raw, el.get_text(" ", strip=True), "button")

    # <iframe src>, <embed src>, <object data>  (the engine reads frames itself, with viewer unwrapping, when include_frames=False)
    if include_frames:
        for attr_tag, attr in [("iframe", "src"), ("embed", "src"), ("object", "data")]:
            for tag in soup.find_all(attr_tag):
                src = tag.get(attr, "").strip()
                add_link(src, tag.get("title", ""), attr_tag)

    return links


def find_document_links(html: str, base_url: str, include_frames: bool = True) -> list[DocumentLink]:
    """
    Extract and deduplicate all document links from an HTML page.
    Returns list of DocumentLink objects with extension-confirmed types.
    """
    links = _extract_links_from_html(html, base_url, include_frames)
    log.info("Found %d document link(s) on %s", len(links), base_url)
    return links


# ── Links WITHOUT a document extension that might still deliver one ──────────────────────────────────
# Many sites link to a PDF as /notice/123/pdf, /download?id=7, ?format=pdf ... A link is only a *candidate* here:
# the engine verifies each one by what the server really returns (Content-Type / Content-Disposition) before
# counting it, so a weak hint can cost one request but can never create a false "document".
_DOC_MIME = re.compile(r"application/(?:pdf|x-pdf|msword|vnd\.openxmlformats-officedocument\.wordprocessingml\.document)", re.I)
_WORDS = re.compile(r"\b(?:download|pdf|attachment|full[\s-]?text|get file|view document|open document|printable|print version)\b", re.I)
_CLASS_HINT = re.compile(r"(?:^|[\s_-])(?:pdf|download|attachment|fulltext)(?:$|[\s_-])", re.I)
_LAST_SEGMENT = {"pdf", "download", "downloads", "file", "attachment", "document", "print", "fulltext", "full-text", "export"}
_QUERY_FORMAT = re.compile(r"(?:^|[&;])(?:format|type|output|export|ext|as|download)=(?:pdf|docx?|true|1|yes)(?:$|[&;])", re.I)
_PATH_QUERY_HINT = re.compile(r"(?:download|attachment|getfile|get_file|fileid|file_id|docid|document_id|/files?/|\.ashx|/api/.*(?:file|document|pdf))", re.I)
_NOT_DOCUMENT_EXT = {".html", ".htm", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico", ".zip",
                     ".mp4", ".mp3", ".xml", ".json", ".txt", ".rss"}


def _hint_text(tag) -> str:
    """Everything a person would read or see on the link: text, labels, and the alt text / names of icons inside it."""
    parts = [tag.get_text(" ", strip=True), tag.get("aria-label", ""), tag.get("title", "")]
    for inner in tag.find_all(True):
        parts += [inner.get("alt", ""), inner.get("title", ""), inner.get("aria-label", "")]
        src = inner.get("src", "") or ""
        if inner.name == "img" and src:
            parts.append(src.rsplit("/", 1)[-1])
    return " ".join(str(p) for p in parts if p)


def _hint_classes(tag) -> str:
    names = list(tag.get("class", [])) + [str(tag.get("id", ""))]
    for inner in tag.find_all(True):
        names += list(inner.get("class", []))
    return " ".join(names)


def _score_anchor(tag, full: str) -> int:
    """How strongly an <a> looks like a document download (0 = not at all). Independent signals add up."""
    from .utils import url_extension
    parsed = urlparse(full)
    score = 0
    if _DOC_MIME.search(tag.get("type", "") or ""):
        score += 4                                      # the page itself says the target is a document
    if tag.has_attr("download"):
        score += 3                                      # <a download>
    if _CLASS_HINT.search(_hint_classes(tag)):
        score += 2                                      # class="pdf", "btn-download", "fa-file-pdf" ...
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
    if last in _LAST_SEGMENT:
        score += 2                                      # .../pdf  .../download
    if _QUERY_FORMAT.search(parsed.query):
        score += 2                                      # ?format=pdf
    if _PATH_QUERY_HINT.search(parsed.path + "?" + parsed.query):
        score += 1
    if _WORDS.search(_hint_text(tag)):
        score += 1                                      # "View PDF", "Download", icon alt text
    return score


def find_candidate_links(html: str, base_url: str, known: set[str] | None = None, limit: int = 20,
                         include_frames: bool = True) -> list[str]:
    """Unique http(s) URLs, best candidates first, that may deliver a document but have no recognised extension."""
    from .utils import url_extension
    known = known or set()
    soup = BeautifulSoup(html, "html.parser")
    base_norm = normalize_url(base_url)
    found: dict[str, int] = {}
    order: list[str] = []

    def add(raw: str, score: int) -> None:
        raw = (raw or "").strip()
        if score <= 0 or not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            return
        full = normalize_url(raw, base_url)
        if not full or not full.lower().startswith(("http://", "https://")) or full == base_norm or full in known:
            return
        ext = url_extension(full)
        if ext in SUPPORTED_EXTENSIONS or ext in _NOT_DOCUMENT_EXT:
            return                                       # already counted by extension, or clearly not a document
        if full not in found:
            order.append(full)
        found[full] = max(found.get(full, 0), score)

    # machine-readable declarations of "the PDF version of this page" (repositories, journals, gazettes...)
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"^(?:citation_pdf_url|dc\.identifier\.pdf)$", re.I)}):
        add(meta.get("content", ""), 6)
    for link in soup.find_all("link", href=True):
        rel = link.get("rel", [])
        rel = rel if isinstance(rel, list) else [rel]
        if "alternate" in [r.lower() for r in rel] and _DOC_MIME.search(link.get("type", "") or ""):
            add(link["href"], 6)
    if include_frames:
        for el in soup.find_all(["object", "embed", "iframe"]):
            if _DOC_MIME.search(el.get("type", "") or ""):
                add(el.get("data") or el.get("src") or "", 5)

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            add(href, _score_anchor(tag, normalize_url(href, base_url) or ""))
    for el, raw in _button_targets(soup):                      # a button whose click opens an address is a strong hint
        add(raw, _score_anchor(el, normalize_url(raw, base_url) or "") + 2)

    ranked = sorted(order, key=lambda u: (-found[u], order.index(u)))      # strongest first, then page order
    return ranked[:limit]


# ── Documents shown inside the page: <iframe>, <embed>, <object> ─────────────────────────────────────
# A page often shows its PDF in a frame: directly (src=".../a.pdf"), through a viewer (".../viewer.html?file=a.pdf",
# Google / Office viewers), through an address with no extension that still serves a PDF, or through another HTML
# page that holds the PDF. This only *finds and unwraps* the frame addresses; the engine fetches them and decides
# what they really are.
_VIEWER_PATH = re.compile(r"viewer|pdf\.?js|gview|embed\.aspx|/op/", re.I)
_DOC_ADDRESS = re.compile(r"\.(?:pdf|docx?)$", re.I)
_VIEWER_PARAMS = ("file", "url", "src", "doc", "document", "pdf", "source", "u")
_SKIP_FRAME_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be", "vimeo.com", "facebook.com", "twitter.com", "x.com",
                     "linkedin.com", "instagram.com", "doubleclick.net", "googletagmanager.com", "google-analytics.com",
                     "googlesyndication.com", "recaptcha.net", "maps.google.com", "disqus.com", "addthis.com")
_DRIVE_FILE = re.compile(r"^/file/d/([\w-]+)", re.I)


def unwrap_viewer(url: str) -> str:
    """The document address inside a viewer URL, or the URL unchanged when it is not a viewer."""
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if host.endswith("drive.google.com"):
            m = _DRIVE_FILE.match(p.path)
            if m:
                return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        known_viewer = bool(_VIEWER_PATH.search(p.path)) or host.endswith(("docs.google.com", "officeapps.live.com"))
        q = parse_qs(p.query)
        for key in _VIEWER_PARAMS:
            for val in q.get(key, []):
                val = unquote(val).strip()
                if not val or val.startswith(("javascript:", "data:", "blob:")):
                    continue
                # A viewer can sit at any path (e.g. a PDF.js copy at /web/?file=...pdf), so a parameter that carries
                # a document address is enough, whatever the path is called.
                if known_viewer or _DOC_ADDRESS.search(val.split("#")[0].split("?")[0]):
                    inner = urljoin(url, val)
                    if inner.lower().startswith(("http://", "https://")):
                        return inner
    except ValueError:
        pass
    return url


def find_embedded_documents(html: str, base_url: str, limit: int = 8) -> list[dict]:
    """Frames that may hold a document: [{"tag", "src", "target"}] in page order, viewer wrappers unwrapped.

    Tracking pixels, hidden frames, video / social / ad frames and 'about:blank' are ignored."""
    soup = BeautifulSoup(html, "html.parser")
    base_norm = normalize_url(base_url)
    out, seen = [], set()
    for el in soup.find_all(["iframe", "frame", "embed", "object"]):
        raw = ((el.get("data") if el.name == "object" else el.get("src")) or "").strip()
        if not raw or raw.startswith(("javascript:", "about:", "data:", "blob:", "#", "mailto:")):
            continue
        dims = [str(el.get(a, "")).strip().lower().removesuffix("px") for a in ("width", "height")]
        style = str(el.get("style", "")).replace(" ", "").lower()
        if any(d in ("0", "1") for d in dims) or "display:none" in style or el.has_attr("hidden"):
            continue
        src = normalize_url(raw, base_url)
        if not src or not src.lower().startswith(("http://", "https://")):
            continue
        host = urlparse(src).netloc.lower().split(":")[0]
        if any(host == h or host.endswith("." + h) for h in _SKIP_FRAME_HOSTS):
            continue
        target = normalize_url(unwrap_viewer(src))
        if target == base_norm or target in seen:
            continue
        seen.add(target)
        out.append({"tag": el.name, "src": src, "target": target})
    return out[:limit]


def document_base_url(html: str, page_url: str) -> str:
    """The URL relative links resolve against: the page URL, unless the HTML declares <base href>."""
    try:
        tag = BeautifulSoup(html[:200_000], "html.parser").find("base", href=True)
        return urljoin(page_url, tag["href"].strip()) if tag else page_url
    except Exception:
        return page_url


# ── Bot-protection / "verify you are human" pages ────────────────────────────────────────────────────
# Many sites answer automated clients with HTTP 200 and a JavaScript challenge page instead of the real content.
# That page has no document links, which must NOT be reported as "this page has no documents".
_BOT_PROTECTION = [
    ("Radware", re.compile(r"<title>\s*radware|__uzdbm_|perfdrive\.com|shieldsquare", re.I)),
    ("Cloudflare", re.compile(r"<title>\s*(?:just a moment|attention required)|cf-browser-verification|challenge-platform|cf_chl_", re.I)),
    ("Imperva/Incapsula", re.compile(r"_incapsula_resource|incapsula incident id", re.I)),
    ("Akamai", re.compile(r"errors\.edgesuite\.net|akamai.{0,40}reference\s*#", re.I | re.S)),
    ("DataDome", re.compile(r"captcha-delivery\.com|datadome", re.I)),
    ("PerimeterX", re.compile(r"px-captcha|perimeterx|_pxhc", re.I)),
    ("Sucuri", re.compile(r"sucuri website firewall", re.I)),
    ("a browser check", re.compile(r"checking your browser before accessing|enable javascript and cookies to continue|"
                                    r"verify (?:that )?you are (?:a )?human|are you a robot", re.I)),
]


def detect_bot_protection(html: str) -> str:
    """Name of the bot-protection service whose challenge page this HTML looks like, or '' for a normal page."""
    head = (html or "")[:200_000]
    for vendor, pattern in _BOT_PROTECTION:
        if pattern.search(head):
            return vendor
    return ""
