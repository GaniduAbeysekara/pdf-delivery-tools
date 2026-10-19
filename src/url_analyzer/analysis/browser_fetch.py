"""Read a file through a real browser window, for sites that refuse automated requests (HTTP 403 from Cloudflare and the like).

A normal Edge/Chrome window is opened with its own profile (so a check you approve once is remembered). The page of the
site is loaded like any visitor would, and the file is then fetched from inside that page - the same request the site's
own pages make, with the browser's cookies. If the site shows a verification page, the window is visible and the
person using the tool approves it; this code only waits for that. It never solves a check and never disguises the browser.

Playwright's sync API belongs to one thread, so one worker thread owns the browser and requests are queued to it.
"""
from __future__ import annotations

import base64
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from ..html_analyzer import detect_bot_protection
from ..logger import log

DEFAULT_PROFILE = Path(__file__).resolve().parents[3] / "data" / "browser_profile"
BLOCKED_CODES = (401, 403, 429, 503)

_JS_FETCH = """async ([url, cap]) => {
  const r = await fetch(url, {credentials: 'include', redirect: 'follow', signal: AbortSignal.timeout(120000)});
  const out = {status: r.status, url: r.url, type: r.headers.get('content-type') || '', disposition: r.headers.get('content-disposition') || '',
               length: Number(r.headers.get('content-length') || 0)};
  if (out.length > cap) { out.tooBig = true; return out; }
  const buf = new Uint8Array(await r.arrayBuffer());
  out.size = buf.length;
  if (buf.length > cap) { out.tooBig = true; return out; }
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  out.b64 = btoa(s);
  return out;
}"""


class BrowserUnavailable(RuntimeError):
    pass


@dataclass
class BrowserFile:
    ok: bool = False
    status: int = 0
    data: bytes = b""
    size: int = 0                      # real size, also when the file was too big to bring back
    too_big: bool = False
    content_type: str = ""
    disposition: str = ""
    final_url: str = ""
    html: str = ""                     # the text, when the answer was a web page
    error: str = ""
    blocked: bool = False              # the site still refused (or the check was not approved)
    approved_by_user: bool = False     # a verification page was shown and passed while waiting
    shield: bool = False               # the answer was a verification page
    skipped: bool = False              # the person chose to skip this URL


@dataclass
class _Job:
    url: str
    max_bytes: int
    abandoned: bool = False            # the caller no longer wants the answer (skipped): stop waiting for the person
    done: threading.Event = field(default_factory=threading.Event)
    result: BrowserFile = field(default_factory=BrowserFile)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class BrowserFetcher:
    def __init__(self, profile_dir: str | Path = DEFAULT_PROFILE, *, headless: bool = False, approve_timeout: float = 180,
                 idle_close: float = 120, nav_timeout: float = 45, validate: Callable[[str], str] | None = None,
                 can_wait: bool | None = None):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.approve_timeout = approve_timeout
        self.idle_close = idle_close
        self.nav_timeout = nav_timeout
        self.validate = validate or (lambda url: "")
        self.can_wait = (not headless) if can_wait is None else can_wait      # a person can approve a verification page in the window
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._waiting: dict | None = None
        self._current: _Job | None = None
        self._pages: dict[str, object] = {}

    # ── for callers (any thread) ────────────────────────────────────────────
    def fetch(self, url: str, *, max_bytes: int = 50 * 1024 * 1024, timeout: float | None = None,
              stop: threading.Event | None = None) -> BrowserFile:
        """The file (or page) at `url`, read through the browser. Never raises. `stop` (set by the caller) skips this URL:
        the call returns at once and the browser stops waiting for it."""
        err = self.validate(url)
        if err:
            return BrowserFile(error=err)
        job = _Job(url=url, max_bytes=max_bytes)
        with self._lock:
            self._q.put(job)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True, name="browser-fetch")
                self._thread.start()
        end = time.monotonic() + (timeout or (self.approve_timeout + self.nav_timeout * 3 + 60))
        while not job.done.wait(0.2):
            if stop is not None and stop.is_set():
                job.abandoned = True
                return BrowserFile(error="Skipped by you.", skipped=True)
            if time.monotonic() > end:
                job.abandoned = True
                return BrowserFile(error="The browser did not answer in time.")
        return job.result

    def skip_current(self) -> bool:
        """Skip the URL the browser is working on right now (e.g. one waiting for a verification page to be approved)."""
        job = self._current
        if job is None:
            return False
        job.abandoned = True
        return True

    def abort(self) -> None:
        """Stop waiting: queued requests are answered 'stopped' and a wait for approval ends."""
        self._abort.set()

    def reset(self) -> None:
        self._abort.clear()

    def state(self) -> dict:
        w = self._waiting
        return {"waiting_for_user": bool(w), "url": (w or {}).get("url", ""), "can_skip": self._current is not None, "seconds_left": max(0, int((w or {}).get("until", 0) - time.monotonic())) if w else 0}

    def close(self) -> None:
        with self._lock:
            t = self._thread
        if t and t.is_alive():
            self._q.put(None)
            t.join(15)

    # ── the worker thread owns the browser ──────────────────────────────────
    def _worker(self) -> None:
        pw = ctx = None
        try:
            while True:
                try:
                    job = self._q.get(timeout=self.idle_close)
                except queue.Empty:
                    with self._lock:
                        if self._q.empty():
                            self._thread = None
                            return
                    continue
                if job is None:
                    return
                if self._abort.is_set():
                    job.result = BrowserFile(error="Stopped.")
                    job.done.set()
                    continue
                if job.abandoned:                                       # skipped while it was still queued
                    job.result = BrowserFile(error="Skipped by you.", skipped=True)
                    job.done.set()
                    continue
                self._current = job
                try:
                    if ctx is None:
                        pw, ctx = self._open()
                    job.result = self._fetch(ctx, job)
                except BrowserUnavailable as e:
                    job.result = BrowserFile(error=str(e))
                except Exception as e:
                    msg = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
                    log.warning("Browser fetch failed for %s: %s", job.url, msg)
                    job.result = BrowserFile(error=f"The browser could not read it: {msg}")
                    if "closed" in str(e).lower():                       # the person closed the window: start a new one next time
                        ctx, self._pages = None, {}
                finally:
                    self._waiting = self._current = None
                    job.done.set()
        finally:
            self._pages = {}
            for closer in (lambda: ctx and ctx.close(), lambda: pw and pw.stop()):
                try:
                    closer()
                except Exception:
                    pass
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _open(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable("Playwright is not installed. Run `pip install playwright` (it uses the Edge or Chrome "
                                     "already on this computer).") from e
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        errors = []
        for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                ctx = pw.chromium.launch_persistent_context(str(self.profile_dir), headless=self.headless, **kw)
                ctx.set_default_timeout(int(self.nav_timeout * 1000))
                log.info("Browser window opened for blocked sites (%s, %s)", kw.get("channel", "chromium"), "hidden" if self.headless else "visible")
                return pw, ctx
            except Exception as e:
                errors.append(str(e).splitlines()[0][:120])
        pw.stop()
        raise BrowserUnavailable("No browser window could be opened. Install Microsoft Edge or Google Chrome. "
                                 f"Details: {errors[-1] if errors else 'unknown'}")

    # ── one file ────────────────────────────────────────────────────────────
    def _fetch(self, ctx, job: _Job) -> BrowserFile:
        origin = _origin(job.url)
        page = self._pages.get(origin)
        if page is None or page.is_closed():
            page = ctx.new_page()
            self._pages[origin] = page
        approved = False
        if not page.url.startswith(origin):
            ok, approved = self._open_page(page, origin + "/", job)
            if not ok:
                return BrowserFile(error="Skipped by you.", skipped=True) if job.abandoned else self._blocked(job, approved)
        if job.abandoned:
            return BrowserFile(error="Skipped by you.", skipped=True)
        out = self._read(page, job)
        if out.blocked and self.can_wait and not self._abort.is_set() and not job.abandoned:
            # the file's own address may show the check: open it so it can be approved there, then read again
            ok, ap2 = self._open_page(page, job.url, job)
            approved = approved or ap2
            if ok:
                out = self._read(page, job)
            elif job.abandoned:
                return BrowserFile(error="Skipped by you.", skipped=True)
        out.approved_by_user = approved
        if out.blocked and out.shield:
            out.error = self._blocked(job, approved).error
        return out

    def _open_page(self, page, url: str, job: _Job) -> tuple[bool, bool]:
        """Load `url` in the window. Returns (usable, approved_by_user)."""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.nav_timeout * 1000))
        except Exception as e:
            if "Download is starting" not in str(e) and "ERR_ABORTED" not in str(e):      # a file address: fine, the page is on the site's origin
                if not page.url.startswith(_origin(url)):
                    log.info("Browser could not open %s: %s", url, str(e).splitlines()[0][:120])
                    return False, False
        shield = self._shield(page)
        if not shield:
            return True, False
        if not self.can_wait:
            return False, False
        log.info("%s verification shown for %s - waiting for the person to approve it in the window", shield, url)
        self._waiting = {"url": url, "until": time.monotonic() + self.approve_timeout}
        try:
            page.bring_to_front()
        except Exception:
            pass
        end = time.monotonic() + self.approve_timeout
        while time.monotonic() < end and not self._abort.is_set() and not job.abandoned:
            page.wait_for_timeout(500)
            if not self._shield(page):
                self._waiting = None
                page.wait_for_timeout(800)
                return True, True
        self._waiting = None
        return False, False

    @staticmethod
    def _shield(page) -> str:
        try:
            return detect_bot_protection(page.content())
        except Exception:
            return ""

    def _read(self, page, job: _Job) -> BrowserFile:
        try:
            res = page.evaluate(_JS_FETCH, [job.url, job.max_bytes])
        except Exception as e:
            return BrowserFile(error=f"The browser could not fetch it: {str(e).splitlines()[0][:140]}")
        status = int(res.get("status") or 0)
        final = res.get("url") or job.url
        err = self.validate(final)
        if err:
            return BrowserFile(error=f"Redirected to an unsafe address: {err}", final_url=final)
        out = BrowserFile(status=status, content_type=res.get("type", ""), disposition=res.get("disposition", ""), final_url=final,
                          size=int(res.get("size") or res.get("length") or 0))
        if res.get("tooBig"):
            out.ok, out.too_big = status < 400, True
            return out
        data = base64.b64decode(res.get("b64") or "")
        out.size = len(data)
        if status >= 400:
            out.blocked = status in BLOCKED_CODES
            out.error = f"HTTP {status}"
            if "html" in out.content_type.lower() and detect_bot_protection(data[:200000].decode("utf-8", "replace")):
                out.blocked = out.shield = True
            return out
        out.ok, out.data = True, data
        if "html" in out.content_type.lower() or data[:15].lower().startswith((b"<!doctype", b"<html")):
            out.html = data.decode("utf-8", "replace")
            if detect_bot_protection(out.html):
                out.ok, out.blocked, out.shield, out.error = False, True, True, "The site answered with a verification page."
        return out

    def _blocked(self, job: _Job, approved: bool) -> BrowserFile:
        why = ("the browser window is hidden, so a verification page cannot be approved" if not self.can_wait
               else "the verification page was not approved in the browser window in time")
        return BrowserFile(blocked=True, error=f"The site shows a verification page and {why}.")


def filename_of(bf: BrowserFile) -> str:
    m = re.search(r"filename\*?=(?:[^'\";]*'')?\"?([^\";]+)\"?", bf.disposition or "", re.I)
    if m:
        return unquote(m.group(1).strip())
    return unquote(Path(urlparse(bf.final_url).path).name)


_default: BrowserFetcher | None = None
_default_lock = threading.Lock()


def default_fetcher(headless: bool | None = None, validate: Callable[[str], str] | None = None) -> BrowserFetcher:
    """The process-wide browser: one window, one profile (an approval is remembered for the next run)."""
    global _default
    with _default_lock:
        if _default is None:
            import os
            hide = headless if headless is not None else os.environ.get("URL_ANALYZER_BROWSER_HEADLESS", "") == "1"
            _default = BrowserFetcher(headless=hide, validate=validate)
        return _default
