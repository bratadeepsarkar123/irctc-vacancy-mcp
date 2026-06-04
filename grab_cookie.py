"""
grab_cookie.py  —  ONE-TIME cookie capture for IRCTC chart APIs
================================================================
Launches a fresh Chrome window with remote debugging, navigates to
https://www.irctc.co.in/online-charts/, then captures cookies via CDP.

Usage:
    python grab_cookie.py

Then paste the printed $env:IRCTC_COOKIE=... line in your server terminal
and restart uvicorn.

Cookies last ~30 min. Re-run when they expire.
"""

import sys
import json
import os
import subprocess
import tempfile
import time
import socket
import urllib.request
import urllib.error
import threading

CDP_PORT = 9222
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
IRCTC_URL = "https://www.irctc.co.in/online-charts/"


# ── Port / CDP helpers ────────────────────────────────────────────────────────

def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Low-level TCP check — works regardless of which HTTP client is installed."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cdp_get(path: str) -> dict | None:
    """Simple HTTP GET to the CDP endpoint using stdlib (no curl_cffi)."""
    try:
        url = f"{CDP_BASE}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def check_chrome_debug_port() -> bool:
    """Return True if Chrome debugging port 9222 is responding."""
    if not _port_open(CDP_PORT):
        return False
    info = _cdp_get("/json/version")
    if info:
        print(f"  Chrome debug found: {info.get('Browser', 'unknown')}")
        return True
    return False


# ── Chrome launcher ───────────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    """Return path to chrome.exe on Windows."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def launch_debug_chrome() -> bool:
    """
    Launch Chrome with remote debugging in a FRESH temp profile.
    Using a temp profile avoids the 'already running' conflict with
    your normal Chrome session.
    """
    chrome = _find_chrome()
    if not chrome:
        print("  ERROR: chrome.exe not found. Install Google Chrome.")
        return False

    # Fresh temp profile = no conflict with existing Chrome instance
    temp_profile = tempfile.mkdtemp(prefix="irctc_debug_chrome_")

    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={temp_profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        IRCTC_URL,
    ]

    print(f"  Launching Chrome with debugging: {chrome}")
    print(f"  Profile: {temp_profile}")
    subprocess.Popen(args, close_fds=True)
    return True


def wait_for_port(secs: int = 15) -> bool:
    """Poll port 9222 until Chrome responds."""
    print(f"  Waiting up to {secs}s for Chrome to start...", end="", flush=True)
    for _ in range(secs * 2):
        if _port_open(CDP_PORT) and _cdp_get("/json/version"):
            print(" ready!")
            return True
        time.sleep(0.5)
        print(".", end="", flush=True)
    print(" timeout.")
    return False


# ── CDP cookie capture ────────────────────────────────────────────────────────

def get_irctc_tab() -> dict | None:
    """Find the IRCTC tab (or any page tab) in CDP."""
    tabs = _cdp_get("/json") or []
    # Prefer tab already on irctc.co.in
    for tab in tabs:
        if "irctc" in tab.get("url", "").lower() and tab.get("type") == "page":
            return tab
    # Fall back to first page tab
    for tab in tabs:
        if tab.get("type") == "page":
            return tab
    return None


def wait_for_irctc_tab(timeout: int = 30) -> dict | None:
    """Wait until the IRCTC page is loaded in the debug Chrome."""
    print(f"  Waiting up to {timeout}s for IRCTC page to load...", end="", flush=True)
    for _ in range(timeout * 2):
        tab = get_irctc_tab()
        if tab and "irctc" in tab.get("url", "").lower():
            print(" loaded!")
            return tab
        time.sleep(0.5)
        print(".", end="", flush=True)
    print()
    return get_irctc_tab()  # return whatever we have


def get_cookies_via_cdp(ws_url: str) -> list:
    """Fetch all cookies via CDP WebSocket."""
    try:
        import websocket
    except ImportError:
        print("  Installing websocket-client...")
        subprocess.run([sys.executable, "-m", "pip", "install", "websocket-client", "-q"],
                       check=True)
        import websocket

    cookies: list = []
    done = threading.Event()

    def on_message(ws, message):
        data = json.loads(message)
        if data.get("id") == 1:
            cookies.extend(data.get("result", {}).get("cookies", []))
            ws.close()
            done.set()

    def on_error(ws, error):
        print(f"  WebSocket error: {error}")
        done.set()

    ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
    done.wait(timeout=10)
    return cookies


def filter_irctc_cookies(cookies: list) -> str:
    """Keep only IRCTC-domain cookies, format as Cookie header string."""
    relevant = [
        f"{c['name']}={c['value']}"
        for c in cookies
        if "irctc.co.in" in c.get("domain", "")
    ]
    return "; ".join(relevant)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("IRCTC Cookie Grabber")
    print("=" * 52)

    already_running = check_chrome_debug_port()

    if already_running:
        print("  Debug Chrome already running on port 9222.")
    else:
        print("  Debug Chrome not found on port 9222.")
        print("  Launching fresh Chrome with debugging enabled...")
        if not launch_debug_chrome():
            sys.exit(1)
        if not wait_for_port(secs=15):
            print("\n  Chrome did not start in time. Try again.")
            sys.exit(1)

    # Wait for the IRCTC page to be loaded
    tab = wait_for_irctc_tab(timeout=30)
    if not tab:
        print("\n  No tabs found in debug Chrome.")
        sys.exit(1)

    url = tab.get("url", "")
    print(f"\n  Active tab: {url}")

    # Always wait 7s for Akamai to finish setting cookies after page load
    print("  Waiting 7s for page to fully settle (Akamai cookies)...", end="", flush=True)
    for _ in range(14):
        time.sleep(0.5)
        print(".", end="", flush=True)
    print(" done")

    if "irctc" not in url.lower():
        print("  IRCTC page not loaded yet.")
        print(f"  Navigating to {IRCTC_URL}...")
        # Navigate via CDP
        tabs = _cdp_get("/json") or []
        target_id = tab.get("id")
        # Use navigate via WebSocket to the irctc page
        navigate_done = threading.Event()

        def nav_msg(ws, msg):
            navigate_done.set()
            ws.close()

        try:
            import websocket
            ws_url = tab.get("webSocketDebuggerUrl", "")
            nav_ws = websocket.WebSocketApp(ws_url, on_message=nav_msg)
            t = threading.Thread(target=nav_ws.run_forever, daemon=True)
            t.start()
            time.sleep(0.3)
            nav_ws.send(json.dumps({
                "id": 99, "method": "Page.navigate",
                "params": {"url": IRCTC_URL}
            }))
            navigate_done.wait(timeout=5)
        except Exception:
            pass

        print("  Waiting 8s for IRCTC page to load fully...")
        time.sleep(8)
        tab = get_irctc_tab()

    ws_url = (tab or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        print("  ERROR: No WebSocket URL for tab.")
        sys.exit(1)

    print("  Capturing cookies via CDP...")
    cookies = get_cookies_via_cdp(ws_url)

    if not cookies:
        print("  No cookies found. IRCTC page may not be fully loaded.")
        sys.exit(1)

    print(f"  Total cookies captured: {len(cookies)}")
    irctc_cookie_str = filter_irctc_cookies(cookies)

    if not irctc_cookie_str:
        print("  No IRCTC-specific cookies found.")
        all_domains = sorted({c.get("domain", "") for c in cookies})
        print(f"  Domains seen: {all_domains}")
        sys.exit(1)

    count = irctc_cookie_str.count(";") + 1
    print(f"  Extracted {count} IRCTC cookies\n")

    # ── Output ──
    print("=" * 52)
    print("  STEP 1 — Run in your SERVER terminal (PowerShell):\n")
    print(f'  $env:IRCTC_COOKIE="{irctc_cookie_str}"')
    print()
    print("  STEP 2 — Restart uvicorn:\n")
    print("  uvicorn src.http_server:app --host 0.0.0.0 --port 8000 --reload")
    print("=" * 52)

    # Save to file so user can just dot-source it
    env_file = "irctc_cookie.env"
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f'$env:IRCTC_COOKIE="{irctc_cookie_str}"\n')
    print(f"\n  Also saved to {env_file}")
    print("  Shortcut: run  .  .\\irctc_cookie.env  then restart uvicorn")


if __name__ == "__main__":
    main()
