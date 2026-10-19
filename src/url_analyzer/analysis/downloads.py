"""Save the documents of analysed URLs into a NEW folder, each file named after its Document ID.

The folder is always created fresh (<base>/<label>_<date>_<time>) - never the shared Downloads folder and never an existing
folder - and holds a `download_report.csv` listing what was saved, what was skipped and why.
"""
from __future__ import annotations

import csv
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..logger import log
from .document_analyzer import DIRECT_TYPES
from .engine import FetchedDocument
from .models import URLAnalysisResult

REPORT = "download_report.csv"
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MAX_NAME = 120


def safe_name(text: str) -> str:
    """A file-name stem that is valid on Windows, macOS and Linux and keeps the Document ID readable."""
    s = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", (text or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s.lower() in RESERVED:
        s = f"_{s}"
    return s[:MAX_NAME].rstrip(" .")


def new_folder(base: str | Path, label: str) -> Path:
    """Create and return a new folder inside `base` (made if missing). An existing folder is never reused."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_name(label) or 'PDFs'}_{time.strftime('%Y-%m-%d_%H%M%S')}"
    folder, n = base / stem, 1
    while folder.exists():
        n += 1
        folder = base / f"{stem}_{n}"
    folder.mkdir()
    return folder


@dataclass
class Item:
    row: int                      # 1-based position in the results table
    document_id: str
    title: str
    source_url: str               # the URL that was analysed
    target: str = ""              # the address of the document to save ('' = nothing to save)
    skip: str = ""                # why there is nothing to save


@dataclass
class Saved:
    row: int
    document_id: str
    title: str
    source_url: str
    target: str
    file: str = ""
    status: str = ""              # Saved | Failed | Skipped
    size: int = 0
    via: str = ""
    error: str = ""

    def as_row(self) -> list:
        return [self.row, self.document_id, self.title, self.source_url, self.target, self.file, self.status, self.size, self.via, self.error]


HEAD = ["Row", "Document ID", "Title", "URL", "Document URL", "File", "Status", "Size (bytes)", "Read via", "Error / reason"]


def target_of(r: URLAnalysisResult) -> tuple[str, str]:
    """(address of the document to save, why there is none)."""
    if r.content_type in DIRECT_TYPES or r.direct_document:
        return (r.document_url or r.final_url or r.url), ""
    if r.document_link_type == "SINGLE" and r.document_url:
        return r.document_url, ""
    if r.document_link_type == "MULTIPLE":
        return "", f"The page has {r.document_link_count or 'several'} different documents - open it and send the one you need"
    if r.document_link_type == "NONE":
        return "", "No document was found on this page"
    if r.status == "BLOCKED":
        return r.url, ""                                   # refused to the automated check: try it through the browser window
    if r.analysis_status == "Failed" or r.status in ("NOT_WORKING", "ERROR", "TIMEOUT"):
        return "", r.error or "The URL did not work"
    return "", "The URL is not a document"


def plan(results: list[URLAnalysisResult | None], indices: list[int] | None) -> list[Item]:
    wanted = range(len(results)) if indices is None else sorted({i for i in indices if isinstance(i, int)})
    items = []
    for i in wanted:
        if not 0 <= i < len(results):
            continue
        r = results[i]
        if r is None:
            items.append(Item(i + 1, "", "", "", skip="Not analysed yet"))
            continue
        target, why = target_of(r)
        items.append(Item(i + 1, r.document_id, r.book_title, r.url, target, why))
    return items


class Namer:
    """Unique, case-insensitive file names: <DocumentId>.pdf, then <DocumentId>_2.pdf ... Rows without an ID get Row<N>."""
    def __init__(self):
        self._used: set[str] = set()

    def name(self, it: Item, ext: str) -> str:
        stem = safe_name(it.document_id) or f"NoDocumentId_Row{it.row}"
        cand, n = f"{stem}{ext}", 1
        while cand.lower() in self._used:
            n += 1
            cand = f"{stem}_{n}{ext}"
        self._used.add(cand.lower())
        return cand


def run(analyzer, items: list[Item], folder: Path, *, use_browser: bool = False, workers: int = 4,
        progress: Callable[[Saved], None] | None = None, cancelled: Callable[[], bool] | None = None,
        on_start: Callable[[Item, threading.Event], None] | None = None,
        on_end: Callable[[Item], None] | None = None) -> list[Saved]:
    """Fetch and save every item. The same address is fetched once and copied for each Document ID that uses it."""
    namer, lock = Namer(), threading.Lock()
    first_file: dict[str, Path] = {}
    saved: list[Saved] = []

    def finish(s: Saved) -> Saved:
        with lock:
            saved.append(s)
        if progress:
            progress(s)
        return s

    def work(it: Item) -> Saved:
        s = Saved(it.row, it.document_id, it.title, it.source_url, it.target)
        if it.skip or not it.target:
            s.status, s.error = "Skipped", it.skip or "No document to save"
            return finish(s)
        if cancelled and cancelled():
            s.status, s.error = "Skipped", "Stopped before this file"
            return finish(s)
        key = it.target.split("#")[0]
        with lock:
            twin = first_file.get(key)
        if twin is not None and twin.exists():
            with lock:
                name = namer.name(it, twin.suffix)
            shutil.copyfile(twin, folder / name)
            s.file, s.status, s.size, s.via = name, "Saved", (folder / name).stat().st_size, "copy of the same address"
            return finish(s)
        skip = threading.Event()
        if on_start:
            on_start(it, skip)
        try:
            doc = fetch_skippable(analyzer, it.target, use_browser, skip)
        finally:
            if on_end:
                on_end(it)
        if not doc.ok:
            s.status, s.error = ("Skipped", doc.error) if skip.is_set() else ("Failed", doc.error)
            return finish(s)
        with lock:
            name = namer.name(it, doc.ext)
        part = folder / (name + ".part")
        part.write_bytes(doc.data)
        part.replace(folder / name)
        with lock:
            first_file.setdefault(key, folder / name)
        s.file, s.status, s.size, s.via = name, "Saved", len(doc.data), "browser window" if doc.via == "browser" else "direct"
        return finish(s)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # items with the same address run one after another so the later ones can copy the first (no second download)
        groups: dict[str, list[Item]] = {}
        for it in items:
            groups.setdefault(it.target.split("#")[0] if it.target else f"\0{it.row}", []).append(it)

        def run_group(group: list[Item]) -> None:
            for it in group:
                work(it)

        pending = {pool.submit(run_group, g) for g in groups.values()}
        while pending:
            done, pending = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
            for f in done:
                if f.exception():
                    log.error("Saving a document failed unexpectedly: %s", f.exception())
    saved.sort(key=lambda s: s.row)
    write_report(folder, saved)
    return saved


def fetch_skippable(analyzer, target: str, use_browser: bool, skip: threading.Event):
    """fetch_document, but the person can skip it: the call returns at once when `skip` is set (the file is dropped)."""
    box: dict = {}

    def go():
        box["doc"] = analyzer.fetch_document(target, use_browser, skip)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.2)
        if skip.is_set():
            return FetchedDocument(error="Skipped by you.")
    return box.get("doc") or FetchedDocument(error="The file could not be read.")


def write_report(folder: Path, saved: list[Saved]) -> Path:
    path = folder / REPORT

    def safe(v):
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        for s in saved:
            w.writerow([safe(c) for c in s.as_row()])
    return path
