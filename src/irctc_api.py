"""
IRCTC Online Charts API Client
Confirmed endpoints (HAR + JS bundle analysis, NO AUTH REQUIRED):
  POST /online-charts/api/trainComposition  -> coach list + train meta
  POST /online-charts/api/coachComposition  -> berth breakdown by class
  POST /online-charts/api/vacantBerth       -> segment-wise free berths per coach
  GET  /eticketing/protected/mapps1/trnscheduleenquiry/{trainNo} -> full station order

NOTE: Akamai WAF blocks datacenter IPs (Azure/GCP/AWS).
      Run from a local/residential machine. Cloudflare Tunnel -> local is the best deployment.
"""

import asyncio
import os
import aiohttp
import requests

BASE        = "https://www.irctc.co.in/online-charts/api"
SCHED_BASE  = "https://www.irctc.co.in/eticketing/protected/mapps1/trnscheduleenquiry"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.irctc.co.in",
    "Referer": "https://www.irctc.co.in/online-charts/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# The schedule endpoint returns JSON with slightly different Accept requirements
SCHED_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": HEADERS["User-Agent"],
    "Referer": "https://www.irctc.co.in/",
    "Origin": "https://www.irctc.co.in",
}


def _inject_cookie(h: dict) -> dict:
    """Optionally inject IRCTC_COOKIE env var into request headers."""
    cookie = os.environ.get("IRCTC_COOKIE", "")
    if cookie:
        return {**h, "Cookie": cookie}
    return h


# ── Sync helpers ─────────────────────────────────────────────

def train_composition(train_no: str, jdate: str, boarding: str) -> dict:
    """
    jdate: "YYYY-MM-DD"
    Returns: {cdd: [{coachName, classCode, vacantBerths}],
              trainName, from, to, remote, trainStartDate, ...}
    """
    r = requests.post(
        f"{BASE}/trainComposition",
        json={"trainNo": train_no, "jDate": jdate, "boardingStation": boarding},
        headers=_inject_cookie(HEADERS), timeout=15
    )
    r.raise_for_status()
    return r.json()


def train_schedule(train_no: str) -> list:
    """
    Fetch the full ordered station code list for a train from the schedule
    enquiry endpoint.

    Returns: ["NDLS", "CNB", "PRYJ", "MGS", ...] in journey order.
    Falls back to [] on any error so callers can try composition fallback.

    Endpoint: GET /eticketing/protected/mapps1/trnscheduleenquiry/{trainNo}

    Observed response shapes (IRCTC changes keys occasionally):
      Shape A: {"trainScheduleDetails": [{"stationCode": ..., "serialNumber": ...}, ...]}
      Shape B: {"stationList":           [{"stnCode": ..., "seqNo": ...}, ...]}
      Shape C: {"stopList":              [{"code": ..., "order": ...}, ...]}
      Shape D: {"stationDetails":        [{"stationCode": ..., "sno": ...}, ...]}

    We try all four keys and sort by whichever ordering field is present.
    """
    try:
        r = requests.get(
            f"{SCHED_BASE}/{train_no}",
            headers=_inject_cookie(SCHED_HEADERS),
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    # Try all known wrapper keys in priority order
    raw = (
        data.get("trainScheduleDetails")
        or data.get("stationList")
        or data.get("stopList")
        or data.get("stationDetails")
        or []
    )
    if not raw:
        return []

    # Each stop is either a plain string or a dict
    if isinstance(raw[0], str):
        return [s.upper() for s in raw if s]

    # It's a list of dicts — sort by whichever ordering field exists
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
        if (
            stop.get("stationCode")
            or stop.get("stnCode")
            or stop.get("code")
            or stop.get("station")
        )
    ]


def vacant_berth(train_no: str, boarding: str, remote: str,
                 source: str, jdate: str, coach: str, cls: str) -> dict:
    """Segment-wise free berths for one coach."""
    r = requests.post(
        f"{BASE}/vacantBerth",
        json={
            "trainNo": train_no, "boardingStation": boarding,
            "remoteStation": remote, "trainSourceStation": source,
            "jDate": jdate, "coach": coach, "cls": cls
        },
        headers=_inject_cookie(HEADERS), timeout=15
    )
    r.raise_for_status()
    return r.json()


# ── Async parallel fetch for all coaches ────────────────────

async def _fetch_one(session: aiohttp.ClientSession, payload: dict) -> dict:
    async with session.post(
        f"{BASE}/vacantBerth", json=payload,
        headers=_inject_cookie(HEADERS)
    ) as resp:
        return await resp.json(content_type=None)


async def _fetch_schedule_async(train_no: str) -> list:
    """
    Async wrapper around train_schedule() so it can be awaited in
    the same event loop as the parallel vacant-berth fetches.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, train_schedule, train_no)


async def all_vacant_async(train_no: str, boarding: str, remote: str,
                           source: str, jdate: str,
                           coaches: list) -> list:
    """
    Fetch vacantBerth for all coaches in parallel.
    coaches: [{coachName, classCode}, ...]  from trainComposition cdd list.
    Returns: [{coachName, classCode, data|error}, ...]
    """
    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_one(session, {
                "trainNo": train_no, "boardingStation": boarding,
                "remoteStation": remote, "trainSourceStation": source,
                "jDate": jdate, "coach": c["coachName"], "cls": c["classCode"]
            })
            for c in coaches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    return [
        {
            "coachName": c["coachName"],
            "classCode": c["classCode"],
            **( {"data": r} if not isinstance(r, Exception) else {"error": str(r)} )
        }
        for c, r in zip(coaches, results)
    ]
