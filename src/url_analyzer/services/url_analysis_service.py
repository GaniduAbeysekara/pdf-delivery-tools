"""Runs URL-analysis jobs. Every input path (paste, Excel, Daily Backlog) starts its job here, so they all
share the same engine call, the same result events, the same re-analysis and the same export."""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from ..analysis import downloads as dl
from ..analysis.engine import MAX_SUPPLIED_HTML, default_analyzer
from ..analysis import (BookRecord, URLAnalysisResult, URLAnalyzer, analyze_urls, export_analysis_results)
from ..logger import log

MAX_JOBS = 20
MAX_DOWNLOADS = 10


class UrlAnalysisService:
    def __init__(self, output_dir: str | Path, analyzer: URLAnalyzer | None = None):
        self.output_dir = Path(output_dir)
        self.analyzer = analyzer            # None -> the shared default analyzer (shared cache)
        self._jobs: dict[str, dict] = {}
        self._downloads: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.download_base = self.output_dir.parent / "pdf_downloads"      # where new download folders are made by default

    # ── start ───────────────────────────────────────────────────────────────
    def start(self, records: list[BookRecord], *, source: str, source_name: str = "",
              source_columns: list[str] | None = None, notes: list[str] | None = None,
              duplicates_removed: int = 0, force_refresh: bool = False, use_browser: bool = False) -> dict:
        job_id = uuid.uuid4().hex[:10]
        job = {"id": job_id, "state": "running", "records": records, "total": len(records), "done": 0,
               "batch_total": len(records), "batch_done": 0, "events": [], "results": [None] * len(records),
               "error": "", "source": source, "source_name": source_name, "source_columns": source_columns or [],
               "notes": notes or [], "duplicates_removed": duplicates_removed, "use_browser": use_browser}
        with self._lock:
            while len(self._jobs) >= MAX_JOBS:
                self._jobs.pop(next(iter(self._jobs)))
            self._jobs[job_id] = job
        self._launch(job, list(range(len(records))), force_refresh)
        return {"job_id": job_id, "total": len(records), "source": source, "notes": job["notes"],
                "duplicates_removed": duplicates_removed}

    def start_prepared(self, results: list[URLAnalysisResult], *, source: str, source_name: str = "", source_columns: list[str] | None = None,
                       notes: list[str] | None = None) -> dict:
        """A finished job made of results that were produced elsewhere (a site recipe). The dashboard, downloads and
        'send to Base Template Analysis' work as for any other job; 're-analyze' does not apply (there is nothing to fetch)."""
        job_id = uuid.uuid4().hex[:10]
        events = [{"idx": i, **r.to_dict()} for i, r in enumerate(results)]
        job = {"id": job_id, "state": "done", "records": [BookRecord(url=r.url, document_id=r.document_id, book_title=r.book_title) for r in results],
               "total": len(results), "done": len(results), "batch_total": len(results), "batch_done": len(results), "events": events,
               "results": list(results), "error": "", "source": source, "source_name": source_name, "source_columns": source_columns or [],
               "notes": notes or [], "duplicates_removed": 0, "cancel": False, "prepared": True}
        with self._lock:
            while len(self._jobs) >= MAX_JOBS:
                self._jobs.pop(next(iter(self._jobs)))
            self._jobs[job_id] = job
        return {"job_id": job_id, "total": len(results), "source": source, "notes": job["notes"], "duplicates_removed": 0}

    def _launch(self, job: dict, indices: list[int], force: bool) -> None:
        def on_result(local_idx: int, res: URLAnalysisResult) -> None:
            idx = indices[local_idx]
            with self._lock:
                if job["results"][idx] is None:
                    job["done"] += 1
                job["results"][idx] = res
                job["events"].append({"idx": idx, **res.to_dict()})
                job["batch_done"] += 1

        job["cancel"] = False
        if job.get("use_browser"):
            self._analyzer().browser().reset()

        def run() -> None:
            try:
                analyze_urls([job["records"][i] for i in indices], analyzer=self.analyzer,
                             force_refresh=force, on_result=on_result, cancelled=lambda: job["cancel"],
                             use_browser=bool(job.get("use_browser")))
                job["state"] = "stopped" if job["cancel"] else "done"
            except Exception:
                log.exception("URL analysis job failed")
                job["state"], job["error"] = "error", "The analysis stopped unexpectedly. Please try again."

        job["batch_total"], job["batch_done"], job["state"] = len(indices), 0, "running"
        threading.Thread(target=run, daemon=True).start()

    def _analyzer(self) -> URLAnalyzer:
        return self.analyzer or default_analyzer()

    # ── query / control ─────────────────────────────────────────────────────
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return self._jobs.get(job_id)

    def status(self, job_id: str, start: int = 0) -> dict | None:
        job = self.get(job_id)
        if not job:
            return None
        with self._lock:
            events = job["events"][start:]
            return {"state": job["state"], "total": job["total"], "done": job["done"],
                    "batch_total": job["batch_total"], "batch_done": job["batch_done"], "events": events,
                    "next": start + len(events), "error": job["error"], "source": job["source"],
                    "source_name": job["source_name"], "notes": job["notes"],
                    "duplicates_removed": job["duplicates_removed"], "use_browser": bool(job.get("use_browser")),
                    "browser": self._browser_state(job)}

    def _browser_state(self, job: dict) -> dict:
        """Is the browser window waiting for the person to approve a site's verification page?"""
        fetcher = self._analyzer().browser_fetcher
        if not (job.get("use_browser") or job.get("saving")) or fetcher is None:
            return {"waiting_for_user": False}
        return fetcher.state()

    def cancel(self, job_id: str) -> str | None:
        """Ask a running analysis to stop. Returns the job's state ('running' = stopping now), or None for an unknown job."""
        job = self.get(job_id)
        if not job:
            return None
        if job["state"] == "running":
            job["cancel"] = True
            if job.get("use_browser") and self._analyzer().browser_fetcher is not None:
                self._analyzer().browser_fetcher.abort()            # a wait for approval in the window ends too
        return job["state"]

    def reanalyze(self, job_id: str, indices: list[int] | None = None, use_browser: bool | None = None) -> bool:
        """Fresh (non-cached) re-check of the given result rows, or of every row. `use_browser` switches the
        browser window for blocked sites on or off from now on (None keeps the job's setting)."""
        job = self.get(job_id)
        if not job or job["state"] == "running" or job.get("prepared"):
            return False
        if use_browser is not None:
            job["use_browser"] = bool(use_browser)
        valid = sorted({i for i in (indices if indices is not None else range(job["total"]))
                        if isinstance(i, int) and 0 <= i < job["total"]})
        if not valid:
            return False
        self._launch(job, valid, True)
        return True

    def apply_page_source(self, job_id: str, index: int, html: str) -> str:
        """Re-analyse one row from HTML the user copied out of their browser. Returns '' or a user-facing error."""
        job = self.get(job_id)
        if not job:
            return "Unknown analysis."
        if job["state"] == "running":
            return "Wait for the running analysis to finish first."
        if not isinstance(index, int) or not 0 <= index < job["total"]:
            return "That row does not exist."
        if not html or "<" not in html:
            return "Paste the page's HTML source."
        job["records"][index].page_html = html[:MAX_SUPPLIED_HTML]
        self._launch(job, [index], True)
        return ""

    def dataset_rows(self, job_id: str, indices: list[int] | None = None) -> list[dict] | None:
        """Document ID / Domain / Spider Template / URL of finished results (all, or the rows at `indices`)
        for the Base Template Analysis hand-off. `row` is the 1-based position in the results table."""
        job = self.get(job_id)
        if not job:
            return None
        with self._lock:
            wanted = range(len(job["results"])) if indices is None else sorted({i for i in indices if isinstance(i, int)})
            return [{"row": i + 1, "document_id": r.document_id, "domain": r.domain, "spidering_template": r.spidering_template, "url": r.url}
                    for i in wanted if 0 <= i < len(job["results"]) and (r := job["results"][i]) is not None]

    def export(self, job_id: str, indices: list[int] | None = None) -> Path | None:
        """Excel export of every result, or only the rows at `indices` (the user's selection)."""
        job = self.get(job_id)
        if not job:
            return None
        with self._lock:
            if indices is None:
                results = [r for r in job["results"] if r is not None]
            else:
                results = [job["results"][i] for i in sorted(set(indices))
                           if isinstance(i, int) and 0 <= i < len(job["results"]) and job["results"][i] is not None]
        if not results:
            return None
        suffix = "" if indices is None else "_selected"
        return export_analysis_results(results, self.output_dir / f"{job_id}_URL_Analysis{suffix}.xlsx",
                                       source=job["source"], source_name=job["source_name"],
                                       source_columns=job["source_columns"])

    # ── saving the documents (Download PDFs) ────────────────────────────────
    def start_download(self, job_id: str, indices: list[int] | None, *, use_browser: bool, base: str | Path | None = None) -> dict:
        """Save the documents of the given result rows (all when None) into a NEW folder, named by Document ID.
        Returns {"download_id", "folder", "total", "saveable", "skipped"} or {"error"}."""
        job = self.get(job_id)
        if not job:
            return {"error": "Unknown analysis."}
        with self._lock:
            results = list(job["results"])
        items = dl.plan(results, indices)
        saveable = [it for it in items if it.target and not it.skip]
        if not items:
            return {"error": "There are no results to save yet."}
        if not saveable:
            return {"error": "None of these rows has a document to save (" + (items[0].skip or "no document") + ")."}
        try:
            folder = dl.new_folder(base or self.download_base, job["source_name"] or "PDFs")
        except OSError as e:
            return {"error": f"The folder could not be created: {str(e)[:120]}"}
        did = uuid.uuid4().hex[:10]
        dj = {"id": did, "job_id": job_id, "state": "running", "folder": str(folder.resolve()), "total": len(items), "done": 0, "saved": 0, "failed": 0,
              "skipped": 0, "bytes": 0, "events": [], "cancel": False, "use_browser": use_browser, "error": "", "active": {}}
        with self._lock:
            while len(self._downloads) >= MAX_DOWNLOADS:
                self._downloads.pop(next(iter(self._downloads)))
            self._downloads[did] = dj
        job["saving"] = True
        analyzer = self._analyzer()
        if use_browser:
            analyzer.browser().reset()

        def progress(s: dl.Saved) -> None:
            with self._lock:
                dj["done"] += 1
                dj["saved" if s.status == "Saved" else "failed" if s.status == "Failed" else "skipped"] += 1
                dj["bytes"] += s.size
                dj["events"].append({"row": s.row, "document_id": s.document_id, "file": s.file, "status": s.status, "error": s.error, "via": s.via})

        def started(it: dl.Item, skip: threading.Event) -> None:
            with self._lock:
                dj["active"][it.row] = {"row": it.row, "document_id": it.document_id, "url": it.target, "since": time.monotonic(), "skip": skip}

        def ended(it: dl.Item) -> None:
            with self._lock:
                dj["active"].pop(it.row, None)

        def run() -> None:
            try:
                dl.run(analyzer, items, folder, use_browser=use_browser, progress=progress, cancelled=lambda: dj["cancel"],
                       on_start=started, on_end=ended)
                dj["state"] = "stopped" if dj["cancel"] else "done"
            except Exception:
                log.exception("Saving documents failed")
                dj["state"], dj["error"] = "error", "Saving the documents stopped unexpectedly."
            finally:
                job["saving"] = False

        threading.Thread(target=run, daemon=True).start()
        return {"download_id": did, "folder": dj["folder"], "total": len(items), "saveable": len(saveable), "skipped": len(items) - len(saveable)}

    def download_status(self, did: str, start: int = 0) -> dict | None:
        with self._lock:
            dj = self._downloads.get(did)
            if not dj:
                return None
            events = dj["events"][start:]
            job = self._jobs.get(dj["job_id"]) or {}
            return {"state": dj["state"], "folder": dj["folder"], "total": dj["total"], "done": dj["done"], "saved": dj["saved"],
                    "failed": dj["failed"], "skipped": dj["skipped"], "bytes": dj["bytes"], "events": events, "next": start + len(events),
                    "error": dj["error"], "use_browser": dj["use_browser"],
                    "active": [{"row": a["row"], "document_id": a["document_id"], "url": a["url"], "seconds": int(time.monotonic() - a["since"])}
                               for a in dj["active"].values()], "browser": self._browser_state(job) if dj["use_browser"] else {"waiting_for_user": False}}

    def cancel_download(self, did: str) -> bool:
        with self._lock:
            dj = self._downloads.get(did)
        if not dj:
            return False
        dj["cancel"] = True
        if dj["use_browser"] and self._analyzer().browser_fetcher is not None:
            self._analyzer().browser_fetcher.abort()
        return True

    def skip_download_item(self, did: str, row: int) -> bool:
        """Give up on one file that is being fetched (slow, or waiting for a verification page) and go on with the others."""
        with self._lock:
            dj = self._downloads.get(did)
            a = dj["active"].get(row) if dj else None
        if not a:
            return False
        a["skip"].set()
        return True

    def skip_browser_wait(self, job_id: str) -> bool:
        """During an analysis: skip the URL the browser window is working on (e.g. waiting for a verification page)."""
        job = self.get(job_id)
        fetcher = self._analyzer().browser_fetcher
        return bool(job and fetcher is not None and fetcher.skip_current())

    def download_folder(self, did: str) -> str | None:
        with self._lock:
            dj = self._downloads.get(did)
        return dj["folder"] if dj else None
