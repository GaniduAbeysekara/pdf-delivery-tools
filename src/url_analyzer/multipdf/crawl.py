"""Several levels of pages: a list page -> its detail pages -> ... -> the PDFs, with a scope for every level.

    Level 1:  open the address you gave, apply scope 1      -> links to open next        (e.g. every agency of a department)
    Level 2:  open each of those pages, apply scope 2       -> links to open next        (e.g. every rule of the agency)
    Level 3:  open each of those pages, apply scope 3       -> the document links        (e.g. the PDF link of the rule)

Every page is read exactly like a single page (page HTML first, a browser when sections must be opened or a link's address
only a script knows). One browser is shared by all pages. The links of the last level are the documents; each one remembers
the titles of the links that led to it ("Mental Health Services > 2 CCR 501-1").
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..analysis.inputs import document_key
from ..logger import log
from . import browser as browser_mod
from .extract import DEFAULT_MAX, FoundPdf
from .finder import Discovery, FindError, PdfFinder

MAX_LEVELS = 5
DEFAULT_MAX_PAGES = 300
HARD_MAX_PAGES = 1500
MAX_SECONDS = 3 * 3600


@dataclass
class _Page:
    url: str
    chain: list[str]          # titles of the links that led here


class Crawler:
    def __init__(self, finder: PdfFinder):
        self.finder = finder

    def run(self, url: str, scopes: list[str], *, types: tuple[str, ...] = ("pdf",), max_items: int = DEFAULT_MAX, strict: bool = False,
            mode: str = "auto", click_selector: str = "", next_selector: str = "", max_pages: int = DEFAULT_MAX_PAGES,
            progress=None, cancelled=None) -> Discovery:
        scopes = list(scopes) or [""]
        if len(scopes) > MAX_LEVELS:
            raise FindError(f"At most {MAX_LEVELS} levels are supported.")
        if len(scopes) == 1:                                           # an ordinary single-page search (a scope is optional)
            return self.finder.find(url, scopes[0], click_selector=click_selector, next_selector=next_selector, mode=mode, types=types,
                                    max_items=max_items, strict=strict, progress=progress, cancelled=cancelled)

        if any(not s for s in scopes):
            raise FindError("Every level needs a scope (a CSS selector for the links to follow).")
        max_pages = max(1, min(int(max_pages or DEFAULT_MAX_PAGES), HARD_MAX_PAGES))
        started = time.monotonic()
        out = Discovery(mode="crawl")
        out.stats.update(levels=[], pages_opened=0, pages_failed=0, clicks=0, resolved=0, unresolved=0, duplicates=0)
        errors: list[str] = []
        opened = 0
        fast = browser_mod.Limits(first_quiet_ms=800, scope_wait_seconds=15, docs_wait_seconds=6)     # later pages: the first level proved the site works
        pages = [_Page(url, [])]
        seen_pages = {document_key(url)}
        seen_docs: set[str] = set()
        browsers = browser_mod.BrowserSession()

        def say(**kw):
            if progress:
                progress(kw)

        try:
            for li, scope in enumerate(scopes):
                final = li == len(scopes) - 1
                level = {"level": li + 1, "pages": 0, "links": 0}
                next_pages: list[_Page] = []
                for pi, page in enumerate(pages):
                    if cancelled and cancelled():
                        out.notes.append("Stopped by the user.")
                        raise _Stop
                    if opened >= max_pages or time.monotonic() - started > MAX_SECONDS:
                        out.notes.append(f"Stopped after opening {opened:,} pages (the limit); more pages exist at level {li + 1}.")
                        raise _Stop
                    say(phase="crawl", message=f"Level {li + 1} of {len(scopes)}: page {pi + 1} of {len(pages)}", links=len(out.found) if final else len(next_pages),
                        clicks=out.stats["clicks"])
                    try:
                        d = self.finder.find(page.url, scope, click_selector=click_selector if li == 0 else "",
                                             next_selector=next_selector if li == 0 else "", mode=mode if li == 0 else "auto",
                                             types=types, max_items=max_items - len(out.found) if final else DEFAULT_MAX,
                                             strict=strict and final, cancelled=cancelled, session=browsers, resolve_links=final,
                                             limits=None if li == 0 else fast)
                    except FindError as e:
                        out.stats["pages_failed"] += 1
                        errors.append(f"level {li + 1}: {page.url[:110]} - {e}")
                        if li == 0:
                            raise                                       # the page you gave must work
                        continue
                    opened += 1
                    level["pages"] += 1
                    for k in ("clicks", "resolved", "unresolved", "duplicates"):
                        out.stats[k] += int(d.stats.get(k, 0) or 0)
                    if li == 0:
                        out.final_url = d.final_url
                    for f in d.found:
                        if final:
                            key = document_key(f.url)
                            if key in seen_docs:
                                out.stats["duplicates"] += 1
                                continue
                            if len(out.found) >= max_items:
                                out.stats["truncated"] = True
                                raise _Stop
                            seen_docs.add(key)
                            trail = [t for t in page.chain + ([f.path] if f.path else []) if t]
                            out.found.append(FoundPdf(url=f.url, title=f.title, path=" > ".join(trail), attr=f.attr, node_id=f.node_id, source=page.url))
                            level["links"] += 1
                        else:
                            key = document_key(f.url)
                            if key in seen_pages:
                                continue
                            seen_pages.add(key)
                            next_pages.append(_Page(f.url, page.chain + [f.title or f.url.rsplit("/", 1)[-1][:60]]))
                            level["links"] += 1
                out.stats["levels"].append(level)
                if not final:
                    if not next_pages:
                        raise FindError(f"Level {li + 1} found no links to open ({level['pages']} page(s) read with scope: {scope}). "
                                        + ("Problems: " + "; ".join(errors[:3]) if errors else "Check that scope."))
                    pages = next_pages
        except _Stop:
            pass
        finally:
            browsers.close()

        out.stats["pages_opened"] = opened
        out.stats["seconds"] = round(time.monotonic() - started, 1)
        out.notes.insert(0, "Opened " + f"{opened:,} page{'s' if opened != 1 else ''} over {len(out.stats['levels'])} levels ("
                         + ", ".join(f"level {lv['level']}: {lv['pages']} page(s) -> {lv['links']} link(s)" for lv in out.stats["levels"]) + ").")
        if out.stats["resolved"]:
            out.notes.append(f"{out.stats['resolved']:,} link(s) had no address of their own; clicking them showed where they lead"
                             + (f" ({out.stats['unresolved']} led nowhere)." if out.stats["unresolved"] else "."))
        if out.stats["pages_failed"]:
            out.notes.append(f"{out.stats['pages_failed']} page(s) could not be read: " + "; ".join(errors[:5]) + (" ..." if len(errors) > 5 else ""))
        if out.stats.get("truncated"):
            out.notes.append(f"Stopped at the maximum of {max_items:,} PDFs; there are more.")
        log.info("Multi-PDF crawl: %s -> %d link(s), %d page(s) in %.1fs", url, len(out.found), opened, out.stats["seconds"])
        return out


class _Stop(Exception):
    """Internal: leave the loops (limit reached or user stop) but keep what was found."""
