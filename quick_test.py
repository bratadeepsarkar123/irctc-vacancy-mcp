"""
quick_test.py  —  Self-contained IRCTC vacancy test
=====================================================
Just run:  python quick_test.py

Auto-flow:
  1. Checks if server has a cookie set (/health)
  2. If no cookie → reads irctc_cookie.env → pushes to /set-cookie
  3. If irctc_cookie.env missing → runs grab_cookie.py to capture fresh cookies
  4. Calls /run → check_irctc_vacancy
  5. Also does a raw vacantBerth call to inspect the response structure
"""

import sys
import json
import os
import subprocess

try:
    from curl_cffi import requests as cffi_req
    _HAVE_CFFI = True
except ImportError:
    import requests as cffi_req
    _HAVE_CFFI = False

import urllib.request

BASE = "http://localhost:8000"
ENV_FILE = os.path.join(os.path.dirname(__file__), "irctc_cookie.env")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _http_get(path: str) -> dict | None:
    """Simple GET to server using stdlib (always works)."""
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(path: str, body: dict) -> dict | None:
    """Simple POST to server using stdlib."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{BASE}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  POST {path} error: {e}")
        return None


def read_cookie_from_env_file() -> str:
    """Read cookie string from irctc_cookie.env (PowerShell format)."""
    if not os.path.isfile(ENV_FILE):
        return ""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Handle: $env:IRCTC_COOKIE="..."
                if "IRCTC_COOKIE" in line and "=" in line:
                    _, _, val = line.partition("=")
                    return val.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def run_grab_cookie() -> str:
    """Run grab_cookie.py as subprocess and return the captured cookie."""
    print("\n  Running grab_cookie.py to capture fresh session cookies...")
    grab = os.path.join(os.path.dirname(__file__), "grab_cookie.py")
    result = subprocess.run(
        [sys.executable, grab],
        capture_output=False,  # show output live
    )
    if result.returncode != 0:
        print("  grab_cookie.py failed.")
        return ""
    return read_cookie_from_env_file()


def ensure_cookie() -> bool:
    """
    Make sure the server has a valid IRCTC cookie.
    Order: 1) already set  2) irctc_cookie.env  3) run grab_cookie.py
    Returns True if a cookie was successfully pushed.
    """
    health = _http_get("/health")
    if health and health.get("irctc_cookie_set"):
        print("  ✓ Server already has a cookie set.")
        return True

    print("  Server has no cookie. Trying irctc_cookie.env...")
    cookie = read_cookie_from_env_file()

    if not cookie:
        print("  irctc_cookie.env not found or empty. Launching grab_cookie.py...")
        cookie = run_grab_cookie()

    if not cookie:
        print("  ERROR: Could not obtain a cookie. Aborting.")
        return False

    # Push cookie to server via /set-cookie
    print(f"  Pushing cookie to server ({len(cookie)} chars)...")
    resp = _http_post("/set-cookie", {"cookie": cookie})
    if resp and resp.get("status") == "ok":
        print("  ✓ Cookie pushed to server successfully.")
        return True
    else:
        print(f"  ERROR pushing cookie: {resp}")
        return False


def section(label: str):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")


# ── Direct IRCTC API calls (bypass server, use curl_cffi directly) ────────────

def irctc_post(endpoint: str, body: dict, cookie: str) -> dict | None:
    """POST directly to IRCTC API with the session cookie."""
    kwargs = {"impersonate": "chrome124"} if _HAVE_CFFI else {}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.irctc.co.in",
        "Referer": "https://www.irctc.co.in/online-charts/",
        "Cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    try:
        r = cffi_req.post(
            f"https://www.irctc.co.in/online-charts/api/{endpoint}",
            json=body, headers=headers, timeout=20, **kwargs
        )
        return r.json()
    except Exception as e:
        print(f"  ERROR calling {endpoint}: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # ── 0. Cookie setup ───────────────────────────────────────────
    section("Cookie Setup")
    cookie_ok = ensure_cookie()

    # Read cookie for direct API calls below
    raw_cookie = read_cookie_from_env_file() or os.environ.get("IRCTC_COOKIE", "")

    # ── 1. Health ─────────────────────────────────────────────────
    section("GET /health")
    h = _http_get("/health")
    print(json.dumps(h, indent=2))

    # ── 2. MCP tool call via server ───────────────────────────────
    section("POST /run → check_irctc_vacancy (train 15708 DLI→CNB)")
    result = _http_post("/run", {
        "tool": "check_irctc_vacancy",
        "parameters": {
            "train_no": "15708",
            "journey_date": "2026-06-03",
            "boarded_from": "DLI",
            "travel_to": "CNB",
        },
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if not raw_cookie:
        print("\n  No cookie available for direct API tests. Skipping raw calls.")
        return

    # ── 3. Raw trainComposition ───────────────────────────────────
    section("RAW trainComposition (key inspection)")
    tc = irctc_post("trainComposition", {
        "trainNo": "15708", "jDate": "2026-06-03", "boardingStation": "DLI"
    }, raw_cookie)

    if not tc:
        print("  No response from trainComposition")
        return

    remote = tc.get("remote") or ""
    next_remote = tc.get("nextRemote") or ""
    print(f"  from={tc.get('from')} to={tc.get('to')}")
    print(f"  remote={remote!r}  nextRemote={next_remote!r}")
    print(f"  chartStatusResponseDto={json.dumps(tc.get('chartStatusResponseDto'), ensure_ascii=False)}")

    coaches = tc.get("cdd") or []
    print(f"  Coaches: {len(coaches)}")
    for c in coaches:
        print(f"    {c.get('coachName'):4} ({c.get('classCode'):3}) vacantBerths={c.get('vacantBerths')}")

    # Use remote (charting station) for coachComposition
    chart_stn = remote or "DLI"

    # ── 4. Raw vacantBerth ────────────────────────────────────────
    section(f"RAW vacantBerth (boardingStation={chart_stn}, cls=2A)")
    vb = irctc_post("vacantBerth", {
        "trainNo": "15708", "jDate": "2026-06-03",
        "boardingStation": chart_stn, "cls": "2A", "chartType": 1
    }, raw_cookie)

    if vb:
        print(f"  Keys: {list(vb.keys())}")
        vbd = vb.get("vbd") or []
        print(f"  vbd count: {len(vbd)}")
        if vbd:
            print(f"  First entry keys: {list(vbd[0].keys())}")
            print(f"  First 3 entries:\n{json.dumps(vbd[:3], indent=4, ensure_ascii=False)}")
        if vb.get("error"):
            print(f"  error: {vb['error']!r}")
    else:
        print("  No response")

    # ── 5. Raw coachComposition ───────────────────────────────────
    section(f"RAW coachComposition (coach=A1, boardingStation={chart_stn})")
    cc = irctc_post("coachComposition", {
        "trainNo": "15708", "jDate": "2026-06-03",
        "boardingStation": chart_stn, "coach": "A1", "cls": "2A"
    }, raw_cookie)

    if cc:
        print(f"  Keys: {list(cc.keys())}")
        bdd = cc.get("bdd") or []
        print(f"  bdd count: {len(bdd)}")
        if bdd:
            print(f"  First berth keys: {list(bdd[0].keys())}")
            print(f"  First berth:\n{json.dumps(bdd[0], indent=4, ensure_ascii=False)}")
        if cc.get("error"):
            print(f"  error: {cc['error']!r}")
    else:
        print("  No response")

    print("\n\nDone.")


if __name__ == "__main__":
    main()
