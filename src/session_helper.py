"""
session_helper.py

Uses Playwright to open the IRCTC chart page in a headless browser,
let it load (and set its own cookies), then captures the session cookies.

Run once:
  python src/session_helper.py

Outputs:
  export IRCTC_COOKIE="..."

Then set that env variable before starting the MCP server.
Cookies typically expire in ~30 minutes; re-run as needed.
"""

import asyncio
import sys


async def get_irctc_cookies() -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Install playwright: pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Mobile Safari/537.36"
            )
        )
        page = await context.new_page()

        print("Opening IRCTC chart page...", file=sys.stderr)
        await page.goto(
            "https://www.irctc.co.in/online-charts/",
            wait_until="networkidle",
            timeout=30_000,
        )

        cookies = await context.cookies()
        await browser.close()

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return cookie_str


async def main():
    cookie_str = await get_irctc_cookies()
    if cookie_str:
        print(f'export IRCTC_COOKIE="{cookie_str}"')
        print(f"\n# Found {cookie_str.count(';') + 1} cookies", file=sys.stderr)
    else:
        print("No cookies captured.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
