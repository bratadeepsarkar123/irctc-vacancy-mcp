"""
main_tool.py  —  Single entry point for both MCP servers.

Pipeline:
  1. train_composition()       -> coach list + train meta
  2. train_schedule()          -> full ordered station list  <-- NEW
     fallback: stationList from composition if schedule call fails
  3. all_vacant_async()        -> parallel vacantBerth for every coach
  4. filter_vacant()           -> pure Python gap-finding, no AI
  5. format_result()           -> compact string; THIS is what the AI sees

The AI/LLM never receives raw API data.
"""

import asyncio
from typing import Optional

from .irctc_api import (
    train_composition,
    train_schedule,
    _fetch_schedule_async,
    all_vacant_async,
)
from .vacancy_filter import filter_vacant, format_result


def _stations_from_composition(comp: dict) -> list:
    """
    Fallback: extract whatever station info the composition endpoint returned.
    trainComposition only gives from/remote (2 points), which is not enough
    for mid-route windows — but it is a usable last resort.
    """
    raw = comp.get("stationList") or []
    if raw and isinstance(raw[0], dict):
        return [
            (
                s.get("stationCode") or s.get("stnCode") or s.get("value") or ""
            ).upper()
            for s in raw
            if (s.get("stationCode") or s.get("stnCode") or s.get("value"))
        ]
    if raw and isinstance(raw[0], str):
        return [s.upper() for s in raw if s]

    # Absolute fallback: just the two endpoints we know
    src   = (comp.get("from") or comp.get("trainSourceStation") or "").upper()
    dest  = (comp.get("to")   or comp.get("destination")        or "").upper()
    return [s for s in [src, dest] if s]


async def find_vacant_berths(
    train_no: str,
    jdate: str,          # "YYYY-MM-DD"
    boarded_from: str,   # e.g. "NDLS"
    travel_to: str,      # e.g. "CNB"
    class_filter: Optional[str] = None,  # e.g. "SL", "3A"; None = all classes
) -> str:
    """
    Async orchestrator. Returns the pre-filtered, formatted string
    that the AI should present directly to the user.
    """
    boarded_from = boarded_from.upper().strip()
    travel_to    = travel_to.upper().strip()

    # ── Step 1: Train composition ────────────────────────────
    try:
        comp = train_composition(train_no, jdate, boarded_from)
    except Exception as e:
        return f"Error fetching train composition: {e}"

    train_name   = comp.get("trainName") or comp.get("name") or train_no
    remote       = (comp.get("remote") or comp.get("remoteStation") or boarded_from).upper()
    source       = (comp.get("from")   or comp.get("trainSourceStation") or boarded_from).upper()

    coaches_raw  = comp.get("cdd") or comp.get("coachList") or comp.get("coaches") or []
    if not coaches_raw:
        return f"No coach data returned for train {train_no}. Chart may not be prepared yet."

    # Normalise coach list to [{coachName, classCode}]
    coaches = [
        {
            "coachName": c.get("coachName") or c.get("coach") or c.get("coachId") or "",
            "classCode": c.get("classCode") or c.get("cls")   or c.get("coachClass") or "",
        }
        for c in coaches_raw
        if (c.get("coachName") or c.get("coach") or c.get("coachId"))
    ]

    # ── Step 2: Station order from schedule endpoint ─────────
    # Run schedule fetch concurrently with nothing (we need it before Step 3,
    # but we can at least await it cleanly in async context).
    station_list = await _fetch_schedule_async(train_no)

    if not station_list:
        # Fallback: use whatever the composition gave us
        station_list = _stations_from_composition(comp)

    if not station_list:
        return (
            f"Could not retrieve station list for train {train_no}. "
            "Try again — schedule endpoint may be temporarily unavailable."
        )

    # Validate user stations are actually on this train's route
    if boarded_from not in station_list:
        return (
            f"Station {boarded_from!r} not found in {train_no} route.\n"
            f"Known stations (first 10): {station_list[:10]}"
        )
    if travel_to not in station_list:
        return (
            f"Station {travel_to!r} not found in {train_no} route.\n"
            f"Known stations (first 10): {station_list[:10]}"
        )
    if station_list.index(boarded_from) >= station_list.index(travel_to):
        return (
            f"{boarded_from} appears after {travel_to} in this train's route. "
            "Please check the station codes."
        )

    # ── Step 3: Parallel vacantBerth for all coaches ─────────
    try:
        all_data = await all_vacant_async(
            train_no, boarded_from, remote, source, jdate, coaches
        )
    except Exception as e:
        return f"Error fetching berth data: {e}"

    # ── Step 4: Pure-Python filter ───────────────────────────
    vacant = filter_vacant(all_data, boarded_from, travel_to, class_filter, station_list)

    # ── Step 5: Format for AI ────────────────────────────────
    return format_result(train_name, train_no, boarded_from, travel_to, jdate, vacant)


def find_vacant_berths_sync(
    train_no: str,
    jdate: str,
    boarded_from: str,
    travel_to: str,
    class_filter: Optional[str] = None,
) -> str:
    """Synchronous wrapper for callers that cannot await (e.g. Flask, simple scripts)."""
    return asyncio.run(
        find_vacant_berths(train_no, jdate, boarded_from, travel_to, class_filter)
    )
