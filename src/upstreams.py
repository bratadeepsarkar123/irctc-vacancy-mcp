"""
upstreams.py - Upstream dependency registry and circuit breaker logic.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Source Tiers
TIER_OFFICIAL_LIVE = "official_live"
TIER_OFFICIAL_STATIC_CACHE = "official_static_cache"
TIER_THIRD_PARTY_ADVISORY = "third_party_advisory"
TIER_SCHEDULED_ESTIMATE = "scheduled_estimate"
TIER_UNAVAILABLE = "unavailable"

STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_DEGRADED = "DEGRADED"

class UpstreamNode:
    """
    Tracks health of an upstream dependency (circuit breaker).
    """
    def __init__(self, name: str, threshold: int = 5, timeout_sec: int = 60):
        self.name = name
        self.threshold = threshold
        self.timeout_sec = timeout_sec

        self._lock = threading.RLock()
        self._failures = 0
        self._last_failure_time: Optional[float] = None
        self._last_success_time: Optional[float] = None
        self._state = STATE_UP

    def get_state(self) -> str:
        with self._lock:
            if self._state == STATE_DOWN:
                if self._last_failure_time and (time.time() - self._last_failure_time) > self.timeout_sec:
                    # Half-open: allow a request through to test if it's recovered
                    return STATE_DEGRADED
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = STATE_UP
            self._last_success_time = time.time()

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self.threshold:
                self._state = STATE_DOWN

    def get_status_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self.get_state(),
                "failures": self._failures,
                "last_failure_time": self._last_failure_time,
                "last_success_time": self._last_success_time,
            }

class Registry:
    def __init__(self):
        self.nodes: Dict[str, UpstreamNode] = {
            "ntes_appservand": UpstreamNode("NTES AppServAnd"),
            "irctc_online_charts": UpstreamNode("IRCTC Online Charts", threshold=3),
            "irctc_schedule": UpstreamNode("IRCTC Schedule Enquiry", threshold=5),
            "browser_context": UpstreamNode("Browser Context Transport"),
            "third_party_scrapers": UpstreamNode("Third-Party Scrapers", threshold=3),
            "irctc_chart_worker": UpstreamNode("IRCTC Chart Worker (Residential)", threshold=3, timeout_sec=30),
        }

    def get(self, name: str) -> UpstreamNode:
        if name not in self.nodes:
            self.nodes[name] = UpstreamNode(name)
        return self.nodes[name]

    def all_status(self) -> list[Dict[str, Any]]:
        return [node.get_status_dict() for node in self.nodes.values()]

    def probe(self, probes: Dict[str, Callable[[], Any]]) -> list[Dict[str, Any]]:
        """
        Run explicit synthetic checks for selected upstreams.

        This is intentionally opt-in; passive health endpoints should not hit
        railway sites or third-party scrapers on every load balancer ping.
        """
        results = []
        for key, probe_fn in probes.items():
            node = self.get(key)
            started = time.time()
            try:
                probe_fn()
                node.record_success()
                ok = True
                error = None
            except Exception as exc:  # pragma: no cover - exercised via tests
                node.record_failure()
                ok = False
                error = str(exc)

            status = node.get_status_dict()
            status.update({
                "key": key,
                "probe_ok": ok,
                "probe_error": error,
                "probe_latency_ms": int((time.time() - started) * 1000),
            })
            results.append(status)
        return results

# Global registry instance
registry = Registry()
