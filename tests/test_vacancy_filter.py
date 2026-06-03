"""
tests/test_vacancy_filter.py
Unit tests for the pure-Python vacancy filtering logic.
Run: pytest tests/test_vacancy_filter.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from vacancy_filter import (
    station_index,
    vacant_windows_from_bsd,
    vacant_windows_from_booked,
    window_covers,
    filter_vacant,
    format_result,
)

# ── Fixture: a simple 6-station route ─────────────────────────────────────────
ROUTE = ["NDLS", "CNB", "PRYJ", "MGS", "PNBE", "HWH"]


# ── station_index ──────────────────────────────────────────────────────────────

def test_station_index_found():
    assert station_index(ROUTE, "CNB") == 1

def test_station_index_case_insensitive():
    assert station_index(ROUTE, "pnbe") == 4

def test_station_index_not_found():
    assert station_index(ROUTE, "DBRT") == -1


# ── vacant_windows_from_bsd ────────────────────────────────────────────────────

def test_bsd_empty_means_fully_vacant():
    """Empty bsd → berth free for full route."""
    windows = vacant_windows_from_bsd([], ROUTE)
    assert len(windows) == 1
    assert windows[0]["from"] == "NDLS"
    assert windows[0]["to"] == "HWH"

def test_bsd_all_occupied():
    """All segments occupied → no vacant windows."""
    bsd = [
        {"occupancy": True, "from": "NDLS", "to": "CNB", "quota": "GN"},
        {"occupancy": True, "from": "CNB", "to": "HWH", "quota": "GN"},
    ]
    windows = vacant_windows_from_bsd(bsd, ROUTE)
    assert len(windows) == 0

def test_bsd_one_vacant_segment():
    """Single vacant segment in bsd."""
    bsd = [
        {"occupancy": True,  "from": "NDLS", "to": "CNB",  "quota": "GN"},
        {"occupancy": False, "from": "CNB",  "to": "PRYJ", "quota": "GN"},
        {"occupancy": True,  "from": "PRYJ", "to": "HWH",  "quota": "GN"},
    ]
    windows = vacant_windows_from_bsd(bsd, ROUTE)
    assert len(windows) == 1
    assert windows[0]["from"] == "CNB"
    assert windows[0]["to"] == "PRYJ"

def test_bsd_gnrs_skipped():
    """GNRS quota segments should not appear as vacant (reserved berths)."""
    bsd = [
        {"occupancy": False, "from": "NDLS", "to": "HWH", "quota": "GNRS"},
    ]
    windows = vacant_windows_from_bsd(bsd, ROUTE)
    assert len(windows) == 0

def test_bsd_multiple_vacant_segments():
    """Multiple vacant gaps."""
    bsd = [
        {"occupancy": False, "from": "NDLS", "to": "CNB",  "quota": "GN"},
        {"occupancy": True,  "from": "CNB",  "to": "MGS",  "quota": "GN"},
        {"occupancy": False, "from": "MGS",  "to": "HWH",  "quota": "GN"},
    ]
    windows = vacant_windows_from_bsd(bsd, ROUTE)
    assert len(windows) == 2
    froms = [w["from"] for w in windows]
    assert "NDLS" in froms
    assert "MGS" in froms


# ── vacant_windows_from_booked (legacy path) ───────────────────────────────────

def test_booked_empty_means_fully_vacant():
    windows = vacant_windows_from_booked([], ROUTE)
    assert len(windows) == 1
    assert windows[0]["from"] == "NDLS"
    assert windows[0]["to"] == "HWH"

def test_booked_full_journey_booking():
    """One booking covering full route → no vacant windows."""
    booked = [{"fromStn": "NDLS", "toStn": "HWH"}]
    windows = vacant_windows_from_booked(booked, ROUTE)
    # After NDLS-HWH is occupied, there's nothing left
    assert all(w["from"] == w["to"] for w in windows) or len(windows) == 0

def test_booked_gap_in_middle():
    """Passenger A: NDLS→CNB, Passenger B: MGS→HWH → gap exists between CNB and MGS."""
    # Use a route where there IS a station between the two bookings
    route = ["NDLS", "CNB", "PRYJ", "MGS", "PNBE", "HWH"]
    booked = [
        {"fromStn": "NDLS", "toStn": "CNB"},   # occupies NDLS→CNB (idx 0→1)
        {"fromStn": "MGS",  "toStn": "HWH"},   # occupies MGS→HWH  (idx 3→5)
    ]
    windows = vacant_windows_from_booked(booked, route)
    # Gap should exist between CNB (1) and MGS (3): i.e. PRYJ is free
    assert len(windows) >= 1
    # At least one window should start after CNB and end before MGS
    has_gap = any(
        station_index(route, w["from"]) >= station_index(route, "PRYJ")
        and station_index(route, w["to"]) <= station_index(route, "PRYJ")
        for w in windows
    )
    assert has_gap

def test_booked_alternative_key_names():
    """Supports from/fromStation/boardingPoint key variants."""
    booked = [{"fromStation": "NDLS", "toStation": "MGS"}]
    windows = vacant_windows_from_booked(booked, ROUTE)
    # Should find a gap after MGS
    has_post_gap = any(
        station_index(ROUTE, w["from"]) > station_index(ROUTE, "MGS")
        for w in windows
    )
    assert has_post_gap


# ── window_covers ──────────────────────────────────────────────────────────────

def test_window_covers_exact_match():
    window = {"from": "NDLS", "to": "CNB"}
    assert window_covers(window, "NDLS", "CNB", ROUTE)

def test_window_covers_user_segment_inside():
    window = {"from": "NDLS", "to": "HWH"}
    assert window_covers(window, "CNB", "PNBE", ROUTE)

def test_window_does_not_cover_partial():
    window = {"from": "CNB", "to": "PRYJ"}
    assert not window_covers(window, "NDLS", "CNB", ROUTE)

def test_window_covers_unknown_station():
    window = {"from": "NDLS", "to": "HWH"}
    assert not window_covers(window, "NDLS", "DBRT", ROUTE)


# ── filter_vacant (integration) ────────────────────────────────────────────────

def _mock_coach_data(bsd: list, coach_name: str = "S4",
                     class_code: str = "SL") -> list:
    """Build mock all_coaches_async() output."""
    return [{
        "coachName": coach_name,
        "classCode": class_code,
        "source": "coachComposition",
        "data": {
            "bdd": [{
                "berthNo": 32,
                "berthCode": "LB",
                "enable": True,
                "bsd": bsd,
            }]
        },
    }]

def test_filter_finds_vacant_berth():
    bsd = []  # fully vacant
    data = _mock_coach_data(bsd)
    results = filter_vacant(data, "NDLS", "CNB", None, ROUTE)
    assert len(results) == 1
    assert results[0]["coachName"] == "S4"
    assert results[0]["berth"] == 32
    assert results[0]["berthCode"] == "LB"

def test_filter_no_result_when_full():
    bsd = [{"occupancy": True, "from": "NDLS", "to": "HWH", "quota": "GN"}]
    data = _mock_coach_data(bsd)
    results = filter_vacant(data, "NDLS", "CNB", None, ROUTE)
    assert len(results) == 0

def test_filter_class_filter_applied():
    bsd = []
    data = _mock_coach_data(bsd, class_code="SL")
    results = filter_vacant(data, "NDLS", "CNB", "3A", ROUTE)
    assert len(results) == 0  # SL coach excluded when filtering for 3A

def test_filter_disabled_berth_skipped():
    data = [{
        "coachName": "S4",
        "classCode": "SL",
        "source": "coachComposition",
        "data": {
            "bdd": [{
                "berthNo": 1,
                "berthCode": "LB",
                "enable": False,  # disabled
                "bsd": [],
            }]
        },
    }]
    results = filter_vacant(data, "NDLS", "CNB", None, ROUTE)
    assert len(results) == 0

def test_filter_dmgd_berth_skipped():
    data = [{
        "coachName": "S4",
        "classCode": "SL",
        "source": "coachComposition",
        "data": {
            "bdd": [{
                "berthNo": 5,
                "berthCode": "MB",
                "enable": True,
                "bsd": [{"occupancy": False, "from": "NDLS", "to": "HWH", "quota": "DMGD"}],
            }]
        },
    }]
    results = filter_vacant(data, "NDLS", "CNB", None, ROUTE)
    assert len(results) == 0

def test_filter_error_coach_skipped():
    data = [{"coachName": "S4", "classCode": "SL", "error": "timeout"}]
    results = filter_vacant(data, "NDLS", "CNB", None, ROUTE)
    assert len(results) == 0


# ── format_result ──────────────────────────────────────────────────────────────

def test_format_result_no_vacant():
    result = format_result("RAJDHANI EXP", "12302", "NDLS", "CNB", "2026-06-04", [])
    assert "No vacant berths" in result
    assert "12302" in result

def test_format_result_with_berths():
    vacant = [{
        "coachName": "S4",
        "classCode": "SL",
        "berth": 32,
        "berthCode": "LB",
        "freeFrom": "NDLS",
        "freeTo": "CNB",
    }]
    result = format_result("TEST EXP", "12302", "NDLS", "CNB", "2026-06-04", vacant)
    assert "Vacant berths found: 1" in result
    assert "Coach S4" in result
    assert "32" in result
    assert "LB" in result
