"""Find every PDF link on a page (optionally inside a CSS scope), opening collapsed sections when that is needed.

    page address (+ scope)
        |-- fetch the page (same safe HTTP client as the rest of the tool) and read the links it already contains
        |-- if sections are collapsed / the page builds its links with JavaScript -> open it in a browser, click every
        |   collapsed section inside the scope, wait for the content, read the finished page
        '-- links (address, title, outline path) are handed to the normal URL analysis (page count, last modified ...)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urldefrag

from ..analysis.document_analyzer import DIRECT_TYPES, detect_content_type, disposition_filename
from ..analysis.engine import URLAnalyzer
from ..analysis.http_analyzer import CHUNK, header
from ..analysis.inputs import document_key
from ..html_analyzer import detect_bot_protection, find_embedded_documents
from ..logger import log
from . import browser as browser_mod
from . import scope as scope_mod
from urllib.parse import urlparse

from .extract import DEFAULT_MAX, FoundPdf, count_expanders, count_unresolved, extract

MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_STATIC_FRAMES = 8          # frame documents fetched when reading a page without a browser
MODES = ("auto", "page", "browser")


class FindError(ValueError):
    """The search could not be carried out (message is safe to show to the user)."""


@dataclass
class Discovery:
    found: list[FoundPdf] = field(default_factory=list)
    mode: str = "page"                                   # how the links were read: "page" (HTML only) or "browser"
    final_url: str = ""
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    prepared: list = field(default_factory=list)          # ready-made URLAnalysisResults (from a site recipe); the normal analysis is then skipped


def normalise_url(url: str) -> str:
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u.lstrip("/")
    return u


class PdfFinder:
    def __init__(self, analyzer: URLAnalyzer, browser_fn: Callable | None = None, browser_ok: Callable[[], bool] | None = None,
                 browser_limits: browser_mod.Limits | None = None):
        self.a = analyzer
        self.browser_fn = browser_fn or browser_mod.render_and_expand
        self.browser_ok = browser_ok or browser_mod.available
        self.limits = browser_limits

    # ── input checks (cheap, run before a job starts) ────────────────────────
    def check(self, url: str, scope: str = "", click_selector: str = "", next_selector: str = "", mode: str = "auto") -> dict:
        url = normalise_url(url)
        if not url:
            raise FindError("Enter the address of the page.")
        err = self.a.http.validate(url)
        if err:
            raise FindError(err)
        if mode not in MODES:
            raise FindError("Unknown mode.")
        try:
            return {"url": url, "scope": scope_mod.validate(scope, "scope"),
                    "click_selector": scope_mod.validate(click_selector, "click-to-open"),
                    "next_selector": scope_mod.validate(next_selector, "next-page"), "mode": mode}
        except scope_mod.ScopeError as e:
            raise FindError(str(e)) from e

    # ── the search ───────────────────────────────────────────────────────────
    def _fetch_page(self, url: str, deadline: float) -> tuple[str, str, str]:
        """(final_url, html, kind) where kind is 'html' or 'document' (the address itself is a PDF / Word file)."""
        notes: list[str] = []
        try:
            resp, _, _, final = self.a.http.request(url, deadline, notes)
        except Exception as e:
            message, _ = self.a.http.failure_message(e)
            raise FindError(f"The page could not be fetched: {message}") from e
        try:
            if resp.status_code >= 400:
                raise FindError(f"The page could not be fetched: HTTP {resp.status_code}")
            it = resp.iter_content(CHUNK)
            head = next(it, b"") or b""
            ctype = detect_content_type(header(resp.headers, "Content-Type"), final, head,
                                        disposition_filename(header(resp.headers, "Content-Disposition")))
            if ctype in DIRECT_TYPES:
                return final, "", "document"
            data, truncated = self.a.http.read(it, head, MAX_PAGE_BYTES, deadline)
            try:
                html = data.decode("utf-8")
            except UnicodeDecodeError:
                html = data.decode("cp1252", "replace")
            return final, html, "html"
        finally:
            resp.close()

    def _read_static(self, html: str, base: str, scope: str, types: tuple, max_items: int, strict: bool, click_selector: str,
                     deadline: float):
        """Links in the page's own HTML plus those in its frames (<iframe>/<frame> documents are fetched too, two levels deep).
        Returns (Extraction, number of collapsed sections seen, number of links with no address of their own)."""
        merged = extract(html, base, scope, types, max_items, strict)
        collapsed = count_expanders(html, scope, click_selector)
        unresolved = count_unresolved(html, scope)
        seen = {document_key(f.url) for f in merged.found}
        budget = {"frames": MAX_STATIC_FRAMES}

        def visit(page_html: str, page_url: str, depth: int) -> None:
            nonlocal collapsed, unresolved
            if depth >= 2:
                return
            for fr in find_embedded_documents(page_html, page_url, 8):
                if fr["tag"] not in ("iframe", "frame") or fr["target"] != fr["src"] or budget["frames"] <= 0:
                    continue                                          # a viewer wrapper is already unwrapped by extract()
                budget["frames"] -= 1
                try:
                    final, fhtml, kind = self._fetch_page(fr["src"], deadline)
                except FindError:
                    continue
                if kind == "document":
                    if document_key(final) not in seen and len(merged.found) < max_items:
                        seen.add(document_key(final))
                        merged.found.append(FoundPdf(url=final, title=final.rsplit("/", 1)[-1]))
                    continue
                ex = extract(fhtml, final, scope, types, max_items - len(merged.found), strict)
                merged.duplicates += ex.duplicates
                merged.skipped += ex.skipped
                merged.truncated = merged.truncated or ex.truncated
                for f in ex.found:
                    if document_key(f.url) in seen:
                        merged.duplicates += 1
                    else:
                        seen.add(document_key(f.url))
                        merged.found.append(f)
                collapsed += count_expanders(fhtml, scope, click_selector)
                unresolved += count_unresolved(fhtml, scope)
                visit(fhtml, final, depth + 1)

        visit(html, base, 0)
        return merged, collapsed, unresolved

    def find(self, url: str, scope: str = "", *, click_selector: str = "", next_selector: str = "", mode: str = "auto",
             types: tuple[str, ...] = ("pdf",), max_items: int = DEFAULT_MAX, strict: bool = False,
             progress: Callable[[dict], None] | None = None, cancelled: Callable[[], bool] | None = None,
             session=None, resolve_links: bool = True, limits: browser_mod.Limits | None = None) -> Discovery:
        chk = self.check(url, scope, click_selector, next_selector, mode)
        url, scope, click_selector, next_selector, mode = chk["url"], chk["scope"], chk["click_selector"], chk["next_selector"], chk["mode"]
        max_items = max(1, min(int(max_items or DEFAULT_MAX), DEFAULT_MAX))
        t0 = time.monotonic()
        out = Discovery(final_url=url)

        def say(**kw):
            if progress:
                progress(kw)

        static = None
        collapsed = unresolved = 0
        shielded = False
        if mode != "browser":
            say(message="Reading the page", phase="page")
            final, html, kind = self._fetch_page(urldefrag(url)[0], t0 + self.a.s.total_deadline)     # a #fragment is never sent to a server
            out.final_url = final
            if kind == "document":
                out.found = [FoundPdf(url=final, title=final.rsplit("/", 1)[-1])]
                out.notes.append("The address itself is a document, not a page - it was analysed directly.")
                out.stats = {"pages": 0, "clicks": 0, "duplicates": 0}
                return out
            static, collapsed, unresolved = self._read_static(html, final, scope, types, max_items, strict, click_selector, t0 + self.a.s.total_deadline)
            shielded = bool(not static.found and detect_bot_protection(html))
            out.stats["static_found"] = len(static.found)
            out.stats["collapsed_sections"] = collapsed
            out.stats["scripted_links"] = unresolved

        needs_script = bool(resolve_links and scope and unresolved > 0)          # links whose address only a click reveals
        if cancelled and cancelled():                                         # the user pressed Stop while the page was being read
            out.found = static.found if static else []
            out.notes.append("Stopped by the user.")
            return out
        want_browser = mode == "browser" or (mode == "auto" and (collapsed > 0 or needs_script or click_selector or next_selector
                                                                  or not (static and static.found)))
        if want_browser and not self.browser_ok():
            msg = ("A browser is needed (to open collapsed sections, or to find where links that only a script knows lead), "
                   "but Playwright is not installed. Run `pip install playwright` (it uses the Edge or Chrome already on this computer).")
            if mode == "browser" or not (static and static.found):
                raise FindError(msg)
            out.notes.append(msg + " Only the links already visible on the page were read.")
            want_browser = False

        result_static = static
        if want_browser:
            def on_progress(d):
                say(phase="browser", **d)
            try:
                self.a.http.throttle((urlparse(url).hostname or "").lower())      # the browser is polite to the site too
                res = self.browser_fn(url, scope=scope, click_selector=click_selector, next_selector=next_selector,
                                      validate=self.a.http.validate, user_agent=self.a.s.user_agent, limits=limits or self.limits,
                                      progress=on_progress, cancelled=cancelled, session=session, resolve_links=resolve_links)
            except (browser_mod.BrowserUnavailable, browser_mod.BrowserError) as e:
                if mode == "browser" or not (static and static.found):
                    raise FindError(str(e)) from e
                out.notes.append(f"{e} Only the links already visible on the page were read.")
                res = None
            if res is not None:
                say(message="Collecting the PDF links", phase="extract")
                seen: set[str] = set()
                dups = skipped = 0
                for page_url, html in res.snapshots:
                    ex = extract(html, page_url, scope, types, max_items - len(out.found), strict)
                    dups += ex.duplicates
                    skipped += ex.skipped
                    for f in ex.found:
                        k = document_key(f.url)
                        if k in seen:
                            dups += 1
                        else:
                            seen.add(k)
                            out.found.append(f)
                    if ex.truncated or len(out.found) >= max_items:
                        out.stats["truncated"] = True
                        break
                if static:                                            # anything only the plain page had (rare) is kept too
                    for f in static.found:
                        k = document_key(f.url)
                        if k not in seen and len(out.found) < max_items:
                            seen.add(k)
                            out.found.append(f)
                out.mode = "browser"
                out.final_url = res.snapshots[-1][0] if res.snapshots else out.final_url
                out.notes.extend(n for n in res.notes if n not in out.notes)
                out.stats.update(clicks=res.clicks, rounds=res.rounds, pages=res.pages, duplicates=dups, skipped=skipped,
                                 blocked_requests=res.blocked_requests, resolved=res.resolved, unresolved=res.unresolved)
                if res.clicks:
                    out.notes.insert(0, f"Opened {res.clicks:,} collapsed section{'s' if res.clicks != 1 else ''} in a browser "
                                        f"({res.rounds} round{'s' if res.rounds != 1 else ''}).")
                if res.blocked_requests:
                    out.notes.append(f"{res.blocked_requests} request(s) made by the page to private or invalid addresses were blocked.")
        if out.mode == "page" and result_static is not None:
            out.found = result_static.found
            out.stats.update(duplicates=result_static.duplicates, skipped=result_static.skipped, clicks=0, pages=1)
            if result_static.truncated:
                out.stats["truncated"] = True
        if shielded and not out.found:                       # reported only when nothing could be read even in a browser
            out.notes.append("The page answered with a bot-protection (verification) page, so its links could not be read.")
        if out.stats.get("truncated"):
            out.notes.append(f"Stopped at the maximum of {max_items:,} PDFs; the page has more.")
        out.stats["seconds"] = round(time.monotonic() - t0, 1)
        log.info("Multi-PDF finder: %s -> %d link(s) via %s in %.1fs", url, len(out.found), out.mode, out.stats["seconds"])
        return out
