"""Quick test of live running status extraction."""
import sys
sys.path.insert(0, "src")
from train_schedule import _fetch_live_running_status

result = _fetch_live_running_status("15708")
if not result:
    print("FAILED - no result returned")
    sys.exit(1)

print("=== LIVE STATUS FOR TRAIN 15708 ===")
for k, v in result.items():
    if k == "per_station_delays":
        print(f"  {k}: {len(v)} stations")
        # Show last 3 passed
        passed = [s for s in v if s.get("status") == "passed"]
        if passed:
            print(f"    Last 3 passed:")
            for s in passed[-3:]:
                print(f"      {s['code']}: arr_delay={s.get('arr_delay','--')}, dep_delay={s.get('dep_delay','--')}, actual_arr={s.get('actual_arr','--')}")
        upcoming = [s for s in v if s.get("status") == "upcoming"]
        if upcoming:
            print(f"    Next 2 upcoming:")
            for s in upcoming[:2]:
                print(f"      {s['code']}: status=upcoming")
    else:
        print(f"  {k}: {v}")

print("\nSUCCESS!")
