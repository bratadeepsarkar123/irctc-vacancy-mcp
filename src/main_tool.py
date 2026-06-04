"""
main_tool.py — Single entry point for all MCP servers
======================================================
Pipeline:
  1. train_composition()      → coach list + train meta (cdd key)
  2. train_schedule()         → full ordered station list
     fallback: from/to endpoints from composition
  3. all_coaches_async()      → parallel coachComposition for all coaches
  4. filter_vacant()          → pure Python gap-finding, no AI
  5. format_result()          → compact string; THIS is what AI sees

The AI/LLM never receives raw IRCTC data.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

# Support both package import (from .irctc_api) and direct script/uvicorn execution
sys.path.insert(0, str(Path(__file__).parent))

try:
    from .irctc_api import (
        train_composition,
        all_coaches_async,
        _fetch_schedule_async,
    )
    from .vacancy_filter import filter_vacant, format_result
except ImportError:
    from irctc_api import (
        train_composition,
        all_coaches_async,
        _fetch_schedule_async,
    )
    from vacancy_filter import filter_vacant, format_result


def _stations_from_composition(comp: dict) -> list:
    """
    Fallback: extract station list from composition response.
    trainComposition doesn't return a full stationList — only from/remote.
    We use those as a minimal fallback for single-hop queries.
    """
    raw = comp.get("stationList") or []
    if raw and isinstance(raw[0], dict):
        return [
            (
                s.get("stationCode") or s.get("stnCode")
                or s.get("value") or s.get("code") or ""
            ).upper()
            for s in raw
            if (s.get("stationCode") or s.get("stnCode")
                or s.get("value") or s.get("code"))
        ]
    if raw and isinstance(raw[0], str):
        return [s.upper() for s in raw if s]

    # Absolute fallback: just the endpoints we know from composition
    parts = []
    for key in ("from", "trainSourceStation"):
        v = (comp.get(key) or "").upper()
        if v:
            parts.append(v)
    for key in ("remote", "nextRemote", "to", "destinationStation"):
        v = (comp.get(key) or "").upper()
        if v and v not in parts:
            parts.append(v)
    return parts


async def find_vacant_berths(
    train_no: str,
    jdate: str,           # "YYYY-MM-DD" (other formats auto-normalised)
    boarded_from: str,    # e.g. "NDLS"
    travel_to: str,       # e.g. "CNB"
    class_filter: Optional[str] = None,  # "SL", "3A", "2A", "1A", None=all
) -> str:
    """
    Async orchestrator. Returns the pre-filtered, formatted string
    that the AI should present to the user.
    """
    import re as _re
    from datetime import datetime as _dt

    boarded_from = boarded_from.upper().strip()
    travel_to = travel_to.upper().strip()

    # FIX: same-station check before anything else
    if boarded_from == travel_to:
        return (
            f"Boarding station and destination are both {boarded_from!r}. "
            "Please provide two different stations."
        )

    # FIX: normalise date — accept YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            jdate = _dt.strptime(jdate.strip(), fmt).strftime("%Y-%m-%d")
            break
        except ValueError:
            continue

    # ── Step 1: Train composition ─────────────────────────────
    try:
        comp = train_composition(train_no, jdate, boarded_from)
    except Exception as e:
        return f"Error fetching train composition for {train_no}: {e}"

    # Only treat as an error if the error field is set AND we have no coaches
    if comp.get("error") and not comp.get("cdd"):
        err = comp['error']
        # FIX: give context for common "Chart not prepared" error
        if "chart not prepared" in err.lower() or "chart" in err.lower():
            return (
                f"Chart not available for train {train_no} on {jdate}.\n"
                "This usually means one of:\n"
                "  1. The chart hasn't been prepared yet (typically ready 4-6 hrs before departure)\n"
                "  2. The train number is invalid\n"
                "  3. The boarding station code is wrong for this train\n"
                "  4. The date is too far in the future\n"
                f"Tip: Try the IRCTC website to verify train {train_no} exists and runs on {jdate}."
            )
        return f"IRCTC error for train {train_no}: {err}"

    train_name = comp.get("trainName") or comp.get("name") or train_no
    # `remote` is the CHARTING STATION — this is what coachComposition/vacantBerth
    # need as boardingStation. It is NOT the user's boarding station.
    # IRCTC prepares charts from the charting point; using any other station returns
    # "No Record Found" from coachComposition.
    remote = (
        comp.get("remote") or comp.get("nextRemote")
        or comp.get("remoteStation") or boarded_from
    ).upper()
    source = (comp.get("from") or comp.get("trainSourceStation") or boarded_from).upper()
    train_start_date = comp.get("trainStartDate") or jdate

    # Extract coach list — confirmed key is 'cdd'
    coaches_raw = comp.get("cdd") or comp.get("coachList") or comp.get("coaches") or []
    if not coaches_raw:
        return (
            f"No coach data returned for train {train_no}. "
            "Chart may not be prepared yet — charts are typically available "
            "4–6 hours before departure."
        )

    # Normalise to [{coachName, classCode}]
    coaches = [
        {
            "coachName": (
                c.get("coachName") or c.get("coach")
                or c.get("coachId") or ""
            ),
            "classCode": (
                c.get("classCode") or c.get("cls")
                or c.get("coachClass") or ""
            ),
        }
        for c in coaches_raw
        if (c.get("coachName") or c.get("coach") or c.get("coachId"))
    ]

    # Apply class filter early to reduce API calls
    if class_filter:
        cf = class_filter.upper()
        coaches = [c for c in coaches if c["classCode"].upper() == cf]
        if not coaches:
            all_classes = sorted({
                (c.get("classCode") or "").upper()
                for c in coaches_raw
                if c.get("classCode")
            })
            return (
                f"No {cf} class coaches found in train {train_no}. "
                f"Available classes: {', '.join(all_classes) or 'none detected'}"
            )

    # ── Step 2: Station order ─────────────────────────────────
    # Fetch schedule (full route) concurrently — this gives the complete ordered list
    station_list = await _fetch_schedule_async(train_no)

    if not station_list or len(station_list) <= 3:
        # IRCTC schedule often returns only charting stations (e.g., [MBDP, LKO, BPL])
        # Use confirmtkt.com as primary source for ALL intermediate stops
        try:
            from train_schedule import _fetch_confirmtkt_schedule
            schedule = _fetch_confirmtkt_schedule(train_no)
            if schedule and len(schedule) > len(station_list or []):
                station_list = [stn["code"] for stn in schedule]
        except Exception:
            pass  # Fall through to composition fallback

    if not station_list:
        # Fallback: extract from composition response
        station_list = _stations_from_composition(comp)

    if not station_list:
        return (
            f"Could not retrieve station list for train {train_no}. "
            "Schedule endpoint may be temporarily unavailable. Please try again."
        )

    # Validate user stations exist on route
    if boarded_from not in station_list:
        sample = station_list[:15]
        return (
            f"Station {boarded_from!r} not found in train {train_no}'s route.\n"
            f"Known stations on this train ({len(station_list)} total): {sample}"
        )
    if travel_to not in station_list:
        sample = station_list[:15]
        return (
            f"Station {travel_to!r} not found in train {train_no}'s route.\n"
            f"Known stations on this train ({len(station_list)} total): {sample}"
        )
    if station_list.index(boarded_from) >= station_list.index(travel_to):
        idx_from = station_list.index(boarded_from)
        idx_to = station_list.index(travel_to)
        return (
            f"Station order error: {boarded_from} (stop #{idx_from+1}) comes "
            f"AFTER {travel_to} (stop #{idx_to+1}) on this train's route. "
            "Did you swap the boarding and destination stations?"
        )

    # ── Step 3: Parallel coach data fetch ─────────────────────
    try:
        all_data = await all_coaches_async(
            train_no, jdate, boarded_from, remote, source,
            train_start_date, coaches,
        )
    except Exception as e:
        return f"Error fetching coach data: {e}"

    # ── Step 4: Pure-Python filter ────────────────────────────
    vacant = filter_vacant(all_data, boarded_from, travel_to, class_filter, station_list)

    # ── Step 5: Get departure time for better messaging ───────
    dep_time = ""
    try:
        from train_schedule import _fetch_confirmtkt_schedule
        sched = _fetch_confirmtkt_schedule(train_no)  # Uses cache — free if already fetched
        if sched:
            dep_time = sched[0].get("departure", "")
    except Exception:
        pass

    # ── Step 6: Format for AI ─────────────────────────────────
    return format_result(train_name, train_no, boarded_from, travel_to, jdate, vacant, dep_time=dep_time)


def find_vacant_berths_sync(
    train_no: str,
    jdate: str,
    boarded_from: str,
    travel_to: str,
    class_filter: Optional[str] = None,
) -> str:
    """Synchronous wrapper — use for scripts/Flask/callers that cannot await."""
    return asyncio.run(
        find_vacant_berths(train_no, jdate, boarded_from, travel_to, class_filter)
    )
