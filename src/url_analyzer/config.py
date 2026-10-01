"""Configuration loading and defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml


@dataclass
class RequestConfig:
    timeout: int = 30
    retries: int = 3
    delay: float = 0.5
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
    headers: dict = field(default_factory=lambda: {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })


@dataclass
class ProcessingConfig:
    workers: int = 5
    url_column: str = ""       # empty = auto-detect
    max_pdf_bytes: int = 50 * 1024 * 1024   # 50 MB max download


@dataclass
class DocumentConfig:
    extensions: List[str] = field(default_factory=lambda: [".pdf", ".doc", ".docx"])
    pdf_content_types: List[str] = field(default_factory=lambda: [
        "application/pdf", "application/x-pdf",
    ])
    doc_content_types: List[str] = field(default_factory=lambda: [
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ])


@dataclass
class BrowserConfig:
    enabled: bool = False
    timeout: int = 30


@dataclass
class AppConfig:
    request: RequestConfig = field(default_factory=RequestConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    documents: DocumentConfig = field(default_factory=DocumentConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)


def load_config(path: str | None = None) -> AppConfig:
    """Load config from YAML file, falling back to defaults."""
    if path is None:
        default_path = Path(__file__).parent.parent.parent / "config.yaml"
        path = str(default_path)

    if not os.path.exists(path):
        return AppConfig()

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    cfg = AppConfig()

    req = data.get("request", {})
    cfg.request.timeout = req.get("timeout", cfg.request.timeout)
    cfg.request.retries = req.get("retries", cfg.request.retries)
    cfg.request.delay = req.get("delay", cfg.request.delay)
    if "user_agent" in req:
        cfg.request.user_agent = req["user_agent"]

    proc = data.get("processing", {})
    cfg.processing.workers = proc.get("workers", cfg.processing.workers)
    cfg.processing.url_column = proc.get("url_column", cfg.processing.url_column)

    docs = data.get("documents", {})
    if "extensions" in docs:
        cfg.documents.extensions = docs["extensions"]

    browser = data.get("browser", {})
    cfg.browser.enabled = browser.get("enabled", cfg.browser.enabled)
    cfg.browser.timeout = browser.get("timeout", cfg.browser.timeout)

    return cfg
