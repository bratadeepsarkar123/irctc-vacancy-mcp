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


def _extract_vbd_entries(data: dict) -> list:
    """
    Extract vacant segment entries from vacantBerth response.
    Confirmed key: `vbd` (verified from live API response 2026-06-03).

    Each vbd entry represents ONE vacant window for ONE berth:
      {fromStation, toStation, berthNo, berthCode, coachName, ...}

    These are already-computed vacant windows, NOT passenger segments.
    """
    return data.get("vbd") or []


def _extract_fallback_berths(data: dict) -> list:
    """
    Extract berth list from vacantBerth or legacy response.
    Try all known key names in priority order.
    NOTE: vacantBerth returns vbd[], not berths inside a berth wrapper.
    This function is for legacy coachComposition-like berth-per-row formats.
    """
    return (
        data.get("berthList")
        or data.get("berths")
        or data.get("chartList")
        or data.get("coachBerthList")
        or []
    )


def _norm_station(code: str, station_list: list) -> str:
    """Return the station code uppercased. Check if it exists in station_list."""
    return code.upper() if code else ""


def filter_vacant(
    all_coach_data: list,
    boarded_from: str,
    travel_to: str,
    class_filter: Optional[str],
    station_list: list,
) -> list:
    """
    Main filter function. Processes all_coaches_async() output.

    Handles three response formats:
      1. coachComposition → bdd[]{bsd[]{occupancy, from, to, quota}}
      2. vacantBerth → vbd[]{fromStation, toStation, berthNo, berthCode} (confirmed 2026-06-03)
      3. Legacy fallback → berthList/berths/chartList[]

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

        # ── Path 1: coachComposition (bdd/bsd) ──────────────────
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

        # ── Path 2: vacantBerth (vbd[]) — CONFIRMED key 2026-06-03 ──
        elif data.get("vbd") is not None:
            # Each vbd entry IS a vacant window — no further decomposition needed.
            # Keys (to be confirmed from live response with cookies):
            #   fromStation / from  → start of vacant window
            #   toStation / to      → end of vacant window
            #   berthNo             → berth number
            #   berthCode           → LB/UB/MB/SL/SU
            #   coach / coachName   → coach identifier (may not be in vbd entry)
            for entry in _extract_vbd_entries(data):
                # Normalise from/to station keys
                from_stn = (
                    entry.get("fromStation") or entry.get("from")
                    or entry.get("fromStn") or ""
                ).upper()
                to_stn = (
                    entry.get("toStation") or entry.get("to")
                    or entry.get("toStn") or ""
                ).upper()
                berth_no = entry.get("berthNo") or entry.get("berth") or entry.get("berthNumber") or "?"
                berth_code = (entry.get("berthCode") or entry.get("berthType") or "").strip()

                if not from_stn or not to_stn:
                    continue

                window = {"from": from_stn, "to": to_stn}
                if window_covers(window, b_from, b_to, stn_upper):
                    results.append({
                        "coachName": coach["coachName"],
                        "classCode": cls,
                        "berth": berth_no,
                        "berthCode": berth_code,
                        "freeFrom": from_stn,
                        "freeTo": to_stn,
                    })

        else:
            # ── Path 3: Legacy fallback ────────────────────────
            for berth in _extract_fallback_berths(data):
                berth_no = berth.get("berthNo") or berth.get("berth") or "?"
                berth_code = (berth.get("berthCode") or berth.get("berthType") or
                              berth.get("type") or "").strip()

                bsd = berth.get("bsd", [])
                if bsd:
                    windows = vacant_windows_from_bsd(bsd, stn_upper)
                else:
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
    dep_time: str = "",  # Optional departure time string e.g. "19:10" for better messaging
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
        # Build a helpful 'no berths' message that gives the AI context
        dep_hint = f" (departs {dep_time})" if dep_time else ""
        return (
            f"{header}\n"
            f"No vacant berths found for {boarded_from} → {travel_to}.\n"
            f"\n"
            f"POSSIBLE REASONS:\n"
            f"  1. The train hasn't departed yet{dep_hint} — IRCTC shows 0 berths until\n"
            f"     closer to departure or after chart finalization.\n"
            f"  2. All berths on this segment are genuinely occupied.\n"
            f"  3. The segment is valid but has no RAC/vacant allocations for this quota.\n"
            f"\n"
            f"SUGGESTION: If the train hasn't departed yet, check again 2-3 hours before\n"
            f"departure when IRCTC finalizes no-shows and RAC berths."
        )

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
