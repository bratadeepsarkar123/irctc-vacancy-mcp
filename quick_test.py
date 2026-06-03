"""
quick_test.py  —  Run this directly on your local machine
Usage: python quick_test.py
"""
import sys
import json
import requests

BASE = "http://localhost:8000"

def run(label, method, url, **kwargs):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        r = getattr(requests, method)(url, timeout=90, **kwargs)
        print(f"  HTTP {r.status_code}")
        data = r.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return data
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

# ── 1. Health ────────────────────────────────────────────────────
run("GET /health", "get", f"{BASE}/health")

# ── 2. Tools manifest ────────────────────────────────────────────
run("GET /tools", "get", f"{BASE}/tools")

# ── 3. Real IRCTC call (train 12424, CNB→LKO, tomorrow) ─────────
run(
    "POST /run  →  check_irctc_vacancy (train 12424 CNB→LKO)",
    "post",
    f"{BASE}/run",
    json={
        "tool": "check_irctc_vacancy",
        "parameters": {
            "train_no": "12424",
            "journey_date": "2026-06-04",
            "boarded_from": "CNB",
            "travel_to": "LKO",
        },
    },
)

# ── 4. Raw trainComposition (to verify bdd/cdd key names) ────────
print("\n" + "="*60)
print("  RAW trainComposition response (key inspection)")
print("="*60)
try:
    r = requests.post(
        "https://www.irctc.co.in/online-charts/api/trainComposition",
        json={"trainNo": "12424", "jDate": "2026-06-04", "boardingStation": "CNB"},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://www.irctc.co.in",
            "Referer": "https://www.irctc.co.in/online-charts/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        timeout=15,
    )
    print(f"  HTTP {r.status_code}")
    data = r.json()
    print("  Top-level keys:", list(data.keys()))
    coaches = data.get("cdd") or data.get("coachList") or []
    print(f"  Coaches (cdd): {len(coaches)}")
    if coaches:
        print("  First coach keys:", list(coaches[0].keys()))
        print("  First coach sample:", json.dumps(coaches[0], indent=4, ensure_ascii=False))
    # If we got coaches, fetch coachComposition for first one
    if coaches:
        c = coaches[0]
        coach_name = c.get("coachName") or c.get("coach", "")
        class_code = c.get("classCode") or c.get("cls", "")
        print(f"\n  Fetching coachComposition for {coach_name} ({class_code})...")
        r2 = requests.post(
            "https://www.irctc.co.in/online-charts/api/coachComposition",
            json={"trainNo": "12424", "jDate": "2026-06-04", "boardingStation": "CNB",
                  "coach": coach_name, "cls": class_code},
            headers=r.request.headers,
            timeout=15,
        )
        print(f"  HTTP {r2.status_code}")
        d2 = r2.json()
        print("  coachComposition keys:", list(d2.keys()))
        bdd = d2.get("bdd") or []
        print(f"  Berths (bdd): {len(bdd)}")
        if bdd:
            print("  First berth keys:", list(bdd[0].keys()))
            print("  First berth sample:", json.dumps(bdd[0], indent=4, ensure_ascii=False))
except Exception as e:
    print(f"  ERROR: {e}")

print("\nDone. Paste output here for analysis.")
