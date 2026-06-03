"""
irctc_scraper.py

Core fetch + filter logic for IRCTC chart vacancy.
All filtering is pure Python — no AI involved.
The AI only receives the final compact string.

Real endpoints discovered from browser HAR:
  POST https://www.irctc.co.in/online-charts/api/trainComposition
  POST https://www.irctc.co.in/online-charts/api/vacantBerth
  POST https://www.irctc.co.in/online-charts/api/coachComposition
"""

import os
import asyncio
import httpx
from typing import Optional

BASE_URL = "https://www.irctc.co.in/online-charts/api"

# Headers that mimic a real browser session (from HAR analysis)
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.irctc.co.in",
    "Referer": "https://www.irctc.co.in/online-charts/",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Mobile Safari/537.36"
    ),
}


def _get_headers() -> dict:
    """Merge default headers with optional session cookie from env."""
    headers = DEFAULT_HEADERS.copy()
    cookie = os.environ.get("IRCTC_COOKIE", "")
    if cookie:
        headers["Cookie"] = cookie
    return headers


# ---------------------------------------------------------------------------
# 1. Fetch train composition (returns coach list + station order)
# ---------------------------------------------------------------------------

async def fetch_train_composition(
    client: httpx.AsyncClient,
    train_no: str,
    journey_date: str,  # format: YYYY-MM-DD
    boarding_station: str,
) -> dict:
    """
    POST /online-charts/api/trainComposition
    Returns the full composition payload including:
      - coachList: list of {coachName, coachClass, ...}
      - stationList: ordered list of stations for this train
      - trainStartDate, remote (charting station), from (source station)
    """
    payload = {
        "trainNo": train_no,
        "jDate": journey_date,       # YYYY-MM-DD
        "boardingStation": boarding_station,
    }
    resp = await client.post(
        f"{BASE_URL}/trainComposition",
        json=payload,
        headers=_get_headers(),
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"IRCTC error: {data['error']}")
    return data


# ---------------------------------------------------------------------------
# 2. Fetch vacant berths for one coach
# ---------------------------------------------------------------------------

async def fetch_vacant_berth(
    client: httpx.AsyncClient,
    train_no: str,
    journey_date: str,
    coach_id: str,       # e.g. "S4"
    coach_class: str,    # e.g. "SL"
    train_start_date: str,
    remote_station: str,
    source_station: str,
    boarding_station: str,
    chart_type: Optional[str] = "SECOND_CHART",
) -> dict:
    """
    POST /online-charts/api/vacantBerth
    Returns per-berth occupancy data.
    """
    payload = {
        "trainNo": train_no,
        "jDate": journey_date,
        "clse": coach_class,         # note: IRCTC uses 'clse' not 'cls'
        "coach": coach_id,
        "trainStartDate": train_start_date,
        "remoteStation": remote_station,
        "trainSourceStation": source_station,
        "boardingStation": boarding_station,
        "chartType": chart_type,
    }
    resp = await client.post(
        f"{BASE_URL}/vacantBerth",
        json=payload,
        headers=_get_headers(),
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    # Return even on error so we can skip gracefully
    return {"coachId": coach_id, "coachClass": coach_class, "data": data}


# ---------------------------------------------------------------------------
# 3. Core filtering — find vacant windows on each berth
# ---------------------------------------------------------------------------

def _station_index(station_code: str, station_order: list[str]) -> int:
    """Return index of station in the ordered route, or -1 if not found."""
    try:
        return station_order.index(station_code.upper())
    except ValueError:
        return -1


def compute_vacant_windows(berth_segments: list[dict], station_order: list[str]) -> list[dict]:
    """
    Given the list of booked segments on a single berth, compute the
    vacant windows (gaps between bookings).

    berth_segments: list of {"fromStation": "NDLS", "toStation": "CNB", ...}
    station_order:  ["NDLS", "MTJ", "AGC", "CNB", "ALD", "PNBE", ...]

    Returns: list of {"from": str, "to": str} dicts representing vacant segments.
    """
    if not berth_segments:
        # Berth is entirely vacant for the whole route
        if len(station_order) >= 2:
            return [{"from": station_order[0], "to": station_order[-1]}]
        return []

    idx = {stn: i for i, stn in enumerate(station_order)}

    def safe_idx(stn):
        return idx.get(stn.upper(), -1)

    # Sort booked segments by departure station index
    occupied = sorted(
        [s for s in berth_segments if safe_idx(s.get("fromStation", "")) >= 0],
        key=lambda s: safe_idx(s["fromStation"]),
    )

    windows = []
    prev_end_idx = 0  # start of route

    for seg in occupied:
        seg_start = safe_idx(seg["fromStation"])
        seg_end = safe_idx(seg["toStation"])
        if seg_start < 0 or seg_end < 0:
            continue

        # Gap before this booking?
        if seg_start > prev_end_idx:
            windows.append({
                "from": station_order[prev_end_idx],
                "to": station_order[seg_start - 1] if seg_start > 0 else station_order[seg_start],
            })

        prev_end_idx = max(prev_end_idx, seg_end)

    # Gap after the last booking?
    last_station_idx = len(station_order) - 1
    if prev_end_idx < last_station_idx:
        windows.append({
            "from": station_order[prev_end_idx + 1] if prev_end_idx + 1 <= last_station_idx else station_order[prev_end_idx],
            "to": station_order[last_station_idx],
        })

    return windows


def window_covers_segment(
    window: dict,
    user_from: str,
    user_to: str,
    station_order: list[str],
) -> bool:
    """
    Returns True if the vacant window covers at least the user's travel segment.
    i.e. window.from <= user_from AND window.to >= user_to (by station index)
    """
    idx = {stn: i for i, stn in enumerate(station_order)}

    w_from = idx.get(window["from"].upper(), -1)
    w_to = idx.get(window["to"].upper(), -1)
    u_from = idx.get(user_from.upper(), -1)
    u_to = idx.get(user_to.upper(), -1)

    if -1 in (w_from, w_to, u_from, u_to):
        return False

    return w_from <= u_from and w_to >= u_to


# ---------------------------------------------------------------------------
# 4. High-level: find all vacant berths for user's segment
# ---------------------------------------------------------------------------

BERTH_TYPE_MAP = {
    "LB": "Lower Berth",
    "MB": "Middle Berth",
    "UB": "Upper Berth",
    "SL": "Side Lower",
    "SU": "Side Upper",
    "WL": "Window Lower",
    "WU": "Window Upper",
}


async def find_vacant_berths(
    train_no: str,
    journey_date: str,   # YYYY-MM-DD
    boarded_from: str,   # station code, e.g. "NDLS"
    travel_to: str,      # station code, e.g. "CNB"
    cls: Optional[str] = None,  # filter class, e.g. "SL", "3A", "2A"; None = all
) -> str:
    """
    Main entry point called by both MCP servers.
    Returns a compact, pre-filtered string ready for AI consumption.
    """
    async with httpx.AsyncClient() as client:
        # Step 1: Get train composition
        try:
            composition = await fetch_train_composition(
                client, train_no, journey_date, boarded_from
            )
        except Exception as e:
            return f"Error fetching train composition: {e}"

        # Extract station order
        raw_stations = composition.get("stationList", [])
        if not raw_stations:
            return "Could not retrieve station list for this train."

        # Station list entries have {stationCode, stationName, ...}
        station_order = [
            s.get("stationCode", s.get("value", "")).upper()
            for s in raw_stations
            if s.get("stationCode") or s.get("value")
        ]

        if boarded_from.upper() not in station_order:
            return f"Station {boarded_from} not found in this train's route."
        if travel_to.upper() not in station_order:
            return f"Station {travel_to} not found in this train's route."

        # Extract metadata for subsequent calls
        train_start_date = composition.get("trainStartDate", journey_date)
        remote_station = composition.get("remote", boarded_from)
        source_station = composition.get("from", station_order[0])

        # Extract coach list
        all_coaches = composition.get("coachList", [])
        if not all_coaches:
            return "No coach list found in composition."

        # Filter by class if specified
        coaches_to_check = [
            c for c in all_coaches
            if not cls or c.get("coachClass", "").upper() == cls.upper()
        ]
        if not coaches_to_check:
            return f"No coaches found for class '{cls}' in this train."

        # Step 2: Fetch vacant berth data for all coaches in parallel
        # Use semaphore to avoid hammering IRCTC
        semaphore = asyncio.Semaphore(5)

        async def fetch_with_sem(coach):
            async with semaphore:
                return await fetch_vacant_berth(
                    client,
                    train_no=train_no,
                    journey_date=journey_date,
                    coach_id=coach.get("coachName", ""),
                    coach_class=coach.get("coachClass", ""),
                    train_start_date=train_start_date,
                    remote_station=remote_station,
                    source_station=source_station,
                    boarding_station=boarded_from,
                )

        results = await asyncio.gather(
            *[fetch_with_sem(c) for c in coaches_to_check],
            return_exceptions=True,
        )

        # Step 3: Filter + build output
        output_lines = [
            f"Train {train_no}  |  Date: {journey_date}",
            f"Boarded from: {boarded_from.upper()}  →  To: {travel_to.upper()}",
            f"Class filter: {cls.upper() if cls else 'All'}",
            "",
        ]

        total_vacant = 0

        for result in results:
            if isinstance(result, Exception):
                continue

            coach_id = result.get("coachId", "?")
            coach_class = result.get("coachClass", "?")
            data = result.get("data", {})

            if data.get("error"):
                continue

            # Berth list — key may vary; try common names
            berth_list = (
                data.get("berthList")
                or data.get("berths")
                or data.get("chartList")
                or []
            )

            coach_vacancies = []

            for berth in berth_list:
                berth_no = berth.get("berthNo", berth.get("berth", "?"))
                berth_type = berth.get("berthType", berth.get("type", ""))
                # Passenger segments booked on this berth
                passengers = (
                    berth.get("passengerList")
                    or berth.get("passengers")
                    or []
                )

                # Booked segments = from/to of each passenger
                booked_segments = [
                    {
                        "fromStation": p.get("boardingPoint", p.get("fromStation", "")),
                        "toStation": p.get("destination", p.get("toStation", "")),
                    }
                    for p in passengers
                    if p.get("boardingPoint") or p.get("fromStation")
                ]

                windows = compute_vacant_windows(booked_segments, station_order)

                for window in windows:
                    if window_covers_segment(window, boarded_from, travel_to, station_order):
                        berth_label = BERTH_TYPE_MAP.get(berth_type.upper(), berth_type)
                        coach_vacancies.append(
                            f"  Berth {str(berth_no).rjust(4)} ({berth_type.ljust(2)})  "
                            f"free from {window['from']} → {window['to']}"
                        )
                        total_vacant += 1

            if coach_vacancies:
                output_lines.append(f"── Coach {coach_id} ({coach_class}) ──")
                output_lines.extend(coach_vacancies)
                output_lines.append("")

        if total_vacant == 0:
            output_lines.append(
                f"No vacant berths found for {boarded_from.upper()} → {travel_to.upper()}."
            )
        else:
            output_lines.insert(3, f"Total vacant berths found: {total_vacant}")

        return "\n".join(output_lines)
