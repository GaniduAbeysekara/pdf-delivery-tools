"""In-memory hand-off of structured data between tools (e.g. Backlog -> URL Analysis)."""
from __future__ import annotations

import threading
import time
import uuid

_TTL_SECONDS = 2 * 60 * 60
_MAX_ENTRIES = 50
_lock = threading.Lock()
_store: dict[str, tuple[float, dict]] = {}


def put(payload: dict) -> str:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    with _lock:
        for k in [k for k, (ts, _) in _store.items() if now - ts > _TTL_SECONDS]:
            del _store[k]
        while len(_store) >= _MAX_ENTRIES:
            del _store[min(_store, key=lambda k: _store[k][0])]
        _store[tid] = (now, payload)
    return tid


def get(tid: str) -> dict | None:
    with _lock:
        entry = _store.get(tid)
    if not entry or time.time() - entry[0] > _TTL_SECONDS:
        return None
    return entry[1]
