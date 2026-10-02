"""THE URL analysis engine.

Every input path (pasted URLs, Excel batch upload, books selected in the Daily Backlog, the CLI) calls
`analyze_urls()`. There is exactly one implementation of the checks, so an improvement here
(redirects, page count, language, document detection...) applies to all of them.

Per URL:  check it works -> direct PDF/document?  -> otherwise a landing page: find document links,
          verify them, and if there is exactly ONE follow it and read its metadata (SINGLE);
          several -> MULTIPLE (none is chosen); none -> NONE.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests

from ..backlog.normalize import extract_domain
from ..html_analyzer import (detect_bot_protection, document_base_url, find_candidate_links, find_document_links,
                             find_embedded_documents)
from ..logger import log
from .browser_fallback import PlaywrightRenderer
from .document_analyzer import (DIRECT_TYPES, detect_content_type, disposition_filename, human_size,
                                inspect_document, parse_http_date)
from .http_analyzer import (CHUNK, HTTP_MESSAGES, REDIRECT_CODES, AnalyzerSettings, HTTPAnalyzer, header)
from ..utils import url_extension
from .inputs import document_key, url_key
from .models import BookRecord, URLAnalysisResult

SMALL_BODY_CAP = 2 * 1024 * 1024
MAX_FRAMES = 6          # frames (iframe/embed/object) fetched per page to see whether they hold a document
MAX_FRAME_DEPTH = 2     # page -> frame page -> frame page; deeper nesting is not followed
MAX_NESTED_PROBES = 4   # download-style links verified inside a frame page
MAX_PROBES = 12         # unverified "download" style links checked per landing page (best candidates first)
MAX_LISTED_LINKS = 200
MAX_SUPPLIED_HTML = 5 * 1024 * 1024
Records = Iterable["BookRecord | dict"]


class RateLimited(Exception):
    """HTTP 429: wait `seconds` and try again."""
    def __init__(self, seconds: float):
        super().__init__("rate limited")
        self.seconds = seconds


def classify_status(code: int | None) -> str:
    """WORKING / NOT_WORKING / BLOCKED / ERROR from an HTTP status code."""
    if not code:
        return "ERROR"
    if code in (401, 403, 429):
        return "BLOCKED"
    if 200 <= code < 400:
        return "WORKING"
    return "NOT_WORKING"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class URLAnalyzer:
    def __init__(self, settings: AnalyzerSettings | None = None,
                 session_factory: Callable[[], requests.Session] | None = None,
                 resolver: Callable[[str], list[str]] | None = None,
                 renderer=None):
        self.s = settings or AnalyzerSettings.from_config()
        self.http = HTTPAnalyzer(self.s, session_factory, resolver)
        self.renderer = renderer or (PlaywrightRenderer(self.s.read_timeout) if self.s.browser_fallback else None)
        self._cache: dict[tuple[str, str], tuple[float, URLAnalysisResult]] = {}
        self._cache_lock = threading.Lock()

    def validate(self, url: str) -> str:
        return self.http.validate(url)

    # ── cache ───────────────────────────────────────────────────────────────
    def _cache_key(self, url: str, kind: str = "") -> tuple[str, str]:
        return url_key(url), self.s.fingerprint() + kind

    def _cache_get(self, key) -> URLAnalysisResult | None:
        with self._cache_lock:
            hit = self._cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < self.s.cache_ttl else None

    def _cache_put(self, key, result: URLAnalysisResult) -> None:
        transient = result.analysis_status == "Failed" and result.error.startswith(
            ("Timeout", "Connection failed", "DNS lookup failed", "Unexpected error", "HTTP 429"))
        if not transient:                      # don't remember network hiccups
            with self._cache_lock:
                self._cache[key] = (time.time(), result)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # ── single URL ──────────────────────────────────────────────────────────
    def analyze(self, url: str, force: bool = False) -> URLAnalysisResult:
        """Analyse one URL. Never raises; failures are reported in the result.
        Cached results are returned with `cached=True` unless `force` asks for a fresh check."""
        url = (url or "").strip()
        key = self._cache_key(url)
        if not force:
            hit = self._cache_get(key)
            if hit:
                res = hit.with_record(BookRecord(url=url))
                res.cached = True
                return res
        result = self._analyze_uncached(url)
        result.analyzed_at, result.cached = _now(), False
        self._cache_put(key, result)
        return result

    def _analyze_document(self, url: str, deadline: float) -> URLAnalysisResult:
        """Analyse a document linked from a landing page (never follows links again)."""
        key = self._cache_key(url, "|doc")
        hit = self._cache_get(key)
        if hit:
            return hit
        res = self._analyze_uncached(url, follow=False, nested=True, deadline=deadline)
        res.analyzed_at = _now()
        self._cache_put(key, res)
        return res

    @contextmanager
    def _host_slot(self, host: str, nested: bool):
        # nested requests (links followed from a page whose host slot is already held) never take another slot:
        # holding one slot while waiting for another could deadlock a batch.
        if nested:
            yield
        else:
            with self.http.host_semaphore(host):
                yield

    @staticmethod
    def _sleep(seconds: float, deadline: float) -> None:
        time.sleep(max(0.0, min(seconds, deadline - time.monotonic())))

    def _analyze_uncached(self, url: str, follow: bool = True, nested: bool = False,
                          deadline: float | None = None) -> URLAnalysisResult:
        def fresh() -> URLAnalysisResult:
            return URLAnalysisResult(url=url, domain=extract_domain(url) if url else "")

        r = fresh()
        err = self.http.validate(url)
        if err:
            r.error, r.status, r.url_type = err, "ERROR", "NOT_WORKING"
            return r
        host = (urlparse(url).hostname or "").lower()
        deadline = deadline or time.monotonic() + self.s.total_deadline
        attempts = self.s.retries + 1
        with self._host_slot(host, nested):
            for attempt in range(attempts):
                r = fresh()
                last = attempt == attempts - 1
                try:
                    return self._fetch_and_inspect(url, r, deadline, follow, last)
                except RateLimited as e:
                    r.status, r.error = "BLOCKED", "HTTP 429 - Rate limited"
                    log.warning("429 for %s - waiting %.1fs", url, e.seconds)
                    self._sleep(min(e.seconds, 30), deadline)
                except Exception as e:   # one bad URL must never stop a batch
                    message, retryable = self.http.failure_message(e)
                    if message.startswith("Unexpected error"):
                        log.exception("Unexpected error analysing %s", url)
                    r.error = message
                    r.status = "TIMEOUT" if message.startswith("Timeout") else "ERROR"
                    if not retryable or last or time.monotonic() > deadline:
                        break
                    log.info("Retry %d for %s (%s)", attempt + 1, url, message)
                    self._sleep(self.s.retry_delay * (2 ** attempt), deadline)
        r.analysis_status, r.url_type = "Failed", "NOT_WORKING"
        return r

    def _fetch_and_inspect(self, url: str, r: URLAnalysisResult, deadline: float, follow: bool,
                           last_attempt: bool) -> URLAnalysisResult:
        log.info("Processing: %s", url)
        resp, first_status, hops, final_url = self.http.request(url, deadline, r.notes)
        try:
            code = resp.status_code
            r.http_status, r.final_status, r.redirects, r.final_url = first_status, code, hops, final_url
            r.domain = extract_domain(url)
            ct_raw = header(resp.headers, "Content-Type")
            r.mime_type = ct_raw.split(";")[0].strip().lower()
            if code == 429 and not last_attempt:
                try:
                    wait = float(header(resp.headers, "Retry-After").strip() or 0)
                except ValueError:
                    wait = 0.0
                raise RateLimited(wait or self.s.retry_delay)
            if code >= 400 or code in REDIRECT_CODES:
                label = HTTP_MESSAGES.get(code, "Server error" if code >= 500 else "Request failed")
                r.error = (f"HTTP {code} - Redirect without a destination" if code in REDIRECT_CODES
                           else f"HTTP {code} - {label}")
                r.status = "NOT_WORKING" if code in REDIRECT_CODES else classify_status(code)
                r.url_type, r.analysis_status = "NOT_WORKING", "Failed"
                log.info("Status: %s -> %s", code, r.status)
                return r
            r.status = "WORKING"
            if code == 202:
                r.notes.append("Server answered 202 Accepted - this is often a bot-protection page, not the real content")

            clen_raw = header(resp.headers, "Content-Length").strip()
            clen = int(clen_raw) if clen_raw.isdigit() else None
            it = resp.iter_content(CHUNK)
            head = next(it, b"") or b""
            r.content_type = detect_content_type(ct_raw, final_url, head, disposition_filename(header(resp.headers, "Content-Disposition")))
            r.direct_document = r.content_type in DIRECT_TYPES
            modified = parse_http_date(header(resp.headers, "Last-Modified"))
            cap = self.s.max_download_bytes
            r.analysis_status, r.file_size_bytes = "Success", clen
            ext = urlparse(final_url).path.lower().rsplit(".", 1)[-1]
            html_text: str | None = None

            if r.content_type in ("PDF", "DOCX"):
                r.url_type = "DIRECT_PDF" if r.content_type == "PDF" else "DIRECT_DOCUMENT"
                r.document_link_type, r.document_url = "DIRECT", final_url
                if clen is not None and clen > cap:
                    self._partial(r, f"Document is larger than {human_size(cap)} - page count and language not retrieved")
                else:
                    data, truncated = self.http.read(it, head, cap, deadline)
                    r.file_size_bytes = clen if clen is not None else (None if truncated else len(data))
                    if truncated:
                        self._partial(r, f"Document is larger than {human_size(cap)} - page count and language not retrieved")
                    else:
                        info = inspect_document(r.content_type, data)
                        r.language, r.text_extraction_status = info.language, info.text_status
                        r.page_count = str(info.pages) if info.pages is not None else "Unknown"
                        if info.problem:
                            self._partial(r, info.problem)
                        modified = modified or info.modified
            elif r.content_type == "DOC":
                r.url_type, r.document_link_type, r.document_url = "DIRECT_DOCUMENT", "DIRECT", final_url
                r.page_count = "Unknown"
                if clen is None:
                    data, truncated = self.http.read(it, head, SMALL_BODY_CAP, deadline)
                    r.file_size_bytes = None if truncated else len(data)
            elif r.content_type == "HTML":
                r.url_type = "LANDING_PAGE"
                data, truncated = self.http.read(it, head, SMALL_BODY_CAP, deadline)
                if clen is None:
                    r.file_size_bytes = None if truncated else len(data)
                html_text = data.decode("utf-8", "replace") if data else ""
            else:
                r.url_type = "UNKNOWN"
                if clen is None:
                    data, truncated = self.http.read(it, head, SMALL_BODY_CAP, deadline)
                    r.file_size_bytes = None if truncated else len(data)

            r.file_size = human_size(r.file_size_bytes)
            r.last_modified = modified or "Unknown"
            if r.language == "N/A" and r.content_type in ("PDF", "DOCX"):
                r.language = "Unknown"
            log.info("Status: %s | URL type: %s", code, r.url_type)

            if html_text is not None and follow:
                self._landing(r, html_text, final_url, deadline)
            if (not r.direct_document and ext in ("pdf", "doc", "docx") and r.content_type in ("HTML", "Other")
                    and r.document_link_type in ("", "NONE")):
                self._partial(r, f"URL looks like a .{ext} file but the server returned {r.content_type}")
            return r
        finally:
            resp.close()

    # ── landing pages ───────────────────────────────────────────────────────
    def _extract_links(self, html: str, base_url: str, deadline: float, depth: int = 0) -> tuple[list[str], dict[str, set[str]]]:
        """Unique document URLs on a page and HOW each was found.

        Sources: documents shown in frames (iframe / embed / object - viewers unwrapped, the frame address verified by
        what the server returns, and an HTML page inside a frame searched too), extension-confirmed links and buttons,
        and download-style links whose destination is verified (by Content-Type) to be a document.
        Returns (urls, {url: {"iframe"} | {"link"} | {"iframe", "link"}}). The same document reached two ways is ONE url."""
        urls: list[str] = []
        how: dict[str, set[str]] = {}

        def add(u: str, via: str) -> None:
            k = document_key(u)
            if k not in how:
                how[k] = set()
                urls.append(u)
            how[k].add(via)

        try:
            if depth < MAX_FRAME_DEPTH:
                for frame in find_embedded_documents(html, base_url, MAX_FRAMES):
                    self._read_frame(frame["target"], deadline, depth, add)
            for link in find_document_links(html, base_url, include_frames=False):
                add(link.url, "link")
            probes = MAX_PROBES if depth == 0 else MAX_NESTED_PROBES
            for cand in find_candidate_links(html, base_url, known=set(urls), include_frames=False)[:probes]:
                if document_key(cand) in how:
                    continue
                probed = self._probe(cand, deadline)
                if probed and probed[0] in DIRECT_TYPES:
                    add(probed[1], "link")
        except Exception as e:
            log.warning("Link discovery failed for %s: %s", base_url, e)
        return urls, {u: how[document_key(u)] for u in urls}

    def _read_frame(self, target: str, deadline: float, depth: int, add: Callable[[str, str], None]) -> None:
        """One frame address: a document extension is taken as is; anything else is fetched to see what it is
        (a document served without an extension, or an HTML page that contains the document)."""
        if url_extension(target) in (".pdf", ".doc", ".docx"):
            add(target, "iframe")
            return
        frame = self._fetch_frame(target, deadline)
        if not frame:
            return
        ctype, final, inner = frame
        if ctype in DIRECT_TYPES:
            add(final, "iframe")
        elif ctype == "HTML" and inner:
            found, _ = self._extract_links(inner, document_base_url(inner, final), deadline, depth + 1)
            for u in found:
                add(u, "iframe")

    def _fetch_frame(self, url: str, deadline: float) -> tuple[str, str, str] | None:
        """(content_type, final_url, html) of a frame address; html only for HTML pages."""
        if self.http.validate(url):
            return None
        try:
            resp, _, _, final = self.http.request(url, deadline, [])
        except Exception:
            return None
        try:
            if resp.status_code >= 400:
                return None
            it = resp.iter_content(CHUNK)
            head = next(it, b"") or b""
            ctype = detect_content_type(header(resp.headers, "Content-Type"), final, head,
                                        disposition_filename(header(resp.headers, "Content-Disposition")))
            if ctype != "HTML":
                return ctype, final, ""
            data, _ = self.http.read(it, head, SMALL_BODY_CAP, deadline)
            return ctype, final, data.decode("utf-8", "replace")
        except Exception:
            return None
        finally:
            resp.close()

    def _probe(self, url: str, deadline: float) -> tuple[str, str] | None:
        """(content_type, final_url) of a link, from a streamed GET that reads only the first chunk."""
        if self.http.validate(url):
            return None
        try:
            resp, _, _, final = self.http.request(url, deadline, [])
        except Exception:
            return None
        try:
            if resp.status_code >= 400:
                return None
            head = next(resp.iter_content(CHUNK), b"") or b""
            return detect_content_type(header(resp.headers, "Content-Type"), final, head,
                                       disposition_filename(header(resp.headers, "Content-Disposition"))), final
        finally:
            resp.close()

    def _landing(self, r: URLAnalysisResult, html: str, final_url: str, deadline: float, supplied: bool = False) -> None:
        urls, how = self._extract_links(html, final_url, deadline)
        shield = "" if urls else detect_bot_protection(html)
        if not urls and self.renderer is not None and not supplied:
            rendered = self.renderer.render(final_url)
            if rendered:
                urls, how = self._extract_links(rendered, final_url, deadline)
                if urls:
                    shield = ""
                    r.notes.append("Document links were found by rendering the page in a browser")
                else:
                    shield = detect_bot_protection(rendered)       # the rendered page tells the truth
        if shield and not urls:
            # A verification page has no links by design: "no documents" would be a false answer.
            r.status, r.url_type, r.analysis_status = "BLOCKED", "UNKNOWN", "Unknown"
            r.document_link_type, r.document_link_count, r.document_urls = "", None, []
            if supplied:
                r.error = (f"The pasted HTML is a {shield} verification page, not the real page. Copy the page source "
                           "again after the real page has finished loading in your browser.")
            else:
                r.error = (f"Blocked by bot protection ({shield}): the server returned a verification page instead of the "
                           "real page, so its document links could not be checked. "
                           + ("The browser fallback was blocked too." if self.renderer is not None
                              else "Open the page in a browser and paste its source here (Page Source), "
                                   "or enable the browser fallback (browser.enabled in config.yaml)."))
            log.warning("Bot protection (%s) on %s", shield, final_url)
            return
        r.document_urls = urls[:MAX_LISTED_LINKS]
        r.document_link_count = len(urls)
        log.info("Document links found: %d on %s", len(urls), final_url)
        via = set().union(*how.values()) if how else set()
        if "iframe" in via and "link" in via:         # a viewer frame AND a download link/button: same document or not?
            r.notes.append("The embedded viewer (iframe) and the download link/button point to the same document - reported as SINGLE"
                           if len(urls) == 1 else
                           f"The embedded viewer (iframe) and the download link/button do not point to the same document "
                           f"({len(urls)} different documents) - reported as MULTIPLE")
        elif "iframe" in via:
            r.notes.append("The document is shown in an embedded frame (iframe)" if len(urls) == 1 else
                           f"{len(urls)} different documents are shown in embedded frames (iframe)")
        if not urls:
            r.document_link_type = "NONE"
        elif len(urls) == 1:
            r.document_link_type = "SINGLE"
            self._follow(r, urls[0], deadline)
        else:
            r.document_link_type = "MULTIPLE"     # never pick one at random

    def _follow(self, r: URLAnalysisResult, doc_url: str, deadline: float) -> None:
        """Analyse the single linked document exactly as if it had been given directly."""
        log.info("Following document URL: %s", doc_url)
        sub = self._analyze_document(doc_url, deadline)
        if sub.status == "WORKING" and sub.content_type in DIRECT_TYPES:
            r.document_url = sub.final_url or doc_url
            r.page_count, r.language, r.last_modified = sub.page_count, sub.language, sub.last_modified
            r.file_size_bytes, r.file_size = sub.file_size_bytes, sub.file_size
            r.text_extraction_status = sub.text_extraction_status
            r.notes.extend(n for n in sub.notes if n not in r.notes)
            if sub.analysis_status != "Success":
                self._partial(r, f"Linked document: {sub.error}")
                r.page_count = sub.page_count
        else:
            r.document_url = doc_url
            reason = sub.error or f"the link returned {sub.content_type or 'no document'}"
            self._partial(r, f"Linked document could not be analysed: {reason}")
            r.page_count = "Unknown"

    @staticmethod
    def _partial(r: URLAnalysisResult, message: str) -> None:
        """URL works but metadata is incomplete."""
        r.analysis_status = "Unknown"
        r.error = message
        if r.content_type in ("PDF", "DOCX"):
            r.page_count = "Unknown"

    # ── page source supplied by the user ────────────────────────────────────
    def analyze_html(self, url: str, html: str) -> URLAnalysisResult:
        """Analyse a landing page from HTML the user copied out of their own browser (useful when the site shows
        automated clients a verification page). The page is NOT requested; its document links are found and the
        single linked document, if any, is fetched and analysed as usual. `url` resolves relative links."""
        url = (url or "").strip()
        r = URLAnalysisResult(url=url, domain=extract_domain(url) if url else "")
        err = self.http.validate(url)
        if err:
            r.error, r.status, r.url_type = err, "ERROR", "NOT_WORKING"
            return r
        html = (html or "")[:MAX_SUPPLIED_HTML]
        if not html.strip() or "<" not in html:
            r.error, r.status, r.url_type = "No page source was supplied (paste the page's HTML)", "ERROR", "NOT_WORKING"
            return r
        r.status, r.url_type, r.content_type, r.final_url, r.analysis_status = "WORKING", "LANDING_PAGE", "HTML", url, "Success"
        r.file_size_bytes = len(html.encode("utf-8", "replace"))
        r.file_size = human_size(r.file_size_bytes)
        r.notes.append("Analysed from page source supplied by the user - the page itself was not requested")
        try:
            self._landing(r, html, document_base_url(html, url), time.monotonic() + self.s.total_deadline, supplied=True)
        except Exception as e:
            log.exception("Analysing supplied page source failed for %s", url)
            r.error, r.analysis_status = f"Unexpected error: {str(e)[:120]}", "Failed"
        r.analyzed_at = _now()
        return r

    # ── batches ─────────────────────────────────────────────────────────────
    def analyze_batch(self, records: list[BookRecord],
                      on_result: Callable[[int, URLAnalysisResult], None] | None = None,
                      force: bool = False) -> list[URLAnalysisResult]:
        """Analyse many records concurrently (each distinct URL once). Results keep the input order."""
        results: list[URLAnalysisResult | None] = [None] * len(records)
        groups: dict[str, list[int]] = {}
        supplied: list[int] = []                     # records that come with the page's HTML: analysed individually
        for i, rec in enumerate(records):
            (supplied.append(i) if rec.page_html else groups.setdefault(url_key(rec.url), []).append(i))
        if not groups and not supplied:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(self.s.max_workers, len(groups) + len(supplied)))) as pool:
            futures = {pool.submit(self.analyze, records[idxs[0]].url, force): idxs for idxs in groups.values()}
            futures.update({pool.submit(self.analyze_html, records[i].url, records[i].page_html): [i] for i in supplied})
            for fut in as_completed(futures):
                try:
                    base = fut.result()
                except Exception as e:  # defensive: analyze() should never raise
                    base = URLAnalysisResult(url="", error=f"Unexpected error: {str(e)[:120]}")
                for i in futures[fut]:
                    res = base.with_record(records[i])
                    results[i] = res
                    if on_result:
                        on_result(i, res)
        return results  # type: ignore[return-value]


# ── module-level interface ──────────────────────────────────────────────────
_default: URLAnalyzer | None = None
_default_lock = threading.Lock()


def default_analyzer() -> URLAnalyzer:
    """The process-wide analyzer: one shared cache and one set of per-host limits for every input path."""
    global _default
    with _default_lock:
        if _default is None:
            _default = URLAnalyzer()
        return _default


def as_records(records: Records) -> list[BookRecord]:
    return [r if isinstance(r, BookRecord) else BookRecord.from_dict(r) for r in records]


def analyze_urls(records: Records, *, analyzer: URLAnalyzer | None = None, force_refresh: bool = False,
                 on_result: Callable[[int, URLAnalysisResult], None] | None = None) -> list[URLAnalysisResult]:
    """Analyse records (dicts with `url` and optional `document_id`, `book_title`, ... or BookRecords).

    The one entry point used by manual paste, Excel upload, the Daily Backlog hand-off and the CLI.
    """
    return (analyzer or default_analyzer()).analyze_batch(as_records(records), on_result, force_refresh)
