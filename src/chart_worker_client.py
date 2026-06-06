"""
chart_worker_client.py — HTTP client for the cloud control plane to call the
residential IRCTC chart worker through a reverse tunnel.

Usage:
  Set IRCTC_WORKER_URL env var to the tunnel URL (e.g. https://xxx.trycloudflare.com)
  or http://localhost:8001 for local development.

  Set IRCTC_WORKER_SECRET to the shared secret for authentication.

If IRCTC_WORKER_URL is not set, all functions raise WorkerNotConfigured and
callers should fall back to direct irctc_api.py calls.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

try:
    from curl_cffi import requests as cffi_requests
    _HAVE_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore[no-redef]
    _HAVE_CFFI = False

logger = logging.getLogger(__name__)


class WorkerNotConfigured(Exception):
    """Raised when IRCTC_WORKER_URL is not set."""


class WorkerUnavailable(Exception):
    """Raised when the worker is unreachable or returns an error."""


def _worker_url() -> str:
    """Get the worker base URL from env. Raises WorkerNotConfigured if unset."""
    url = os.environ.get("IRCTC_WORKER_URL", "").strip().rstrip("/")
    if not url:
        raise WorkerNotConfigured("IRCTC_WORKER_URL is not set")
    return url


def _headers() -> dict:
    """Build request headers including the shared secret."""
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    secret = os.environ.get("IRCTC_WORKER_SECRET", "").strip()
    if secret:
        h["X-Worker-Secret"] = secret
    return h


def _post(path: str, body: dict, timeout: int = 30) -> dict:
    """
    POST to the worker and return parsed JSON.
    Raises WorkerUnavailable on any failure.
    """
    url = f"{_worker_url()}{path}"
    started = time.time()
    try:
        if _HAVE_CFFI:
            r = cffi_requests.post(url, json=body, headers=_headers(), timeout=timeout)
        else:
            r = cffi_requests.post(url, json=body, headers=_headers(), timeout=timeout)

        latency_ms = int((time.time() - started) * 1000)

        if r.status_code == 401:
            raise WorkerUnavailable("Worker authentication failed (401)")
        if r.status_code == 403:
            # IRCTC returned 403 through the worker — cookie/permission issue
            raise PermissionError(f"IRCTC returned 403 via worker: {r.text[:200]}")
        if r.status_code == 502:
            raise WorkerUnavailable(f"Worker upstream error (502): {r.text[:200]}")
        if r.status_code >= 400:
            raise WorkerUnavailable(
                f"Worker returned HTTP {r.status_code}: {r.text[:200]}"
            )

        logger.debug("Worker %s responded in %dms", path, latency_ms)
        return r.json()
    except (WorkerUnavailable, WorkerNotConfigured, PermissionError):
        raise
    except Exception as e:
        latency_ms = int((time.time() - started) * 1000)
        raise WorkerUnavailable(f"Worker request failed ({latency_ms}ms): {e}") from e


def _get(path: str, timeout: int = 10) -> dict:
    """GET from the worker and return parsed JSON."""
    url = f"{_worker_url()}{path}"
    try:
        if _HAVE_CFFI:
            r = cffi_requests.get(url, headers=_headers(), timeout=timeout)
        else:
            r = cffi_requests.get(url, headers=_headers(), timeout=timeout)

        if r.status_code == 401:
            raise WorkerUnavailable("Worker authentication failed (401)")
        if r.status_code >= 400:
            raise WorkerUnavailable(f"Worker returned HTTP {r.status_code}")

        return r.json()
    except (WorkerUnavailable, WorkerNotConfigured):
        raise
    except Exception as e:
        raise WorkerUnavailable(f"Worker health check failed: {e}") from e


# ── Public API ────────────────────────────────────────────────────────────────

def is_worker_configured() -> bool:
    """True if IRCTC_WORKER_URL is set."""
    return bool(os.environ.get("IRCTC_WORKER_URL", "").strip())


def worker_health() -> dict:
    """
    Check worker liveness. Returns health dict or raises WorkerUnavailable.
    """
    return _get("/chart/health")


def worker_train_composition(train_no: str, jdate: str, boarding: str) -> dict:
    """Fetch trainComposition through the residential worker."""
    return _post("/chart/trainComposition", {
        "trainNo": train_no,
        "jDate": jdate,
        "boardingStation": boarding,
    })


def worker_coach_composition(
    train_no: str, jdate: str, boarding: str, coach: str, cls: str
) -> dict:
    """Fetch coachComposition through the residential worker."""
    return _post("/chart/coachComposition", {
        "trainNo": train_no,
        "jDate": jdate,
        "boardingStation": boarding,
        "coach": coach,
        "cls": cls,
    })


def worker_vacant_berth(
    train_no: str,
    jdate: str,
    boarding: str,
    remote: str,
    source: str,
    train_start_date: str,
    coach: str,
    cls: str,
    chart_type: int = 2,
) -> dict:
    """Fetch vacantBerth through the residential worker."""
    return _post("/chart/vacantBerth", {
        "trainNo": train_no,
        "jDate": jdate,
        "boardingStation": boarding,
        "remoteStation": remote,
        "trainSourceStation": source,
        "trainStartDate": train_start_date,
        "coach": coach,
        "clse": cls,
        "chartType": chart_type,
    })


def worker_set_cookie(cookie: str) -> dict:
    """Push a new IRCTC cookie to the residential worker."""
    return _post("/chart/set-cookie", {"cookie": cookie})
