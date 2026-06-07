"""
irctc_api.py — IRCTC Online Charts API Client
==============================================
Confirmed endpoints (HAR capture + JS bundle analysis, NO AUTH REQUIRED for chart pages):

  POST /online-charts/api/trainComposition
       body: {trainNo, jDate, boardingStation}
       response: {cdd: [{coachName, classCode, positionFromEngine, vacantBerths}],
                  trainName, from, to, remote, nextRemote, trainStartDate, ...}

  POST /online-charts/api/vacantBerth
       body: {trainNo, jDate, boardingStation, remoteStation,
              trainSourceStation, coach, clse, trainStartDate, chartType}
       response: per-berth occupancy (key names TBD until live test, multi-fallback)

  POST /online-charts/api/coachComposition
       body: {trainNo, jDate, boardingStation, coach, cls}
       response: {bdd: [{berthNo, berthCode, enable, bsd: [{occupancy, from, to, quota}]}]}

  GET  /eticketing/protected/mapps1/trnscheduleenquiry/{trainNo}
       response: full ordered station list (multiple known key shapes)

Akamai uses TLS fingerprinting — Python `requests` is silently dropped even from
residential IPs. We use `curl_cffi` (Chrome TLS impersonation) as the primary
HTTP client, falling back to `requests` if curl_cffi is not installed.
"""

import asyncio
import os
import time
from datetime import datetime
from typing import Optional

try:
    from upstreams import registry, STATE_DOWN, TIER_OFFICIAL_LIVE, TIER_OFFICIAL_STATIC_CACHE, TIER_SCHEDULED_ESTIMATE
except ImportError:
    pass


# Browser-based API: only use if explicitly available AND we're not on Windows
# (on Windows/local, curl_cffi + residential IP is more reliable than headless Playwright)
_HAVE_BROWSER = False
is_browser_available = lambda: False  # noqa: E731
browser_post = None  # type: ignore



# curl_cffi impersonates Chrome TLS — fallback when browser is not open.
try:
    from curl_cffi import requests as cffi_requests
    _HAVE_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore[no-redef]
    _HAVE_CFFI = False

BASE = "https://www.irctc.co.in/online-charts/api"
SCHED_BASE = "https://www.irctc.co.in/eticketing/protected/mapps1/trnscheduleenquiry"

# Headers matching the real browser fingerprint from HAR
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.irctc.co.in",
    "Referer": "https://www.irctc.co.in/online-charts/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

SCHED_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": HEADERS["User-Agent"],
    "Referer": "https://www.irctc.co.in/",
    "Origin": "https://www.irctc.co.in",
}


def _inject_cookie(h: dict) -> dict:
    """Optionally inject IRCTC_COOKIE env var into request headers.
    Set this if Akamai starts blocking: export IRCTC_COOKIE='...'
    (use session_helper.py to grab a valid cookie via Playwright).
    """
    cookie = os.environ.get("IRCTC_COOKIE", "")
    cookie = cookie.replace("\r", "").replace("\n", "").strip().strip("'\"")
    if cookie:
        return {**h, "Cookie": cookie}
    return h


def _post_with_retry(url: str, body: dict, max_retries: int = 3) -> dict:
    """
    POST with exponential backoff retry. Returns parsed JSON.

    Priority order:
      1. Residential chart worker (via tunnel) — if IRCTC_WORKER_URL is set
      2. browser_api.browser_post() — runs fetch() inside the debug Chrome
      3. curl_cffi direct HTTP (may be blocked by Akamai on cloud IPs)
    """
    try:
        from upstreams import registry, STATE_DOWN
        node = registry.get("irctc_online_charts")
        if node.get_state() == STATE_DOWN:
            raise RuntimeError("Circuit breaker open for IRCTC Online Charts")
    except ImportError:
        node = None

    # Extract the endpoint name from the URL for browser_api / worker routing
    endpoint = url.rsplit("/", 1)[-1]

    # Try residential chart worker first (primary for cloud deployments)
    try:
        from chart_worker_client import is_worker_configured, WorkerUnavailable
        if is_worker_configured():
            try:
                from chart_worker_client import (
                    worker_train_composition,
                    worker_coach_composition,
                    worker_vacant_berth,
                )
                worker_node = None
                try:
                    from upstreams import registry as _reg
                    worker_node = _reg.get("irctc_chart_worker")
                except ImportError:
                    pass

                if endpoint == "trainComposition":
                    res = worker_train_composition(
                        body.get("trainNo", ""),
                        body.get("jDate", ""),
                        body.get("boardingStation", ""),
                    )
                elif endpoint == "coachComposition":
                    res = worker_coach_composition(
                        body.get("trainNo", ""),
                        body.get("jDate", ""),
                        body.get("boardingStation", ""),
                        body.get("coach", ""),
                        body.get("cls", ""),
                    )
                elif endpoint == "vacantBerth":
                    res = worker_vacant_berth(
                        body.get("trainNo", ""),
                        body.get("jDate", ""),
                        body.get("boardingStation", ""),
                        body.get("remoteStation", ""),
                        body.get("trainSourceStation", ""),
                        body.get("trainStartDate", body.get("jDate", "")),
                        body.get("coach", ""),
                        body.get("clse", body.get("cls", "")),
                        body.get("chartType", 2),
                    )
                else:
                    raise WorkerUnavailable(f"Unknown endpoint for worker: {endpoint}")

                if worker_node:
                    worker_node.record_success()
                if node:
                    node.record_success()
                return res
            except PermissionError:
                # IRCTC 403 through worker — cookie issue, don't fall through
                if worker_node:
                    worker_node.record_failure()
                if node:
                    node.record_failure()
                raise
            except WorkerUnavailable as we:
                import logging
                logging.getLogger(__name__).warning(
                    "Chart worker unavailable for %s: %s. Falling back to local methods.",
                    endpoint, we,
                )
                if worker_node:
                    worker_node.record_failure()
    except ImportError:
        pass  # chart_worker_client not available — skip

    # Try browser-based fetch (secondary — bypasses Akamai from local)
    if _HAVE_BROWSER and is_browser_available():
        try:
            res = browser_post(endpoint, body)
            if node: node.record_success()
            return res
        except Exception as browser_err:
            # Log and fall through to direct HTTP
            import logging
            logging.getLogger(__name__).warning(
                "browser_api failed for %s: %s. Falling back to direct HTTP.",
                endpoint, browser_err
            )

    # Fallback: direct HTTP via curl_cffi
    headers = _inject_cookie(HEADERS)
    delay = 1.0
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            if _HAVE_CFFI:
                r = cffi_requests.post(
                    url, json=body, headers=headers,
                    timeout=20, impersonate="chrome124"
                )
            else:
                r = cffi_requests.post(url, json=body, headers=headers, timeout=20)

            if r.status_code in (401, 403):
                raise PermissionError(f"IRCTC returned HTTP {r.status_code} (Forbidden/Unauthorized). Cookie may be expired.")

            r.raise_for_status()
            if node: node.record_success()
            return r.json()
        except PermissionError as pe:
            if node: node.record_failure()
            raise pe
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
    if node: node.record_failure()
    raise RuntimeError(f"Failed after {max_retries} retries: {last_err}")


# ── Sync helpers ──────────────────────────────────────────────────────────────

def train_composition(train_no: str, jdate: str, boarding: str) -> dict:
    """
    Fetch train composition (coach list + train meta).
    jdate: "YYYY-MM-DD"

    Returns dict with confirmed keys:
      cdd       → list of {coachName, classCode, positionFromEngine, vacantBerths}
      trainName → str
      from      → source station code
      to        → destination station code
      remote    → current remote/charting station
      nextRemote
      trainStartDate
    """
    return _post_with_retry(
        f"{BASE}/trainComposition",
        {"trainNo": train_no, "jDate": jdate, "boardingStation": boarding},
    )


def coach_composition(train_no: str, jdate: str, boarding: str,
                      coach: str, cls: str) -> dict:
    """
    Fetch per-berth occupancy for one coach via coachComposition.

    Confirmed response structure (from JS bundle analysis):
      bdd → list of {
        berthNo:         int,
        berthCode:       str  (LB/UB/MB/SL/SU),
        enable:          bool (False = under repair/disabled),
        cabinCoupeNameNo: str,
        bsd:             list of {
          occupancy: bool  (True=occupied, False=vacant),
          from:      str   (station code),
          to:        str   (station code),
          quota:     str   (GN/GNRS/LD/DMGD/...)
        }
      }
    """
    return _post_with_retry(
        f"{BASE}/coachComposition",
        {
            "trainNo": train_no,
            "jDate": jdate,
            "boardingStation": boarding,
            "coach": coach,
            "cls": cls,
        },
    )


def vacant_berth(train_no: str, jdate: str, boarding: str, remote: str,
                 source: str, train_start_date: str, coach: str,
                 cls: str, chart_type: str = "SECOND_CHART") -> dict:
    """
    Fetch segment-wise free berths via vacantBerth endpoint.
    Response key structure unknown until live test — use as supplemental data
    or fall back to coachComposition for per-berth detail.

    Known request body fields (from JS bundle):
      trainNo, jDate, boardingStation, remoteStation,
      trainSourceStation, coach, clse (note: 'clse' not 'cls'),
      trainStartDate, chartType
    """
    return _post_with_retry(
        f"{BASE}/vacantBerth",
        {
            "trainNo": train_no,
            "jDate": jdate,
            "boardingStation": boarding,
            "remoteStation": remote,
            "trainSourceStation": source,
            "trainStartDate": train_start_date,
            "coach": coach,
            "clse": cls,
            "chartType": chart_type,
        },
    )


def train_schedule(train_no: str) -> list:
    """
    Fetch full ordered station code list from the schedule enquiry endpoint.
    Returns ["NDLS", "CNB", "PRYJ", ...] in journey order.
    Falls back to [] on any error.

    Known response shapes (IRCTC changes keys occasionally):
      Shape A: {trainScheduleDetails: [{stationCode, serialNumber}, ...]}
      Shape B: {stationList:          [{stnCode, seqNo}, ...]}
      Shape C: {stopList:             [{code, order}, ...]}
      Shape D: {stationDetails:       [{stationCode, sno}, ...]}
    """
    try:
        from upstreams import registry, STATE_DOWN
        node = registry.get("irctc_schedule")
        if node.get_state() == STATE_DOWN:
            return []
    except ImportError:
        node = None

    try:
        if _HAVE_CFFI:
            r = cffi_requests.get(
                f"{SCHED_BASE}/{train_no}",
                headers=_inject_cookie(SCHED_HEADERS),
                timeout=20, impersonate="chrome124",
            )
        else:
            r = cffi_requests.get(
                f"{SCHED_BASE}/{train_no}",
                headers=_inject_cookie(SCHED_HEADERS),
                timeout=20,
            )
        r.raise_for_status()
        data = r.json()
        if node: node.record_success()
    except Exception:
        if node: node.record_failure()
        return []

    raw = (
        data.get("trainScheduleDetails")
        or data.get("stationList")
        or data.get("stopList")
        or data.get("stationDetails")
        or []
    )
    if not raw:
        return []

    if isinstance(raw[0], str):
        return [s.upper() for s in raw if s]

    def _order(stop: dict) -> int:
        return int(
            stop.get("serialNumber")
            or stop.get("seqNo")
            or stop.get("order")
            or stop.get("sno")
            or stop.get("serialNo")
            or 0
        )

    sorted_stops = sorted(raw, key=_order)
    return [
        (
            stop.get("stationCode")
            or stop.get("stnCode")
            or stop.get("code")
            or stop.get("station")
            or ""
        ).upper()
        for stop in sorted_stops
        if (stop.get("stationCode") or stop.get("stnCode")
            or stop.get("code") or stop.get("station"))
    ]


# ── Async parallel fetch ──────────────────────────────────────────────────────
# NOTE: We use ThreadPoolExecutor + curl_cffi (not aiohttp) because:
#   - aiohttp uses Python's ssl module → same broken TLS fingerprint as requests
#   - curl_cffi wraps libcurl → real Chrome TLS fingerprint + HTTP/2
#   - ThreadPoolExecutor gives us true parallelism for I/O-bound HTTP calls

async def _fetch_schedule_async(train_no: str) -> list:
    """Async wrapper around train_schedule() for use in async contexts."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, train_schedule, train_no)


def _fetch_coach_sync(
    train_no: str,
    jdate: str,
    boarding: str,
    remote: str,
    source: str,
    train_start_date: str,
    coach_name: str,
    class_code: str,
) -> dict:
    """
    Sync fetch for one coach — runs in a thread pool so curl_cffi can be used.

    CRITICAL: The `boardingStation` param for coachComposition must be the
    train's `remote` (charting station returned by trainComposition), NOT
    the user's boarding station. IRCTC prepares the chart relative to the
    charting point. Passing the user's stop returns "No Record Found" once
    the train has moved past it.
    """
    headers = _inject_cookie(HEADERS)
    kwargs = dict(impersonate="chrome124") if _HAVE_CFFI else {}

    # Use the charting station (remote) as boardingStation for coachComposition.
    # Fall back to user boarding station only if remote is not set.
    chart_boarding = remote or boarding

    try:
        # Primary: coachComposition (confirmed bdd/bsd structure from JS bundle)
        # _post_with_retry uses browser_api first (bypasses Akamai), then curl_cffi fallback
        data = _post_with_retry(
            f"{BASE}/coachComposition",
            {
                "trainNo": train_no,
                "jDate": jdate,
                "boardingStation": chart_boarding,
                "coach": coach_name,
                "cls": class_code,
            },
        )
        # Treat "No Record Found" as an error so we fall through to vacantBerth
        if data.get("error") and not data.get("bdd"):
            raise ValueError(f"coachComposition error: {data['error']}")
        return {
            "coachName": coach_name,
            "classCode": class_code,
            "source": "coachComposition",
            "data": data,
            "source_metadata": {
                "source_tier": TIER_OFFICIAL_LIVE,
                "source_name": "IRCTC Online Charts",
                "fetched_at_ist": datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S IST"),
                "is_stale": False,
                "fallback_used": False,
                "confidence": "high",
            }
        }
    except PermissionError as pe:
        return {
            "coachName": coach_name,
            "classCode": class_code,
            "error": str(pe),
        }
    except Exception as coach_err:
        return {
            "coachName": coach_name,
            "classCode": class_code,
            "error": f"coachComposition: {coach_err}",
        }


def _fetch_class_vacant_sync(
    train_no: str,
    jdate: str,
    boarding: str,
    remote: str,
    source: str,
    class_code: str,
    chart_mode: str = "current",
) -> dict:
    """
    Fetch class-level vacantBerth rows.

    The IRCTC UI vacant-berth table is backed by this endpoint. It returns
    vbd[] rows with coachName/from/to/berthNumber, so fetch it once per class
    as supplemental evidence even when coachComposition succeeds.
    """
    chart_boarding = remote or boarding
    base_body = {
        "trainNo": train_no,
        "jDate": jdate,
        "boardingStation": chart_boarding,
        "remoteStation": remote,
        "trainSourceStation": source,
        "cls": class_code,
    }

    mode = (chart_mode or "current").strip().lower()
    if mode in {"historical", "history", "first", "first_chart", "past"}:
        chart_types = (1,)
    elif mode in {"current_only", "second", "second_chart", "latest"}:
        chart_types = (2,)
    else:
        chart_types = (2, 1)

    last_data: dict | None = None
    try:
        # Prefer the latest/current chart. If it is not prepared yet, fall back
        # to first chart so pre-departure trains still work.
        for chart_type in chart_types:
            data = _post_with_retry(
                f"{BASE}/vacantBerth",
                {**base_body, "chartType": chart_type},
            )
            last_data = data
            if data.get("vbd"):
                return {
                    "coachName": "ALL",
                    "classCode": class_code,
                    "source": "vacantBerth",
                    "data": data,
                    "source_metadata": {
                        "source_tier": TIER_OFFICIAL_LIVE,
                        "source_name": "IRCTC Online Charts",
                        "fetched_at_ist": datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S IST"),
                        "is_stale": False,
                        "fallback_used": False,
                        "confidence": "high",
                    }
                }
        return {
            "coachName": "ALL",
            "classCode": class_code,
            "source": "vacantBerth",
            "data": last_data or {"vbd": [], "error": "No Record Found"},
            "source_metadata": {
                "source_tier": TIER_OFFICIAL_LIVE,
                "source_name": "IRCTC Online Charts",
                "fetched_at_ist": datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S IST"),
                "is_stale": False,
                "fallback_used": False,
                "confidence": "high",
            }
        }
    except PermissionError as pe:
        return {
            "coachName": "ALL",
            "classCode": class_code,
            "error": str(pe),
        }
    except Exception as exc:
        return {
            "coachName": "ALL",
            "classCode": class_code,
            "source": "vacantBerth",
            "data": {"vbd": [], "error": str(exc)},
        }


async def all_coaches_async(
    train_no: str,
    jdate: str,
    boarding: str,
    remote: str,
    source: str,
    train_start_date: str,
    coaches: list,  # [{coachName, classCode}, ...]
    concurrency: int = 5,
    chart_mode: str = "current",
) -> list:
    """
    Fetch coachComposition for all coaches in parallel using ThreadPoolExecutor.
    curl_cffi (Chrome TLS) runs in threads — gets both fingerprint bypass AND parallelism.
    concurrency=5 means at most 5 simultaneous IRCTC requests.
    """
    import concurrent.futures

    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(concurrency)

    async def _guarded(coach: dict) -> dict:
        async with semaphore:
            return await loop.run_in_executor(
                None,
                _fetch_coach_sync,
                train_no, jdate, boarding, remote, source, train_start_date,
                coach["coachName"], coach["classCode"],
            )

    tasks = [_guarded(c) for c in coaches]
    coach_results = await asyncio.gather(*tasks)

    class_codes = sorted({
        (coach.get("classCode") or "").upper()
        for coach in coaches
        if coach.get("classCode")
    })

    async def _class_vacant(class_code: str) -> dict:
        async with semaphore:
            return await loop.run_in_executor(
                None,
                _fetch_class_vacant_sync,
                train_no,
                jdate,
                boarding,
                remote,
                source,
                class_code,
                chart_mode,
            )

    vacant_results = await asyncio.gather(*[_class_vacant(c) for c in class_codes])
    return coach_results + vacant_results


# Legacy alias for backward compatibility with existing callers
async def all_vacant_async(train_no: str, boarding: str, remote: str,
                           source: str, jdate: str, coaches: list) -> list:
    """
    Backward-compatible wrapper. Calls coachComposition (new primary endpoint)
    instead of vacantBerth. Returns same shape as before.
    """
    train_start_date = jdate  # best guess; callers should pass trainStartDate from comp
    results = await all_coaches_async(
        train_no, jdate, boarding, remote, source, train_start_date, coaches
    )
    # Remap to old shape so vacancy_filter.py keeps working
    out = []
    for r in results:
        if "error" in r:
            out.append({"coachName": r["coachName"], "classCode": r["classCode"],
                        "error": r["error"]})
        else:
            out.append({"coachName": r["coachName"], "classCode": r["classCode"],
                        "data": r["data"], "source": r.get("source", "")})
    return out




