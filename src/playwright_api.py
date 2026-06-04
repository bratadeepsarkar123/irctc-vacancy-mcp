"""
playwright_api.py — Cloud-native IRCTC API client using Playwright headless Chromium
======================================================================================
This module replaces browser_api.py for cloud deployments.

Instead of connecting to a LOCAL Chrome via CDP on port 9222,
it manages a persistent Playwright browser context INSIDE the container.

The browser is started once at server startup (via start_browser() called from
the FastMCP lifespan hook) and kept alive for the container's lifetime.
On each API call, we run fetch() via page.evaluate() — same technique as CDP,
but the browser lives in the same process.

Cookie Management:
  The IRCTC_COOKIE environment variable is injected into the browser context
  before the first request. The /set-cookie endpoint in the MCP server updates
  this env var at runtime and refreshes the browser cookies.
"""

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Module-level browser state ────────────────────────────────────────────────
_playwright = None
_browser = None
_context = None
_page = None
_lock = asyncio.Lock()

IRCTC_CHARTS_URL = "https://www.irctc.co.in/online-charts/"


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def start_browser() -> None:
    """
    Launch Playwright Chromium and navigate to the IRCTC charts page.
    Call once at server startup.
    """
    global _playwright, _browser, _context, _page

    from playwright.async_api import async_playwright

    logger.info("Starting Playwright Chromium...")
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",    # Cloud Run uses /tmp for shm
            "--disable-gpu",
            "--single-process",           # Reduces memory in constrained envs
        ],
    )

    _context = await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    # Inject IRCTC cookies if provided via env var
    await _inject_cookies_from_env()

    _page = await _context.new_page()

    logger.info("Navigating to IRCTC charts page...")
    try:
        await _page.goto(
            IRCTC_CHARTS_URL,
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        logger.info("IRCTC charts page loaded.")
    except Exception as e:
        logger.warning("Initial page load failed (non-fatal): %s", e)
        # Still usable — fetch() calls will work even if the page timed out
        # as long as we're on the same origin

    logger.info("Playwright browser ready.")


async def stop_browser() -> None:
    """Gracefully shut down Playwright. Call at server shutdown."""
    global _playwright, _browser, _context, _page
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()
    _playwright = _browser = _context = _page = None
    logger.info("Playwright browser stopped.")


async def _inject_cookies_from_env() -> None:
    """Parse IRCTC_COOKIE env var and add cookies to the browser context."""
    cookie_str = os.environ.get("IRCTC_COOKIE", "").strip()
    if not cookie_str:
        logger.info("No IRCTC_COOKIE env var set — running without session cookie.")
        return

    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": "www.irctc.co.in",
                "path": "/",
                "secure": True,
            })

    if cookies and _context:
        await _context.add_cookies(cookies)
        logger.info("Injected %d cookies from IRCTC_COOKIE env var.", len(cookies))


async def refresh_cookie(cookie_str: str) -> None:
    """
    Replace the browser's IRCTC session cookies with a fresh set.
    Called by the /set-cookie MCP endpoint when the user pushes a new cookie.
    """
    global _context
    if not _context:
        raise RuntimeError("Browser not started. Call start_browser() first.")

    # Update env var so it persists across page reloads in this process
    os.environ["IRCTC_COOKIE"] = cookie_str

    # Clear old IRCTC cookies from context
    await _context.clear_cookies()
    await _inject_cookies_from_env()
    logger.info("Browser cookies refreshed.")


# ── Public API ────────────────────────────────────────────────────────────────

def is_browser_available() -> bool:
    """True if the Playwright browser is running and ready."""
    return _page is not None


async def browser_post_async(endpoint: str, body: dict) -> dict:
    """
    POST to https://www.irctc.co.in/online-charts/api/<endpoint>
    from INSIDE the Playwright browser via page.evaluate().

    Returns parsed JSON dict. Raises RuntimeError on failure.
    """
    if not _page:
        raise RuntimeError("Playwright browser not started.")

    # Serialise the body to a JSON string that JS can JSON.parse
    body_json = json.dumps(json.dumps(body))  # double-encode for JS literal

    js = f"""
async () => {{
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
}}
"""

    async with _lock:
        try:
            raw = await _page.evaluate(js)
        except Exception as e:
            # If the page got unloaded/crashed, try to reload and retry once
            logger.warning("page.evaluate failed (%s), reloading IRCTC page...", e)
            await _page.goto(IRCTC_CHARTS_URL, wait_until="domcontentloaded", timeout=30_000)
            raw = await _page.evaluate(js)

    if raw is None:
        raise RuntimeError(f"No response from browser for {endpoint}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {endpoint}: {e}\nRaw: {raw[:200]}")

    if "__error__" in data:
        raise RuntimeError(f"Browser fetch error for {endpoint}: {data['__error__']}")

    return data


def browser_post(endpoint: str, body: dict) -> dict:
    """
    Sync wrapper around browser_post_async() for use from sync contexts
    (e.g. called from a ThreadPoolExecutor thread inside uvicorn).

    When the main asyncio event loop is already running (always the case
    inside uvicorn), loop.run_until_complete() raises 'this event loop is
    already running'. We use run_coroutine_threadsafe() instead, which
    schedules the coroutine on the running loop and blocks the calling
    thread until it completes.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Called from a worker thread while uvicorn's loop is running
        future = asyncio.run_coroutine_threadsafe(browser_post_async(endpoint, body), loop)
        return future.result(timeout=60)
    elif loop:
        return loop.run_until_complete(browser_post_async(endpoint, body))
    else:
        return asyncio.run(browser_post_async(endpoint, body))
