"""Background jobs for the multi-PDF finder: search the page, then hand the PDFs to the normal URL analysis."""
from __future__ import annotations

import threading
import time
import uuid
from urllib.parse import urlparse

from ..analysis.models import SOURCE_FIND, BookRecord
from ..recipes import registry
from ..logger import log
from ..services.url_analysis_service import UrlAnalysisService
from . import scope as scope_mod
from .crawl import DEFAULT_MAX_PAGES, MAX_LEVELS, Crawler
from .finder import Discovery, FindError, PdfFinder

MAX_FINDS = 10


class FindService:
    def __init__(self, finder: PdfFinder, analysis: UrlAnalysisService):
        self.finder = finder
        self.crawler = Crawler(finder)
        self.analysis = analysis
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, params: dict) -> dict:
        """Validate quickly (FindError on bad input), then search in the background."""
        chk = self.finder.check(params.get("url", ""), params.get("scope", ""), params.get("click_selector", ""),
                                params.get("next_selector", ""), params.get("mode", "auto"))
        types = tuple(t for t in (params.get("types") or ["pdf"]) if t in ("pdf", "word")) or ("pdf",)
        follow = params.get("follow") or []
        if not isinstance(follow, list) or len(follow) > MAX_LEVELS - 1:
            raise FindError(f"At most {MAX_LEVELS} levels (the first page plus {MAX_LEVELS - 1} more) are supported.")
        scopes = [chk["scope"]]
        for n, raw in enumerate(follow, 2):
            try:
                sc = scope_mod.validate(str(raw or ""), f"level {n} scope")
            except scope_mod.ScopeError as e:
                raise FindError(str(e)) from e
            if not sc:
                raise FindError(f"Level {n} needs a scope - the links to open (or leave the level out).")
            scopes.append(sc)
        if len(scopes) > 1 and not scopes[0]:
            raise FindError("Level 1 needs a scope when further levels follow: which links on the first page should be opened?")
        try:
            max_pages = int(params.get("max_pages") or DEFAULT_MAX_PAGES)
        except (TypeError, ValueError):
            raise FindError("The maximum number of pages must be a number.")
        chk["scopes"] = scopes
        chk["max_pages"] = max_pages
        opts = params.get("recipe") if isinstance(params.get("recipe"), dict) else {}
        chk["recipe"] = registry.find_recipe(chk["url"]) if chk["mode"] != "page" else None
        chk["recipe_options"] = {"parte": opts.get("parte"), "read_files": bool(opts.get("read_files"))}
        chk["use_browser"] = bool(params.get("browser"))
        try:
            max_items = int(params.get("max_items") or 0) or None
        except (TypeError, ValueError):
            raise FindError("The maximum number of PDFs must be a number.")
        fid = uuid.uuid4().hex[:10]
        job = {"id": fid, "state": "running", "phase": "page", "message": "Starting", "clicks": 0, "links": None, "rounds": 0,
               "found": 0, "error": "", "notes": [], "job_id": None, "cancel": False, "started": time.time(),
               "url": chk["url"], "scope": chk["scope"], "mode_used": "", "stats": {}}
        with self._lock:
            while len(self._jobs) >= MAX_FINDS:
                self._jobs.pop(next(iter(self._jobs)))
            self._jobs[fid] = job
        threading.Thread(target=self._run, args=(job, chk, types, max_items, bool(params.get("strict"))), daemon=True).start()
        return {"find_id": fid}

    def _progress(self, job: dict, d: dict) -> None:
        with self._lock:
            for k in ("phase", "message", "clicks", "rounds"):
                if k in d and d[k] is not None:
                    job[k] = d[k]
            if d.get("links") is not None and d["links"] >= 0:
                job["links"] = d["links"]

    def _run(self, job: dict, chk: dict, types: tuple, max_items: int | None, strict: bool) -> None:
        try:
            recipe = chk.get("recipe")
            if recipe:                                           # a page with its own dedicated crawler (see recipes/registry.py)
                disc = recipe.run(chk["url"], chk["recipe_options"], progress=lambda d: self._progress(job, d), cancelled=lambda: job["cancel"])
            else:
                disc = self.crawler.run(
                    chk["url"], chk["scopes"], click_selector=chk["click_selector"], next_selector=chk["next_selector"], mode=chk["mode"],
                    types=types, max_items=max_items or 5000, strict=strict, max_pages=chk["max_pages"],
                    progress=lambda d: self._progress(job, d), cancelled=lambda: job["cancel"])
            with self._lock:
                job["notes"], job["stats"], job["mode_used"] = disc.notes, disc.stats, disc.mode
            if job["cancel"]:
                job["state"], job["message"] = "cancelled", "Stopped."
                return
            if not disc.found:
                hint = ""
                if disc.stats.get("skipped"):
                    hint = f" The scope matched {disc.stats['skipped']} element(s), but none of them had a PDF address."
                elif chk["scope"]:
                    hint = " Check the scope selector; it may match nothing on the page."
                job["error"] = "No PDF links were found." + hint + (" " + " ".join(disc.notes) if disc.notes else "")
                job["state"] = "error"
                return
            host = (urlparse(disc.final_url or chk["url"]).hostname or "").lower()
            if disc.prepared:                                    # results made by the recipe: shown as they are
                info = self.analysis.start_prepared(disc.prepared, source=SOURCE_FIND, source_name=host, source_columns=["Section Path", "Source Page", "Site IDs", "Found In", "Level"],
                                                    notes=disc.notes)
                with self._lock:
                    job.update(found=len(disc.prepared), job_id=info["job_id"], state="done", message=f"Found {len(disc.prepared):,} attachment(s)")
                return
            has_ids = any(f.node_id for f in disc.found)
            records = []
            for f in disc.found:
                extra = {"Section Path": f.path, "Source Page": f.source or chk["url"]}
                if has_ids:
                    extra["Link ID"] = f.node_id
                if chk["scope"]:
                    extra["Scope"] = " | ".join(chk["scopes"])
                records.append(BookRecord(url=f.url, book_title=f.title, extra=extra))
            columns = ["Section Path", "Source Page"] + (["Link ID"] if has_ids else []) + (["Scope"] if chk["scope"] else [])
            info = self.analysis.start(records, source=SOURCE_FIND, source_name=host, source_columns=columns,
                                       notes=disc.notes, duplicates_removed=int(disc.stats.get("duplicates", 0)),
                                       use_browser=chk["use_browser"])
            with self._lock:
                job.update(found=len(records), job_id=info["job_id"], state="done", message=f"Found {len(records):,} PDF link(s)")
        except FindError as e:
            job["state"], job["error"] = "error", str(e)
        except Exception:
            log.exception("Multi-PDF search failed")
            job["state"], job["error"] = "error", "The search stopped unexpectedly. Please try again."

    def status(self, fid: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(fid)
            if not j:
                return None
            return {k: j[k] for k in ("state", "phase", "message", "clicks", "links", "rounds", "found", "error", "notes", "job_id",
                                      "url", "scope", "mode_used", "stats")} | {"seconds": round(time.time() - j["started"], 1)}

    def cancel(self, fid: str) -> bool:
        with self._lock:
            j = self._jobs.get(fid)
            if not j:
                return False
            j["cancel"] = True
            return True
