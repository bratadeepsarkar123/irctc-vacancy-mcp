"""
edge_case_tests.py — Systematic edge case testing for IRCTC MCP tools
"""
import asyncio
import sys
import os

sys.path.insert(0, 'src')
# Make sure cookie is loaded
import re
from pathlib import Path
raw = Path("irctc_cookie.env").read_text(encoding="utf-8")
m = re.search(r'\$env:IRCTC_COOKIE="(.+)"', raw)
if m:
    os.environ["IRCTC_COOKIE"] = m.group(1)
    print(f"[SETUP] Cookie loaded ({len(m.group(1))} chars)\n")

from main_tool import find_vacant_berths
from irctc_api import _fetch_schedule_async


async def run_test(name, coro):
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print('='*60)
    try:
        result = await coro
        # Show first 3 lines + last line
        lines = result.strip().splitlines()
        if len(lines) <= 6:
            print(result)
        else:
            for l in lines[:4]:
                print(l)
            print(f"  ... ({len(lines)-4} more lines)")
            print(lines[-1])
        print(f"[RESULT] {len(lines)} lines, {len(result)} chars")
    except Exception as e:
        print(f"[EXCEPTION] {type(e).__name__}: {e}")


async def main():
    tests = [
        # 1. Happy path - confirmed working
        ("1. Happy path: MBDP→LKO train 12184 June-03",
         find_vacant_berths("12184", "2026-06-03", "MBDP", "LKO")),

        # 2. Class filter - only 2A
        ("2. Class filter: only 2A coach",
         find_vacant_berths("12184", "2026-06-03", "MBDP", "LKO", class_filter="2A")),

        # 3. Class filter - only SL
        ("3. Class filter: only SL",
         find_vacant_berths("12184", "2026-06-03", "MBDP", "LKO", class_filter="SL")),

        # 4. Class filter - non-existent class
        ("4. Class filter: class not on train (EC)",
         find_vacant_berths("12184", "2026-06-03", "MBDP", "LKO", class_filter="EC")),

        # 5. Invalid/wrong station code (not on route)
        ("5. Station not on route: XYZ→LKO",
         find_vacant_berths("12184", "2026-06-03", "XYZ", "LKO")),

        # 6. Reversed stations (from > to in route order)
        ("6. Reversed stations: LKO→MBDP (wrong direction)",
         find_vacant_berths("12184", "2026-06-03", "LKO", "MBDP")),

        # 7. Chart not prepared (today, likely not prepared yet)
        ("7. Chart not prepared: today June-04",
         find_vacant_berths("12184", "2026-06-04", "MBDP", "LKO")),

        # 8. Past date (chart may be archived)
        ("8. Past date: June-01 (a few days ago)",
         find_vacant_berths("12184", "2026-06-01", "MBDP", "LKO")),

        # 9. Invalid train number
        ("9. Invalid train: 99999",
         find_vacant_berths("99999", "2026-06-03", "NDLS", "CNB")),

        # 10. DD-MM-YYYY date format
        ("10. Date format DD-MM-YYYY: 03-06-2026",
         find_vacant_berths("12184", "03-06-2026", "MBDP", "LKO")),

        # 11. Different train - 12542 (Ltt Gorakhpur SF Express)
        ("11. Different train: 12542 NDLS→CNB today",
         find_vacant_berths("12542", "2026-06-04", "NDLS", "CNB")),

        # 12. Same station from and to
        ("12. Same station: MBDP→MBDP",
         find_vacant_berths("12184", "2026-06-03", "MBDP", "MBDP")),

        # 13. get_train_route
        ("13. get_train_route: train 12184",
         _fetch_schedule_async("12184")),

        # 14. AME on train 12184 (user's original confusion)
        ("14. AME station on train 12184 route?",
         find_vacant_berths("12184", "2026-06-03", "AME", "LKO")),
    ]

    for name, coro in tests:
        await run_test(name, coro)

    print(f"\n{'='*60}")
    print("ALL TESTS DONE")
    print('='*60)


asyncio.run(main())
