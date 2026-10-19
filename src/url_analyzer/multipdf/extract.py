"""Find the document links in a (fully expanded) page: address, readable title and where in the page's outline it sits."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from ..analysis.inputs import document_key
from ..html_analyzer import _ONCLICK_URL, document_base_url, unwrap_viewer
from . import scope as scope_mod

DEFAULT_MAX = 5000
FILE_TYPES = {"pdf": (".pdf",), "word": (".doc", ".docx")}

# Attributes that can carry a file address, in the order they are tried. `href` of a link to a page comes AFTER
# data-pdf / data-file ..., so a tree node with href="/section/12" and data-pdf="/files/12.pdf" yields the PDF.
# data-mpf-url is written by the browser step for a link that has no address of its own (javascript:void(0) + onclick): it
# clicked the link and recorded where the page tried to go.
URL_ATTRS = ("data-mpf-url", "data-pdf", "data-file", "data-download", "data-href", "data-url", "data-link", "data-src", "href", "src", "data", "formaction")

# Controls that open a collapsed section. Used by the page analysis and (as the same CSS) by the browser step.
EXPANDERS = ('[aria-expanded="false"], details:not([open]) > summary, .ttoggle, [data-toggle="collapse"].collapsed, '
             '[data-bs-toggle="collapse"].collapsed, .accordion-button.collapsed, .jstree-closed > .jstree-ocl, '
             '.expandable > .hitarea, .tree-toggle')

_LINK_LIKE = ("a[href], area[href], iframe[src], embed[src], object[data], button[formaction], "
              "[data-pdf], [data-file], [data-download], [data-href], [data-url], [data-link], [onclick]")
_WORDS = re.compile(r"[^\W_]+")


@dataclass
class FoundPdf:
    url: str                    # absolute address
    title: str = ""             # link text (or its title / aria-label)
    path: str = ""              # outline above it, e.g. "Title 1: Government > Division 2 > Chapter 3"
    attr: str = ""              # which attribute the address came from ("href", "data-pdf", ...)
    node_id: str = ""           # data-id of the link, when the page has one
    source: str = ""            # the page the link was found on (set when several levels of pages are followed)


@dataclass
class Extraction:
    found: list[FoundPdf] = field(default_factory=list)
    duplicates: int = 0         # the same address seen again (kept once)
    skipped: int = 0            # matched the scope but carried no document address
    truncated: bool = False     # stopped at the maximum


def doc_pattern(types: tuple[str, ...] = ("pdf",)) -> re.Pattern:
    exts = [e.lstrip(".") for t in types for e in FILE_TYPES.get(t, ())]
    exts = exts or ["pdf"]
    return re.compile(r"\.(?:%s)(?=$|[?#&/])" % "|".join(sorted(set(exts))), re.I)


def browser_url(url: str) -> str:
    """The address as a browser would send it: spaces and other unsafe characters percent-encoded, existing %XX kept."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe="/%:@!$&'()*+,;=~-._"), quote(p.query, safe="=&%+/?:@,;!$'()*~-._"), ""))


def _address_candidates(el: Tag, preferred: list[str]) -> list[tuple[str, str]]:
    """(attribute, raw value) pairs on one element, the scope's own attributes first."""
    order = list(preferred) + [a for a in URL_ATTRS if a not in preferred]
    out = []
    for a in order:
        v = el.get(a)
        if isinstance(v, list):
            v = " ".join(v)
        v = (v or "").strip()
        if v and not v.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:", "blob:")):
            out.append((a, v))
    m = _ONCLICK_URL.search(el.get("onclick", "") or "")
    if m:
        out.append(("onclick", m.group(1).strip()))
    return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _title(el: Tag) -> str:
    own = _clean(el.get_text(" ", strip=True))
    if _WORDS.search(own):
        return own[:300]
    holder = el.find_parent("li")                                      # an icon-only link is named by the item it sits in
    if holder is not None:
        label = _own_label(holder)
        if label:
            return label[:300]
    row = el.find_parent("tr")
    if row is not None:
        cells = [_clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        label = next((c for c in cells if _WORDS.search(c)), "")
        if label:
            return label[:300]
    img = el.find("img")
    for t in (el.get("data-title"), el.get("title"), el.get("aria-label"), img.get("alt") if img else "", img.get("title") if img else ""):
        t = _clean(t or "")
        if _WORDS.search(t):
            return t[:300]
    return ""


def _looks_like_pdf(el: Tag) -> bool:
    """A link that says it is a PDF: 'PDF' in its text, title, class, or in the alt text / file name of its icon."""
    bits = [el.get_text(" ", strip=True), el.get("title"), el.get("aria-label"), el.get("type"), " ".join(el.get("class", [])), el.get("id")]
    for img in el.find_all("img"):
        bits += [img.get("alt"), img.get("title"), (img.get("src") or "").rsplit("/", 1)[-1]]
    return "pdf" in " ".join(str(b) for b in bits if b).lower()


def _own_label(li: Tag) -> str:
    """The label of an outline item: its own text, not the text of the items nested under it."""
    parts: list[str] = []
    for child in li.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name not in ("ul", "ol", "script", "style"):
            parts.append(child.get_text(" ", strip=True))
    label = _clean(" ".join(p for p in (_clean(p) for p in parts) if _WORDS.search(p)))
    return re.sub(r"^[^\w§(\[]+", "", label)[:160]            # drop leading icons / arrows ("> folder-icon Title 1" -> "Title 1")


def outline_path(el: Tag, own_title: str = "") -> str:
    """'Title 1: Government > Division 2 > Chapter 3' - the labels of the list items / <details> that contain `el`."""
    labels: list[str] = []
    norm = _clean(own_title).lower()
    for anc in el.parents:
        if anc.name == "li":
            label = _own_label(anc)
        elif anc.name == "details":
            summ = anc.find("summary", recursive=False)
            label = _clean(summ.get_text(" ", strip=True)) if summ else ""
        else:
            continue
        if not label or (norm and (label.lower() == norm or norm in label.lower() and len(label) < len(norm) + 8)):
            continue                                                  # the link's own list item
        labels.append(label)
    return " > ".join(reversed(labels))


def count_expanders(html: str, scope: str = "", click_selector: str = "") -> int:
    """How many collapsed sections the page (inside the scope's region) shows - they need a browser to be opened."""
    soup = BeautifulSoup(html, "html.parser")
    sel = EXPANDERS + (", " + click_selector if click_selector else "")
    regions = []
    for r in scope_mod.roots(scope) if scope else []:
        regions += soup.select(r)
    seen = set()
    for region in regions or [soup]:
        for e in region.select(sel):
            seen.add(id(e))
    return len(seen)


def count_unresolved(html: str, scope: str) -> int:
    """Elements the scope selects that have no address of their own (javascript:/# link with a click handler, or a button).
    Their address only exists once the page's script runs, so they must be clicked in a browser."""
    if not scope:
        return 0
    n = 0
    for el in BeautifulSoup(html, "html.parser").select(scope_mod.validate(scope)):
        if _address_candidates(el, []):
            continue
        href = (el.get("href") or "").strip().lower()
        if el.get("onclick") or (href.startswith("javascript:") and len(href) > len("javascript:void(0)") + 1):
            n += 1
    return n


def extract(html: str, page_url: str, scope: str = "", types: tuple[str, ...] = ("pdf",), max_items: int = DEFAULT_MAX,
            strict: bool = False) -> Extraction:
    """Document links on the page.

    With a `scope`, the elements it selects are the links the user wants: a selected link yields its address from the attribute
    the scope names (``[data-pdf*='.pdf']`` -> ``data-pdf``), else from its first address - even when that address has no .pdf
    ending (many sites serve files from ``download.do?id=7``; the analysis that follows checks what each one really is).
    A selected *container* yields the document links inside it. Without a scope every document link on the page is taken.
    A link counts as a document when its address ends in .pdf/.doc/.docx, or it says it is a PDF (text, class, icon).
    `strict` keeps only addresses that end in a document extension (or are named by the scope's attribute)."""
    soup = BeautifulSoup(html, "html.parser")
    base = document_base_url(html, page_url)
    pat = doc_pattern(types)
    scope = scope_mod.validate(scope) if scope else ""
    preferred = scope_mod.attributes(scope) if scope else []
    matched = soup.select(scope) if scope else soup.select(_LINK_LIKE)

    out = Extraction()
    seen: set[str] = set()

    def take(el: Tag, direct: bool) -> bool:
        """Add the document address of one element; False when it has no address at all (a container)."""
        cands = _address_candidates(el, preferred)
        if not cands:
            return False
        pick = next(((a, v) for a, v in cands if pat.search(v)), None)
        if pick is None and scope and direct:                         # the user selected this very element
            pick = next(((a, v) for a, v in cands if a in preferred), None) or (None if strict else cands[0])
        if pick is None and not strict and "pdf" in types and _looks_like_pdf(el):
            pick = cands[0]
        if pick is None:
            return True                                               # a link, but not to a document
        attr, value = pick
        url = browser_url(unwrap_viewer(urljoin(base, value)).split("#")[0])      # viewer.html?file=a.pdf -> a.pdf; spaces -> %20
        if not url.lower().startswith(("http://", "https://")):
            return True
        key = document_key(url)
        if key in seen:
            out.duplicates += 1
            return True
        if len(out.found) >= max_items:
            out.truncated = True
            return True
        seen.add(key)
        title = _title(el)
        out.found.append(FoundPdf(url=url, title=title, path=outline_path(el, title), attr=attr, node_id=str(el.get("data-id") or "")))
        return True

    for el in matched:
        if not take(el, True):                                        # a container: look inside it
            inner = el.select(_LINK_LIKE)
            if not inner:
                out.skipped += 1
            for sub in inner:
                take(sub, False)
    return out
