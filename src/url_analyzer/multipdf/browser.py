"""Open a page in a real (headless) browser and open every collapsed section inside the scope.

Why a browser: many pages only create their document links when a title/section is clicked (the page then asks the
server for the children and inserts them). A plain HTTP fetch never sees those links; a browser does exactly what a
person does - click, wait, repeat - and the finished page is then read with the normal link extraction.

Safety: every request the page makes is checked with the same validation as the rest of the tool (no private or local
addresses, only http/https); images, video and fonts are not loaded; downloads are refused.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urldefrag, urlparse

from ..html_analyzer import _SKIP_FRAME_HOSTS
from ..logger import log
from . import scope as scope_mod
from .extract import EXPANDERS


class BrowserUnavailable(RuntimeError):
    """No usable browser / Playwright (message tells the user how to fix it)."""


class BrowserError(RuntimeError):
    """The page could not be opened or processed (message is safe to show)."""


@dataclass
class Limits:
    max_clicks: int = 3000          # sections opened in total
    max_rounds: int = 80            # open-then-wait rounds (the depth of the outline)
    batch: int = 40                 # sections opened at once - keeps the server's load modest
    seconds: float = 600            # whole run
    settle_seconds: float = 25      # wait for one round of loading to finish
    quiet_ms: int = 600             # the page counts as settled after this long without changes
    first_quiet_ms: int = 2500      # ... but right after the page opens, scripts often idle for seconds before they start loading
    scope_wait_seconds: float = 30  # keep waiting for the scope to match something (content that is built late)
    docs_wait_seconds: float = 12   # same, when there is no scope: wait for anything that looks like a document link
    max_pages: int = 30             # "next page" clicks
    max_resolve: int = 200          # links without an address of their own that are clicked to learn where they lead


@dataclass
class BrowserResult:
    snapshots: list[tuple[str, str]] = field(default_factory=list)      # (page address, page HTML) - one per page of results
    clicks: int = 0
    rounds: int = 0
    pages: int = 0
    blocked_requests: int = 0
    resolved: int = 0                # javascript: links whose address was learned by clicking them
    unresolved: int = 0              # ... and those that led nowhere
    notes: list[str] = field(default_factory=list)


# ── the page-side scripts ────────────────────────────────────────────────────
_JS_OBSERVE = """(reset) => {
  if (reset || !window.__mpfMut) window.__mpfMut = Date.now();
  if (!window.__mpfObs) { window.__mpfObs = new MutationObserver(() => { window.__mpfMut = Date.now(); });
    window.__mpfObs.observe(document.documentElement, {childList: true, subtree: true, attributes: true, characterData: true}); }
}"""
_JS_QUIET = "() => Date.now() - (window.__mpfMut || 0)"
_JS_CLICK = """(args) => {
  const [roots, sel, limit] = args;
  if (!window.__mpfGuard) {            // a control that is really a link must open its section, not leave the page
    window.__mpfGuard = true;
    document.addEventListener('click', ev => {
      if (!window.__mpfBlockNav) return;
      const a = ev.target.closest && ev.target.closest('a[href]');
      if (a && !(a.getAttribute('href') || '').startsWith('#')) ev.preventDefault();
    }, true);
  }
  const regions = roots.length ? roots.flatMap(r => { try { return [...document.querySelectorAll(r)]; } catch (e) { return []; } }) : [document.body];
  const out = [], seen = new Set();
  for (const region of regions) {
    let list; try { list = region.querySelectorAll(sel); } catch (e) { continue; }
    for (const e of list) {
      if (seen.has(e) || e.dataset.mpfClicked) continue; seen.add(e);
      if (e.getAttribute('aria-expanded') === 'true' || e.disabled) continue;
      const cs = getComputedStyle(e);
      if (cs.visibility === 'hidden' || cs.display === 'none' || e.getClientRects().length === 0) continue;
      out.push(e); if (out.length >= limit) break;
    }
    if (out.length >= limit) break;
  }
  window.__mpfBlockNav = true;
  try { out.forEach(e => { e.dataset.mpfClicked = '1'; e.click(); }); } finally { window.__mpfBlockNav = false; }
  return out.length;
}"""
_JS_COUNT = "(s) => { try { return document.querySelectorAll(s).length; } catch (e) { return -1; } }"
# no scope: anything that looks like a document link (a .pdf address, a data-pdf attribute, a PDF icon, a download link)
_DOC_PROBE = "a[href*='.pdf' i], [data-pdf], [data-file], a[href*='download' i], a img[alt*='pdf' i], a[class*='pdf' i]"
# links the scope selects that have no address of their own: href="javascript:void(0)" / "#" with a click handler
_JS_MARK_DEAD = """(scope) => {
  let els; try { els = [...document.querySelectorAll(scope)]; } catch (e) { return 0; }
  let n = 0;
  for (const e of els) {
    const h = (e.getAttribute('href') || '').trim().toLowerCase();
    const dead = !h || h === '#' || h.startsWith('#!') || h.startsWith('javascript:');
    if (dead && (e.getAttribute('onclick') || h.length > 'javascript:void(0)'.length + 1)) { e.setAttribute('data-mpf-idx', String(n)); n++; }
  }
  return n;
}"""
_JS_CLICK_IDX = "(i) => { const e = document.querySelector('[data-mpf-idx=\"' + i + '\"]'); if (!e) return false; e.click(); return true; }"
_JS_SET_URL = "(a) => { const e = document.querySelector('[data-mpf-idx=\"' + a[0] + '\"]'); if (e) e.setAttribute('data-mpf-url', a[1]); }"
_JS_NEXT = """(sel) => {
  let list; try { list = [...document.querySelectorAll(sel)]; } catch (e) { return 'invalid'; }
  for (const e of list) {
    if (e.disabled || e.getAttribute('aria-disabled') === 'true' || e.classList.contains('disabled')) continue;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none' || e.getClientRects().length === 0) continue;
    e.click(); return 'clicked';          // the same button may be clicked again ("load more"); the caller stops when nothing changes
  }
  return 'none';
}"""


def _launch(p):
    """A headless browser: Microsoft Edge or Google Chrome if installed (no download needed), else Playwright's Chromium."""
    errors = []
    for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
        try:
            return p.chromium.launch(headless=True, **kw), kw.get("channel", "chromium")
        except Exception as e:                                           # not installed / cannot start
            errors.append(str(e).splitlines()[0][:120])
    raise BrowserUnavailable(
        "No browser could be started. Install Microsoft Edge or Google Chrome (or run `playwright install chromium`). "
        f"Details: {errors[-1] if errors else 'unknown'}")


class BrowserSession:
    """One headless browser for many pages: a crawl opens hundreds, and starting the browser each time would take most of the time.
    It starts on first use. Use it from the thread that created it (Playwright's sync API requires that)."""

    def __init__(self):
        self._pw = None
        self.browser = None
        self.engine = ""

    def open(self) -> "BrowserSession":
        if self.browser is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable("Playwright is not installed. Run `pip install playwright` (it uses the Edge or Chrome "
                                     "already on this computer - no extra browser download is needed).") from e
        self._pw = sync_playwright().start()
        try:
            self.browser, self.engine = _launch(self._pw)
        except Exception:
            self._pw.stop()
            self._pw = None
            raise
        return self

    def close(self) -> None:
        for closer in (lambda: self.browser and self.browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self.browser = self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@contextlib.contextmanager
def _session_scope(session: "BrowserSession | None"):
    """The caller's session (left open for the next page), or a private one that is closed afterwards."""
    own = session is None
    sess = session or BrowserSession()
    sess.open()
    try:
        yield sess
    finally:
        if own:
            sess.close()


def available() -> bool:
    """True when Playwright can be imported (a browser is only looked for when one is actually needed)."""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def render_and_expand(url: str, *, scope: str = "", click_selector: str = "", next_selector: str = "",
                      validate: Callable[[str], str], user_agent: str = "", limits: Limits | None = None,
                      progress: Callable[[dict], None] | None = None, cancelled: Callable[[], bool] | None = None,
                      session: "BrowserSession | None" = None, resolve_links: bool = False) -> BrowserResult:
    """Load `url`, open every collapsed section inside the scope's region, optionally walk through result pages,
    and return the finished page(s). `validate(url)` returns '' for an allowed address, else a message.
    `resolve_links`: links the scope selects that have no address of their own (javascript:void(0) + onclick) are clicked and
    the address the page tries to open is recorded on them (data-mpf-url). `session`: reuse one browser for many pages."""
    lim = limits or Limits()
    result = BrowserResult()
    t_end = time.monotonic() + lim.seconds
    region_roots = scope_mod.roots(scope) if scope else []
    expander_css = EXPANDERS + (", " + click_selector if click_selector else "")
    state = {"inflight": 0, "allow_nav": True, "page": None, "capture": None}
    host_ok: dict[str, bool] = {}

    def say(**kw):
        if progress:
            progress({"clicks": result.clicks, "rounds": result.rounds, "pages": result.pages, **kw})

    with _session_scope(session) as sess:
        browser, engine = sess.browser, sess.engine
        log.info("Multi-PDF: using %s for %s", engine, url)
        ctx = None
        try:
            ctx = browser.new_context(user_agent=user_agent or None, accept_downloads=False, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            state["page"] = page
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: d.dismiss())                           # an alert() must not freeze a click

            def run_js(script, *arg, frame=None):
                """evaluate in the page (or one of its frames); waits and retries when it navigates at that very moment."""
                target = frame or page
                for attempt in range(6):
                    try:
                        return target.evaluate(script, *arg)
                    except Exception as e:
                        if attempt < 5 and any(m in str(e) for m in ("context was destroyed", "navigating", "Cannot find context")):
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                            page.wait_for_timeout(250)
                            continue
                        raise

            def live_frames():
                """The page's own document first, then every frame inside it (frames hold the content on many sites)."""
                return [f for f in page.frames if not f.is_detached()]

            def each_frame(script, *arg) -> list:
                out = []
                for fr in live_frames():
                    try:
                        out.append(run_js(script, *arg, frame=fr))
                    except Exception:                                   # a frame that closed or is mid-navigation
                        continue
                return out

            def content() -> str:
                for attempt in range(6):
                    try:
                        return page.content()
                    except Exception as e:
                        if attempt < 5 and "navigating" in str(e):
                            page.wait_for_timeout(300)
                            continue
                        raise

            def snapshot() -> list[tuple[str, str]]:
                """(address, HTML) of the page and of every frame inside it - a scope applies to each of them."""
                snaps = [(page.url, content())]
                for fr in live_frames():
                    if fr == page.main_frame:
                        continue
                    addr = fr.url or ""
                    if addr == "about:srcdoc" and fr.parent_frame is not None:         # <iframe srcdoc="..."> has no address of its own
                        addr = fr.parent_frame.url or ""
                    host = (urlparse(addr).hostname or "").lower()
                    if not addr.startswith(("http://", "https://")) or any(host == h or host.endswith("." + h) for h in _SKIP_FRAME_HOSTS):
                        continue
                    try:
                        html = fr.content()
                    except Exception:
                        continue
                    if len(html) > 80:
                        snaps.append((addr, html))
                return snaps

            def on_route(route):
                req = route.request
                if req.resource_type in ("image", "media", "font"):
                    return route.abort()
                target = req.url
                if target.split(":", 1)[0].lower() in ("data", "blob", "about"):
                    return route.continue_()
                host = (urlparse(target).hostname or "").lower()
                if host not in host_ok:
                    host_ok[host] = not validate(target)                  # same rules as every other request the tool makes
                if not host_ok[host]:
                    result.blocked_requests += 1
                    return route.abort()
                try:
                    navigating = req.is_navigation_request()
                except Exception:
                    navigating = False
                if state["capture"] is not None and navigating:                # a click is being traced: note where it goes
                    state["capture"].append(target)
                    return route.fulfill(status=204, body="")                     # "No Content": the browser stays where it is, nothing is fetched
                try:
                    main_nav = navigating and req.frame == page.main_frame
                except Exception:                                       # requests with no frame (service workers)
                    main_nav = False
                if main_nav and not state["allow_nav"] and urldefrag(target)[0] != urldefrag(page.url)[0]:
                    result.notes.append("The page tried to navigate away while sections were being opened; that was stopped.")
                    return route.abort()
                return route.continue_()

            ctx.route("**/*", on_route)
            page.on("request", lambda r: state.__setitem__("inflight", state["inflight"] + 1) if r.resource_type in ("xhr", "fetch") else None)
            for ev in ("requestfinished", "requestfailed"):
                page.on(ev, lambda r: state.__setitem__("inflight", max(0, state["inflight"] - 1)) if r.resource_type in ("xhr", "fetch") else None)

            def settle(quiet_ms: int | None = None) -> None:
                """Wait until no data request is in flight and the page has stopped changing."""
                quiet_ms = quiet_ms or lim.quiet_ms
                each_frame(_JS_OBSERVE, True)
                t0 = time.monotonic()
                while time.monotonic() - t0 < lim.settle_seconds and time.monotonic() < t_end:
                    if cancelled and cancelled():
                        return
                    page.wait_for_timeout(150)
                    each_frame(_JS_OBSERVE, False)                       # frames that appeared meanwhile
                    if state["inflight"] == 0 and min(each_frame(_JS_QUIET) or [10 ** 9]) > quiet_ms:
                        return
                result.notes.append("Some content was still loading when the wait ended; a result may be incomplete.")

            def matches() -> int:
                return sum(n for n in each_frame(_JS_COUNT, scope) if isinstance(n, int) and n > 0) if scope else -1

            def found_now() -> int:
                """How many of the links being looked for are on the page (in any frame) right now."""
                if scope:
                    return max(0, matches())
                return sum(n for n in each_frame(_JS_COUNT, _DOC_PROBE) if isinstance(n, int) and n > 0)

            def wait_for_content(first_page: bool) -> None:
                """A quiet page is not proof that it is finished: scripts may wait seconds before loading the list.
                So when nothing that was asked for is on the page yet, keep looking (and opening) until it appears."""
                limit = lim.scope_wait_seconds if scope else lim.docs_wait_seconds
                if not first_page or found_now() > 0:
                    return
                t0 = time.monotonic()
                say(message="Waiting for the page to load its links")
                while time.monotonic() - t0 < limit and time.monotonic() < t_end:
                    if cancelled and cancelled():
                        return
                    page.wait_for_timeout(500)
                    if found_now() > 0:
                        settle()                                        # the rest of the list may still be arriving
                        return
                    if result.clicks < lim.max_clicks and run_expanders_once():
                        settle()
                if scope:
                    result.notes.append(f"The scope matched nothing, even after waiting {int(limit)} seconds for the page to load.")

            def resolve_dead_links() -> None:
                """Click every selected link that has no address of its own and record where the page sends the browser."""
                done = 0
                for fr in live_frames():
                    try:
                        count = run_js(_JS_MARK_DEAD, scope, frame=fr) or 0
                    except Exception:
                        continue
                    for i in range(count):
                        if done >= lim.max_resolve or (cancelled and cancelled()) or time.monotonic() >= t_end:
                            result.notes.append(f"Only the first {lim.max_resolve} links without an address were followed.")
                            return
                        done += 1
                        state["capture"] = []
                        try:
                            run_js(_JS_CLICK_IDX, i, frame=fr)
                            waited = 0
                            while not state["capture"] and waited < 2500:
                                page.wait_for_timeout(100)
                                waited += 100
                        except Exception:
                            pass
                        got, state["capture"] = state["capture"] or [], None
                        for extra in ctx.pages:                                  # pop-ups opened by the click
                            if extra != page:
                                try:
                                    extra.close()
                                except Exception:
                                    pass
                        here = urldefrag(page.url)[0]
                        target = next((u for u in got if u.lower().startswith(("http://", "https://")) and urldefrag(u)[0] != here), "")
                        if target:
                            try:
                                run_js(_JS_SET_URL, [i, target], frame=fr)
                                result.resolved += 1
                            except Exception:
                                result.unresolved += 1
                        else:
                            result.unresolved += 1

            def run_expanders_once() -> bool:
                """One round of opening collapsed sections (they can appear late as well). True when something was opened."""
                n = 0
                for fr in live_frames():
                    try:
                        n += run_js(_JS_CLICK, [region_roots, expander_css, lim.batch], frame=fr) or 0
                    except Exception:
                        continue
                result.clicks += n
                if n:
                    result.rounds += 1
                return bool(n)

            def expand_all() -> None:
                stalled = 0
                while result.rounds < lim.max_rounds and result.clicks < lim.max_clicks and time.monotonic() < t_end:
                    if cancelled and cancelled():
                        result.notes.append("Stopped by the user.")
                        return
                    n = 0
                    for fr in live_frames():                                # the scope's region may be inside a frame
                        room = min(lim.batch, lim.max_clicks - result.clicks) - n
                        if room <= 0:
                            break
                        try:
                            n += run_js(_JS_CLICK, [region_roots, expander_css, room], frame=fr) or 0
                        except Exception:
                            continue
                    if not n:
                        stalled += 1
                        if stalled >= 2:                                   # nothing new twice in a row (after a settle)
                            return
                        settle()
                        continue
                    stalled = 0
                    result.clicks += n
                    result.rounds += 1
                    settle()
                    say(links=matches(), message="Opening sections")
                if result.clicks >= lim.max_clicks:
                    result.notes.append(f"Stopped after opening {lim.max_clicks:,} sections (the limit); more may exist.")
                elif result.rounds >= lim.max_rounds:
                    result.notes.append(f"Stopped after {lim.max_rounds} rounds (the limit); deeper sections may exist.")
                elif time.monotonic() >= t_end:
                    result.notes.append("Stopped at the time limit; more sections may exist.")

            say(message="Opening the page")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                raise BrowserError(f"The page could not be opened in the browser: {str(e).splitlines()[0][:160]}") from e
            if result.blocked_requests and not page.url.startswith(("http://", "https://")):
                raise BrowserError("The page redirected to an address that is not allowed.")
            settle(lim.first_quiet_ms)
            state["allow_nav"] = bool(next_selector)                       # clicking a "next" link may legitimately load a new page

            if page.url.startswith("chrome-error:"):
                raise BrowserError("The page could not be loaded in the browser.")
            seen_html = set()
            for page_no in range(1, lim.max_pages + 1):
                expand_all()
                wait_for_content(page_no == 1)
                if result.clicks and found_now() > 0:
                    expand_all()                                           # sections that appeared while waiting
                if resolve_links and scope:
                    resolve_dead_links()
                snaps = snapshot()
                h = hash(tuple(html for _, html in snaps))
                if h in seen_html:
                    break                                                   # paging did not change anything
                seen_html.add(h)
                result.snapshots.extend(snaps)
                result.pages = page_no
                say(links=matches(), message="Reading the page" if page_no == 1 else f"Read page {page_no}")
                if not next_selector or (cancelled and cancelled()) or time.monotonic() >= t_end:
                    break
                outcomes = each_frame(_JS_NEXT, next_selector)
                outcome = "clicked" if "clicked" in outcomes else ("invalid" if outcomes and all(o == "invalid" for o in outcomes) else "none")
                if outcome == "invalid":
                    raise BrowserError("The 'next page' selector is not valid in the browser.")
                if outcome != "clicked":
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                settle()
            else:
                result.notes.append(f"Stopped after {lim.max_pages} pages (the limit).")
        except (BrowserError, BrowserUnavailable):
            raise
        except Exception as e:
            raise BrowserError(f"The browser stopped unexpectedly: {str(e).splitlines()[0][:160]}") from e
        finally:
            try:
                if ctx is not None:
                    ctx.close()                                             # the browser itself stays open for the next page
            except Exception:
                pass
    if result.resolved or result.unresolved:
        result.notes.append(f"{result.resolved} link(s) had no address of their own; clicking them showed where they lead"
                            + (f" ({result.unresolved} led nowhere)." if result.unresolved else "."))
    if result.blocked_requests:
        log.info("Multi-PDF: %d request(s) made by the page were blocked (private/invalid addresses)", result.blocked_requests)
    return result
