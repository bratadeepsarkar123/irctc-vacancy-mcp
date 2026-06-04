"""
browser_api.py — Execute IRCTC API calls from within the debug Chrome
======================================================================
Akamai Bot Manager ties the _abck cookie cryptographically to the browser's
TLS/canvas/JS fingerprint. Replaying cookies from Python is always detected.

Solution: keep a Chrome window open with --remote-debugging-port=9222
and use CDP Runtime.evaluate to run fetch() calls from INSIDE the browser.
The browser handles all Akamai fingerprinting natively.

The debug Chrome must be open on https://www.irctc.co.in/online-charts/
for this to work (run grab_cookie.py once to open it).
"""

import json
import socket
import threading
import time
import urllib.request
from typing import Optional

CDP_PORT = 9222
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
_TIMEOUT = 20  # seconds per API call


# ── CDP helpers ───────────────────────────────────────────────────────────────

def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1):
            return True
    except OSError:
        return False


def _cdp_get(path: str) -> Optional[dict | list]:
    try:
        req = urllib.request.Request(
            f"{CDP_BASE}{path}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _find_irctc_ws_url() -> Optional[str]:
    """Return WebSocket debugger URL for the IRCTC tab, or None."""
    if not _port_open():
        return None
    tabs = _cdp_get("/json") or []
    if isinstance(tabs, list):
        for tab in tabs:
            if "irctc" in tab.get("url", "").lower() and tab.get("type") == "page":
                return tab.get("webSocketDebuggerUrl")
    return None


def _ws_evaluate(ws_url: str, js_expression: str, timeout: int = _TIMEOUT) -> Optional[str]:
    """
    Run js_expression in the browser via Runtime.evaluate (awaitPromise=True).
    Returns the string value of the expression result, or None on failure.
    """
    try:
        import websocket
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "websocket-client", "-q"])
        import websocket

    result_holder: dict = {}
    done = threading.Event()
    cmd_id = 9001

    def on_message(ws, raw):
        data = json.loads(raw)
        if data.get("id") == cmd_id:
            res = data.get("result", {})
            exc = res.get("exceptionDetails")
            if exc:
                result_holder["error"] = str(exc)
            else:
                val = res.get("result", {})
                result_holder["value"] = val.get("value")
            ws.close()
            done.set()

    def on_error(ws, err):
        result_holder["error"] = str(err)
        done.set()

    ws_app = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error)
    t = threading.Thread(target=ws_app.run_forever, daemon=True)
    t.start()
    time.sleep(0.3)  # give websocket time to connect

    ws_app.send(json.dumps({
        "id": cmd_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": js_expression,
            "awaitPromise": True,
            "returnByValue": True,
        },
    }))

    done.wait(timeout=timeout)

    if "error" in result_holder:
        raise RuntimeError(f"CDP eval error: {result_holder['error']}")
    return result_holder.get("value")


# ── Public API ────────────────────────────────────────────────────────────────

def is_browser_available() -> bool:
    """True if debug Chrome is running with an IRCTC tab open."""
    return bool(_find_irctc_ws_url())


def browser_post(endpoint: str, body: dict) -> dict:
    """
    POST to https://www.irctc.co.in/online-charts/api/<endpoint>
    from INSIDE the debug Chrome browser via CDP Runtime.evaluate.

    Returns parsed JSON dict. Raises RuntimeError on failure.
    """
    ws_url = _find_irctc_ws_url()
    if not ws_url:
        raise RuntimeError(
            "Debug Chrome not found on port 9222. "
            "Run grab_cookie.py to open Chrome with debugging enabled."
        )

    # Build a self-executing async arrow function that does the fetch
    # and returns the response text. We use JSON.stringify so that
    # Runtime.evaluate can return it as a plain string value.
    body_json = json.dumps(json.dumps(body))  # double-encoded: JS will JSON.parse it
    js = f"""
(async () => {{
    try {{
        const resp = await fetch(
            '/online-charts/api/{endpoint}',
            {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }},
                body: {body_json},
            }}
        );
        const text = await resp.text();
        return text;
    }} catch (e) {{
        return JSON.stringify({{__error__: e.toString()}});
    }}
}})()
"""

    raw = _ws_evaluate(ws_url, js, timeout=_TIMEOUT)
    if raw is None:
        raise RuntimeError(f"No response from browser for {endpoint}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {endpoint}: {e}\nRaw: {raw[:200]}")

    if "__error__" in data:
        raise RuntimeError(f"Browser fetch error for {endpoint}: {data['__error__']}")

    return data


# ── Quick smoke test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("Checking for debug Chrome...")
    if not is_browser_available():
        print("ERROR: No IRCTC tab found on port 9222.")
        print("Run: python grab_cookie.py")
        sys.exit(1)

    print("Browser found. Testing trainComposition...")
    try:
        result = browser_post("trainComposition", {
            "trainNo": "15708", "jDate": "2026-06-03", "boardingStation": "DLI"
        })
        coaches = result.get("cdd") or []
        print(f"OK — {len(coaches)} coaches returned")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("Testing vacantBerth...")
    try:
        result = browser_post("vacantBerth", {
            "trainNo": "15708", "boardingStation": "DLI",
            "remoteStation": "DLI", "trainSourceStation": "ASR",
            "jDate": "2026-06-03", "cls": "2A", "chartType": 2
        })
        vbd = result.get("vbd") or []
        print(f"OK — {len(vbd)} vacant entries, error={result.get('error')!r}")
        if vbd:
            print(f"First entry keys: {list(vbd[0].keys())}")
            print(f"First entry: {json.dumps(vbd[0], ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"ERROR: {e}")
