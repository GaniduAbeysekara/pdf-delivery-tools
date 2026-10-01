"""HTTP layer of the engine: URL validation, safe redirect following, bounded body reads."""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
import urllib3

from ..config import AppConfig, load_config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REDIRECT_CODES = {301, 302, 303, 307, 308}
HTTP_MESSAGES = {
    400: "Bad request", 401: "Authentication required", 403: "Access denied", 404: "Not found",
    405: "Method not allowed", 410: "Gone", 429: "Rate limited", 500: "Server error",
    502: "Bad gateway", 503: "Service unavailable", 504: "Gateway timeout",
}
CHUNK = 64 * 1024


@dataclass
class AnalyzerSettings:
    connect_timeout: float = 10
    read_timeout: float = 30
    total_deadline: float = 120        # hard cap per URL, including downloads
    max_redirects: int = 10
    max_download_bytes: int = 50 * 1024 * 1024
    max_workers: int = 5               # overall concurrency
    per_host_limit: int = 2            # be polite to a single website
    retries: int = 1                   # extra attempts for timeouts / connection errors / HTTP 429
    retry_delay: float = 0.5           # exponential back-off base (seconds)
    request_delay: float = 0.0         # minimum gap between requests to the same host (politeness)
    browser_fallback: bool = False     # render pages with a browser when static HTML shows no document links
    allow_private_hosts: bool = False  # block loopback / private / link-local targets
    cache_ttl: float = 3600
    user_agent: str = ""
    headers: dict | None = None

    @classmethod
    def from_config(cls, cfg: AppConfig | None = None, **overrides) -> "AnalyzerSettings":
        cfg = cfg or load_config()
        base = dict(read_timeout=cfg.request.timeout, max_workers=cfg.processing.workers,
                    retries=cfg.request.retries, retry_delay=max(cfg.request.delay, 0.1),
                    request_delay=cfg.request.delay, browser_fallback=cfg.browser.enabled,
                    max_download_bytes=cfg.processing.max_pdf_bytes, user_agent=cfg.request.user_agent,
                    headers=dict(cfg.request.headers))
        base.update(overrides)
        return cls(**base)

    def fingerprint(self) -> str:
        """Settings that change what an analysis produces (used in the cache key)."""
        return f"{self.max_download_bytes}|{self.max_redirects}|{self.allow_private_hosts}"


class FetchError(Exception):
    """Expected, user-presentable failure."""


def resolve_host(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, None)]


def header(headers, name: str) -> str:
    """Case-insensitive header lookup that tolerates plain dicts."""
    if headers is None:
        return ""
    try:
        v = headers.get(name)
        if v is None:
            lname = name.lower()
            v = next((val for k, val in headers.items() if k.lower() == lname), "")
        return str(v or "")
    except Exception:
        return ""


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HTTPAnalyzer:
    def __init__(self, settings: AnalyzerSettings,
                 session_factory: Callable[[], requests.Session] | None = None,
                 resolver: Callable[[str], list[str]] | None = None):
        self.s = settings
        self._session_factory = session_factory or self._default_session
        self._resolver = resolver or resolve_host
        self._local = threading.local()
        self._host_sems: dict[str, threading.Semaphore] = {}
        self._sem_lock = threading.Lock()
        self._next_slot: dict[str, float] = {}
        self._throttle_lock = threading.Lock()

    def _default_session(self) -> requests.Session:
        sess = requests.Session()
        sess.headers.update({"User-Agent": self.s.user_agent, **(self.s.headers or {})})
        return sess

    def session(self):
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        return self._local.session

    def host_semaphore(self, host: str) -> threading.Semaphore:
        with self._sem_lock:
            return self._host_sems.setdefault(host, threading.Semaphore(self.s.per_host_limit))

    def throttle(self, host: str) -> None:
        """Keep at least `request_delay` seconds between requests to the same host."""
        delay = self.s.request_delay
        if delay <= 0 or not host:
            return
        with self._throttle_lock:
            now = time.monotonic()
            at = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = at + delay
        if at > now:
            time.sleep(at - now)

    def validate(self, url: str) -> str:
        """Return an error message, or '' if the URL is safe to request."""
        if not url or not url.strip():
            return "Empty URL"
        if len(url) > 2048 or re.search(r"[\x00-\x1f\s]", url.strip()):
            return "Invalid URL"
        try:
            p = urlparse(url.strip())
            host = p.hostname
        except ValueError:
            return "Invalid URL"
        if not p.scheme:
            return "Invalid URL (not a web address)"
        if p.scheme.lower() not in ("http", "https"):
            return f"Unsupported URL scheme '{p.scheme}' (only http and https are allowed)"
        if not host:
            return "Invalid URL (no host name)"
        if self.s.allow_private_hosts:
            return ""
        try:
            addrs = [host] if _is_ip(host) else self._resolver(host)
        except socket.gaierror:
            return "DNS lookup failed (host not found)"
        except Exception as e:
            return f"Could not resolve host: {str(e)[:80]}"
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a.split("%")[0])
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return "Blocked: the URL points to a private or local network address"
        return ""

    def request(self, url: str, deadline: float, notes: list[str]):
        """GET following redirects manually so every hop is validated.
        Returns (response, first_status, redirect_count, final_url)."""
        sess, verify, hops, first, cur = self.session(), True, 0, None, url
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError
            if hops:
                err = self.validate(cur)
                if err:
                    raise FetchError(f"Redirected to an unsafe address: {err}")
            self.throttle((urlparse(cur).hostname or "").lower())
            try:
                resp = sess.request("GET", cur, timeout=(self.s.connect_timeout, self.s.read_timeout),
                                    allow_redirects=False, stream=True, verify=verify)
            except requests.exceptions.SSLError:
                if verify:
                    verify = False
                    notes.append("SSL certificate could not be verified (analysed anyway)")
                    continue
                raise
            if first is None:
                first = resp.status_code
            loc = header(resp.headers, "Location")
            if resp.status_code in REDIRECT_CODES and loc:
                resp.close()
                hops += 1
                if hops > self.s.max_redirects:
                    raise FetchError("Too many redirects")
                cur = urljoin(cur, loc)
                continue
            return resp, first, hops, cur

    @staticmethod
    def read(it, first: bytes, cap: int, deadline: float) -> tuple[bytes, bool]:
        """Read the rest of a streamed body up to `cap` bytes. Returns (data, truncated)."""
        chunks, total = [first], len(first)
        if total >= cap:
            return first, True
        for chunk in it:
            if time.monotonic() > deadline:
                raise TimeoutError
            chunks.append(chunk)
            total += len(chunk)
            if total >= cap:
                return b"".join(chunks), True
        return b"".join(chunks), False

    def failure_message(self, exc: Exception) -> tuple[str, bool]:
        """Map a network exception to (user message, retryable)."""
        if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
            return f"Timeout after {int(self.s.read_timeout)} seconds", True
        if isinstance(exc, requests.exceptions.TooManyRedirects):
            return "Too many redirects", False
        if isinstance(exc, requests.exceptions.SSLError):
            return f"SSL error: {str(exc)[:100]}", False
        if isinstance(exc, requests.exceptions.ConnectionError):
            msg = str(exc).lower()
            if any(t in msg for t in ("getaddrinfo", "name or service not known", "nodename nor servname",
                                      "failed to resolve", "name resolution")):
                return "DNS lookup failed (host not found)", True
            return "Connection failed: the server could not be reached", True
        if isinstance(exc, FetchError):
            return str(exc), False
        return f"Unexpected error: {str(exc)[:120]}", False
