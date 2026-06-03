"""
vacancy_filter.py — Pure-Python vacancy filtering
==================================================
The AI/LLM receives ONLY the output of format_result().
No raw IRCTC data is ever passed to the AI.

Real bsd (berth segment data) structure confirmed from JS bundle analysis:
  bsd = [{
    occupancy: bool   -- True = this segment is occupied, False = vacant
    from:      str    -- station code where passenger boards
    to:        str    -- station code where passenger exits
    quota:     str    -- GN, GNRS, LD, DMGD, ...
  }]

Berth state logic (confirmed from JS enum):
  VACANT  (3) → bsd is []
  FULL    (1) → all bsd segments have occupancy=True
  PARTIAL (2) → some segments occupied, some vacant (gaps exist)
  Special     → quota=="GNRS" on VACANT berth → treat as PARTIAL
  Special     → quota=="DMGD" → damaged berth (show as disabled)
  Special     → enable==False → under repair (skip entirely)
"""

from typing import Optional


def station_index(station_list: list, code: str) -> int:
    """Return 0-based position of station code in the route. -1 if not found."""
    try:
        return station_list.index(code.upper())
    except ValueError:
        return -1


def vacant_windows_from_bsd(bsd: list, station_list: list) -> list:
    """
    Compute vacant windows on a berth from its bsd (berth segment data) array.
    Uses the real IRCTC bsd structure: [{occupancy, from, to, quota}]

    A segment with occupancy=False is itself a vacant window.
    A segment with occupancy=True is an occupied block.

    Returns: [{from: str, to: str}, ...] — all vacant windows.
    """
    if not bsd:
        # Berth entirely vacant for the full route
        if len(station_list) >= 2:
            return [{"from": station_list[0], "to": station_list[-1]}]
        return []

    windows = []
    stn_upper = [s.upper() for s in station_list]

    for seg in bsd:
        # occupancy=False means this segment IS vacant
        if not seg.get("occupancy", True):
            from_stn = (seg.get("from") or "").upper()
            to_stn = (seg.get("to") or "").upper()
            # Skip GNRS-quota segments even if marked vacant (reserved berths)
            if seg.get("quota") == "GNRS":
                continue
            if from_stn in stn_upper and to_stn in stn_upper:
                windows.append({"from": from_stn, "to": to_stn})

    return windows


def vacant_windows_from_booked(booked_segments: list, station_list: list) -> list:
    """
    Legacy/fallback method: compute vacant windows from a list of booked segments
    [{fromStn/fromStation, toStn/toStation}, ...].

    Used when the API returns passenger list data instead of bsd segments.
    """
    stn_upper = [s.upper() for s in station_list]

    if not booked_segments:
        if len(stn_upper) >= 2:
            return [{"from": stn_upper[0], "to": stn_upper[-1]}]
        return []

    def norm_seg(s: dict) -> Optional[dict]:
        f = (s.get("from") or s.get("fromStn") or s.get("fromStation") or
             s.get("boardingPoint") or "").upper()
        t = (s.get("to") or s.get("toStn") or s.get("toStation") or
             s.get("destination") or "").upper()
        if not f or not t:
            return None
        fi = station_index(stn_upper, f)
        if fi < 0:
            return None
        return {"fromStn": f, "toStn": t, "fi": fi}

    normed = [n for n in (norm_seg(s) for s in booked_segments) if n]
    occupied = sorted(normed, key=lambda s: s["fi"])

    windows = []
    prev_end = 0

    for seg in occupied:
        seg_start = station_index(stn_upper, seg["fromStn"])
        seg_end = station_index(stn_upper, seg["toStn"])
        if seg_start < 0 or seg_end < 0:
            continue
        if seg_start > prev_end:
            windows.append({
                "from": stn_upper[prev_end],
                "to": stn_upper[seg_start - 1],
            })
        prev_end = max(prev_end, seg_end + 1)

    if prev_end < len(stn_upper):
        windows.append({"from": stn_upper[prev_end], "to": stn_upper[-1]})

    return windows


def window_covers(window: dict, boarded_from: str, travel_to: str,
                  station_list: list) -> bool:
    """True if window fully covers the user's journey segment."""
    stn = [s.upper() for s in station_list]
    wi = station_index(stn, window["from"])
    wj = station_index(stn, window["to"])
    ui = station_index(stn, boarded_from.upper())
    uj = station_index(stn, travel_to.upper())
    if -1 in (wi, wj, ui, uj):
        return False
    return wi <= ui and uj <= wj


def _extract_bdd_berths(data: dict) -> list:
    """
    Extract berth list from coachComposition response.
    Primary key: bdd (confirmed from JS).
    """
    return data.get("bdd") or []


def _extract_fallback_berths(data: dict) -> list:
    """
    Extract berth list from vacantBerth or legacy response (keys unknown).
    Try all known key names in priority order.
    """
    return (
        data.get("berthList")
        or data.get("berths")
        or data.get("chartList")
        or data.get("coachBerthList")
        or []
    )


def filter_vacant(
    all_coach_data: list,
    boarded_from: str,
    travel_to: str,
    class_filter: Optional[str],
    station_list: list,
) -> list:
    """
    Main filter function. Processes all_coaches_async() output.

    Handles both coachComposition (bdd/bsd) and vacantBerth (fallback keys).
    Returns [{coachName, classCode, berth, berthCode, freeFrom, freeTo}, ...]
    sorted by classCode → coachName → berth number.
    """
    results = []
    b_from = boarded_from.upper()
    b_to = travel_to.upper()
    stn_upper = [s.upper() for s in station_list]

    for coach in all_coach_data:
        if "error" in coach:
            continue

        cls = coach.get("classCode", "")
        if class_filter and cls.upper() != class_filter.upper():
            continue

        data = coach.get("data", {})
        source = coach.get("source", "")

        # ── coachComposition path (bdd/bsd) ──────────────────
        if source == "coachComposition" or data.get("bdd"):
            for berth in _extract_bdd_berths(data):
                # Skip disabled/under-repair berths
                if berth.get("enable") is False:
                    continue

                berth_no = berth.get("berthNo", "?")
                berth_code = (berth.get("berthCode") or "").strip()

                # Skip damaged berths
                bsd = berth.get("bsd", [])
                if bsd and bsd[0].get("quota") == "DMGD":
                    continue

                windows = vacant_windows_from_bsd(bsd, stn_upper)
                for window in windows:
                    if window_covers(window, b_from, b_to, stn_upper):
                        results.append({
                            "coachName": coach["coachName"],
                            "classCode": cls,
                            "berth": berth_no,
                            "berthCode": berth_code,
                            "freeFrom": window["from"],
                            "freeTo": window["to"],
                        })
        else:
            # ── vacantBerth / legacy fallback path ────────────
            for berth in _extract_fallback_berths(data):
                berth_no = berth.get("berthNo") or berth.get("berth") or "?"
                berth_code = (berth.get("berthCode") or berth.get("berthType") or
                              berth.get("type") or "").strip()

                # Try bsd structure first (sometimes present in vacantBerth too)
                bsd = berth.get("bsd", [])
                if bsd:
                    windows = vacant_windows_from_bsd(bsd, stn_upper)
                else:
                    # Fall back to passenger list / segment list
                    passengers = (
                        berth.get("passengerList")
                        or berth.get("passengers")
                        or berth.get("bookingList")
                        or []
                    )
                    windows = vacant_windows_from_booked(passengers, stn_upper)

                for window in windows:
                    if window_covers(window, b_from, b_to, stn_upper):
                        results.append({
                            "coachName": coach["coachName"],
                            "classCode": cls,
                            "berth": berth_no,
                            "berthCode": berth_code,
                            "freeFrom": window["from"],
                            "freeTo": window["to"],
                        })

    def _sort_key(x):
        try:
            return (x["classCode"], x["coachName"], int(str(x["berth"])))
        except (ValueError, TypeError):
            return (x["classCode"], x["coachName"], str(x["berth"]))

    return sorted(results, key=_sort_key)


def format_result(
    train_name: str,
    train_no: str,
    boarded_from: str,
    travel_to: str,
    jdate: str,
    vacant: list,
) -> str:
    """
    Convert filtered results into compact human-readable string.
    THIS is the only thing the AI ever receives — never raw IRCTC data.
    """
    header = (
        f"Train {train_no} ({train_name}) | {jdate}\n"
        f"Your segment: {boarded_from} → {travel_to}"
    )

    if not vacant:
        return f"{header}\nNo vacant berths found for your segment."

    lines = [
        header,
        f"Vacant berths found: {len(vacant)}",
        "",
    ]

    current_coach = None
    for v in vacant:
        if v["coachName"] != current_coach:
            current_coach = v["coachName"]
            lines.append(f"── Coach {current_coach} ({v['classCode']}) ──")

        bt = f" [{v['berthCode']}]" if v["berthCode"] else ""
        lines.append(
            f"  Berth {str(v['berth']).rjust(3)}{bt}  "
            f"free {v['freeFrom']} → {v['freeTo']}"
        )

    return "\n".join(lines)
