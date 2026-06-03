"""
All filtering, sorting, and formatting logic.
The AI/LLM never sees raw API data — only the output of format_result().

No changes to core logic from previous version.
Minor: added Optional import for py3.9 compatibility.
"""

from typing import Optional


def station_index(station_list: list, code: str) -> int:
    """Return position of station code in route, -1 if not found."""
    try:
        return station_list.index(code.upper())
    except ValueError:
        return -1


def vacant_windows(booked_segments: list, station_list: list) -> list:
    """
    Given booked segments [{fromStn, toStn}, ...] on a single berth,
    compute the FREE windows as [{from, to}, ...].

    Example:
      Route: [A, B, C, D, E, F]
      Booked: B->D, E->F
      Free windows: A->A (before B), D->E-1 gap
      (edge handling: prev_end tracks last occupied station index + 1)
    """
    if not booked_segments:
        if len(station_list) >= 2:
            return [{"from": station_list[0], "to": station_list[-1]}]
        return []

    valid = [
        s for s in booked_segments
        if station_index(station_list, s.get("fromStn", "")) >= 0
    ]
    occupied = sorted(valid, key=lambda s: station_index(station_list, s["fromStn"]))

    windows = []
    prev_end = 0

    for seg in occupied:
        seg_start = station_index(station_list, seg["fromStn"])
        seg_end   = station_index(station_list, seg["toStn"])
        if seg_start < 0 or seg_end < 0:
            continue
        if seg_start > prev_end:
            windows.append({
                "from": station_list[prev_end],
                "to":   station_list[seg_start - 1],
            })
        prev_end = max(prev_end, seg_end + 1)

    if prev_end < len(station_list):
        windows.append({
            "from": station_list[prev_end],
            "to":   station_list[-1],
        })

    return windows


def window_covers(window: dict, boarded_from: str, travel_to: str,
                  station_list: list) -> bool:
    """True if user segment [boarded_from -> travel_to] fits entirely inside window."""
    wi = station_index(station_list, window["from"])
    wj = station_index(station_list, window["to"])
    ui = station_index(station_list, boarded_from)
    uj = station_index(station_list, travel_to)
    if -1 in (wi, wj, ui, uj):
        return False
    return wi <= ui and uj <= wj


def filter_vacant(all_coach_data: list, boarded_from: str, travel_to: str,
                  class_filter: Optional[str], station_list: list) -> list:
    """
    Main filter. Input: raw all_vacant_async() output.
    Output: [{coachName, classCode, berth, berthType, freeFrom, freeTo}, ...]
    Sorted by classCode -> coachName -> berth number.
    """
    results = []
    b_from    = boarded_from.upper()
    b_to      = travel_to.upper()
    stn_upper = [s.upper() for s in station_list]

    for coach in all_coach_data:
        if "error" in coach:
            continue
        cls = coach["classCode"]
        if class_filter and cls.upper() != class_filter.upper():
            continue

        data       = coach.get("data", {})
        chart_list = (
            data.get("chartList")
            or data.get("berths")
            or data.get("coachBerthList")
            or []
        )

        for berth in chart_list:
            berth_no   = berth.get("berthNo") or berth.get("berth") or "?"
            berth_type = (berth.get("berthType") or berth.get("type") or "").strip()
            passengers = (
                berth.get("passengerList")
                or berth.get("passengers")
                or berth.get("bookingList")
                or []
            )

            booked_segs = [
                {
                    "fromStn": (p.get("fromStation") or p.get("fromStn") or "").upper(),
                    "toStn":   (p.get("toStation")   or p.get("toStn")   or "").upper(),
                }
                for p in passengers
                if (p.get("fromStation") or p.get("fromStn"))
            ]

            for window in vacant_windows(booked_segs, stn_upper):
                if window_covers(window, b_from, b_to, stn_upper):
                    results.append({
                        "coachName": coach["coachName"],
                        "classCode": cls,
                        "berth":     berth_no,
                        "berthType": berth_type,
                        "freeFrom":  window["from"],
                        "freeTo":    window["to"],
                    })

    def sort_key(x):
        try:
            return (x["classCode"], x["coachName"], int(str(x["berth"])))
        except ValueError:
            return (x["classCode"], x["coachName"], str(x["berth"]))

    return sorted(results, key=sort_key)


def format_result(train_name: str, train_no: str, boarded_from: str,
                  travel_to: str, jdate: str, vacant: list) -> str:
    """
    Compact human-readable string — THIS is what the AI receives.
    AI never sees raw API data.
    """
    if not vacant:
        return (
            f"Train {train_no} ({train_name}) | {jdate}\n"
            f"Segment: {boarded_from} -> {travel_to}\n"
            f"No vacant berths found for your segment."
        )

    lines = [
        f"Train {train_no} ({train_name}) | {jdate}",
        f"Your segment: {boarded_from} -> {travel_to}",
        f"Vacant berths found: {len(vacant)}",
        "",
    ]

    current_coach = None
    for v in vacant:
        if v["coachName"] != current_coach:
            current_coach = v["coachName"]
            lines.append(f"-- Coach {current_coach} ({v['classCode']}) --")
        bt    = f" [{v['berthType']}]" if v["berthType"] else ""
        lines.append(
            f"  Berth {str(v['berth']).rjust(3)}{bt}  free {v['freeFrom']} -> {v['freeTo']}"
        )

    return "\n".join(lines)
