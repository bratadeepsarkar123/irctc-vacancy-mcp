"""
cache_policy.py - Immutable TTL caching logic.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import time
import threading
from typing import Any, Dict, Optional, Tuple

class TTLCache:
    """
    Thread-safe, immutable TTL cache.
    Returns (value, timestamp) instead of mutating the value.
    """
    def __init__(self, default_ttl: int = 60, persist_dir: Optional[str] = None, namespace: str = "default"):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl
        self.persist_dir = Path(persist_dir) if persist_dir else None
        self.namespace = namespace
        if self.persist_dir:
            self.persist_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Optional[Path]:
        if not self.persist_dir:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.persist_dir / f"{self.namespace}-{digest}.json"

    def _read_persisted(self, key: str) -> Optional[Tuple[float, Any]]:
        path = self._path_for(key)
        if not path or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return float(payload["fetched_time"]), payload["value"]
        except Exception:
            return None

    def _write_persisted(self, key: str, fetched_time: float, value: Any) -> None:
        path = self._path_for(key)
        if not path:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"fetched_time": fetched_time, "value": value}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except (TypeError, OSError):
            # Non-JSON values still work as in-memory cache entries.
            return

    def get(self, key: str, max_stale_ttl: Optional[int] = None) -> Tuple[Optional[Any], Optional[float], bool]:
        """
        Returns (value, fetched_time, is_stale).
        If the value is missing or older than max_stale_ttl (if provided), returns (None, None, False).
        If the value is present and older than self.default_ttl but within max_stale_ttl, returns (value, fetched_time, True).
        """
        with self._lock:
            if key not in self._cache:
                persisted = self._read_persisted(key)
                if persisted is None:
                    return None, None, False
                self._cache[key] = persisted

            fetched_time, value = self._cache[key]
            age = time.time() - fetched_time

            if age <= self.default_ttl:
                return copy.deepcopy(value), fetched_time, False

            if max_stale_ttl is not None and age <= max_stale_ttl:
                return copy.deepcopy(value), fetched_time, True

            # Expired and past max_stale_ttl
            del self._cache[key]
            return None, None, False

    def set(self, key: str, value: Any) -> None:
        """
        Store a value without sharing mutable references with callers.
        """
        with self._lock:
            fetched_time = time.time()
            val_to_store = copy.deepcopy(value)
            self._cache[key] = (fetched_time, val_to_store)
            self._write_persisted(key, fetched_time, val_to_store)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)
            path = self._path_for(key)
            if path:
                path.unlink(missing_ok=True)

# Default global caches
_CACHE_DIR = os.environ.get("IRCTC_MCP_CACHE_DIR")
# Train info/instances: 15 min TTL
train_info_cache = TTLCache(default_ttl=900, persist_dir=_CACHE_DIR, namespace="train-info")
# Schedule / Timetable: 24h TTL, stale acceptable
static_schedule_cache = TTLCache(default_ttl=86400, persist_dir=_CACHE_DIR, namespace="static-schedule")
# Live status: 60 sec TTL, acceptable up to 3 mins stale
live_status_cache = TTLCache(default_ttl=60, persist_dir=_CACHE_DIR, namespace="live-status")
