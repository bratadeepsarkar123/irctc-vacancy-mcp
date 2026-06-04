"""
mcp_server.py - IRCTC Vacancy MCP Server (Phase 2A-2 - Smart Position)
=============================================================
Exposes SSE MCP transport. Works with ngrok tunnel for permanent URL.

Tools exposed:
  check_irctc_vacancy  - Smart vacancy checker (auto-detects charting station)
  get_train_route      - Get full ordered station list for a train
  get_train_position   - Get current train position + schedule + next station
  set_irctc_cookie     - Push a fresh IRCTC session cookie at runtime
"""

import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _load_cookie_from_file() -> bool:
    """Auto-load IRCTC cookie from irctc_cookie.env on startup."""
    env_file = Path(__file__).parent.parent / "irctc_cookie.env"
    if not env_file.exists():
        return False
    try:
        raw = env_file.read_text(encoding="utf-8")
        m = re.search(r'\$env:IRCTC_COOKIE="(.+)"', raw)
        if m:
            os.environ["IRCTC_COOKIE"] = m.group(1)
            logger.info("Auto-loaded IRCTC cookie from %s (%d chars)", env_file, len(m.group(1)))
            return True
    except Exception as e:
        logger.warning("Could not load cookie from file: %s", e)
    return False


# Load cookie immediately at startup (before any request comes in)
_load_cookie_from_file()


@asynccontextmanager
async def lifespan(server: FastMCP):
    """No-op lifespan — cookie already loaded at module level."""
    yield


# -- FastMCP server --

mcp = FastMCP(
    "irctc-vacancy",
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# -- Tool: check_irctc_vacancy (SMART - Phase 2A) --

@mcp.tool()
async def check_irctc_vacancy(
    train_no: str,
    journey_date: str,
    boarding_station: str,
    destination_station: str,
    cls: str = None,
) -> str:
    """
    Find vacant berths on an Indian Railways train for a specific segment.

    Instructions for AI Assistant:
    - If the user says "I just boarded" or asks for "free berths to next stop":
      1. First, call `get_train_position(train_no)` to find the current/next stations.
      2. Then, call this `check_irctc_vacancy` tool using those station codes and today's date.
    - If the user gives city names (e.g., Kanpur), use `get_train_route` first to find the exact station code (e.g., CNB), then use this tool. Please do this automatically without asking the user for confirmation.
    - Dates: "today" = current date, "tomorrow" = next day. Always use the departure date of the train.

    Args:
        train_no:            Train number, e.g. "15708" or "12184"
        journey_date:        Journey date in YYYY-MM-DD format (use departure date)
        boarding_station:    Station CODE, e.g. "DLI", "AME", "CNB", "MBDP"
        destination_station: Station CODE, e.g. "CNB", "LKO", "BPL"
        cls:                 Optional class: SL, 3A, 2A, 1A, CC, EC, 2S, 3E
    """
    from main_tool import find_vacant_berths
    from datetime import date as _date, timedelta as _td

    # Resolve natural language dates
    jdate = journey_date.strip().lower()
    _today = _date.today().strftime("%Y-%m-%d")
    _tomorrow = (_date.today() + _td(days=1)).strftime("%Y-%m-%d")
    if jdate in ("today", "aaj", "abhi", "now"):
        jdate = _today
    elif jdate in ("tomorrow", "kal", "aane wala kal"):
        jdate = _tomorrow
    else:
        jdate = journey_date.strip()
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%m-%d-%Y", "%Y/%m/%d"):
            try:
                from datetime import datetime as _dt
                jdate = _dt.strptime(jdate, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    return await find_vacant_berths(
        train_no=train_no.strip(),
        jdate=jdate,
        boarded_from=boarding_station.strip().upper(),
        travel_to=destination_station.strip().upper(),
        class_filter=cls,
    )


# -- Tool: get_train_route --

@mcp.tool()
async def get_train_route(train_no: str) -> str:
    """
    Get the full ordered list of stations for a train, with names and codes.

    Use this tool BEFORE check_irctc_vacancy when:
    - The user gives station NAMES instead of codes (e.g. "Amethi" not "AME")
    - You need to find the correct station code for a city name
    - The user says "I'm on train X" without specifying exact stations
    - You want to verify which stations are on a train's route

    Returns a numbered list of station codes and names in route order.

    Args:
        train_no: Train number, e.g. "15708" or "12184"
    """
    from train_schedule import _fetch_confirmtkt_schedule
    from irctc_api import _fetch_schedule_async

    # Primary: confirmtkt (has station names)
    schedule = _fetch_confirmtkt_schedule(train_no.strip())
    if schedule:
        lines = [f"Train {train_no} route ({len(schedule)} stations):"]
        for i, stn in enumerate(schedule, 1):
            lines.append(f"  {i:3d}. {stn['code']:<6s}  {stn['name']}")
        return "\n".join(lines)

    # Fallback: IRCTC schedule (codes only)
    stations = await _fetch_schedule_async(train_no.strip())
    if stations:
        lines = [f"Train {train_no} route ({len(stations)} stations):"]
        for i, stn in enumerate(stations, 1):
            lines.append(f"  {i:3d}. {stn}")
        return "\n".join(lines)

    return f"Could not fetch route for train {train_no}. The schedule API may be temporarily unavailable."


# -- Tool: get_train_position (Phase 2A-2) --

@mcp.tool()
async def get_train_position(train_no: str) -> str:
    """
    Get current train position, schedule, and next station.

    CRITICAL: Call this tool FIRST when the user says any of:
    - "I just boarded train X"
    - "I'm on train X, any free berths?"
    - "Free berths on train X to next stop?"
    - "Where is train X now?"
    - Any query where the user is ON the train and doesn't specify stations

    This tool returns:
    - Full train schedule with station codes and names
    - Estimated current position based on IST time
    - The current station and next station codes
    - Journey progress percentage

    After calling this, use the returned current_station and next_station
    as inputs to check_irctc_vacancy.

    Args:
        train_no: Train number, e.g. "12184" or "15708"
    """
    from irctc_api import _fetch_schedule_async
    from train_schedule import get_full_schedule

    # Get station list from IRCTC
    stations = await _fetch_schedule_async(train_no.strip())

    # Build full schedule with position estimate
    return get_full_schedule(train_no.strip(), stations)


# -- Tool: set_irctc_cookie --

@mcp.tool()
async def set_irctc_cookie(cookie: str) -> str:
    """
    Push a fresh IRCTC session cookie to the running server.

    Call this when the server reports 403 or expired session.
    Get a fresh cookie from browser DevTools > Application > Cookies > www.irctc.co.in.

    Args:
        cookie: The full Cookie header string from your browser.
    """
    if not cookie or not cookie.strip():
        return "Error: cookie must not be empty."

    os.environ["IRCTC_COOKIE"] = cookie.strip()
    return f"Cookie updated in server ({len(cookie)} chars). Active for all future requests."


# -- Entrypoint --

if __name__ == "__main__":
    import json
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, HTMLResponse, FileResponse
    from starlette.routing import Route, Mount
    from starlette.staticfiles import StaticFiles

    async def http_set_cookie(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        cookie = body.get("cookie", "").strip()
        if not cookie:
            return JSONResponse({"error": "cookie must not be empty"}, status_code=422)
        os.environ["IRCTC_COOKIE"] = cookie
        return JSONResponse({"status": "ok", "cookie_length": len(cookie)})

    async def http_health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "healthy",
            "cookie_set": bool(os.environ.get("IRCTC_COOKIE")),
            "tools": ["check_irctc_vacancy", "get_train_route", "get_train_position", "set_irctc_cookie"],
        })

    # REST wrapper: POST /api/tool  { tool: "get_train_route", args: { train_no: "12184" } }
    # This lets the web chat UI call any MCP tool directly via HTTP.
    async def http_tool_call(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        tool_name = body.get("tool", "")
        args = body.get("args", {})

        # Map tool names to actual functions
        tool_map = {
            "check_irctc_vacancy": check_irctc_vacancy,
            "get_train_route": get_train_route,
            "get_train_position": get_train_position,
            "set_irctc_cookie": set_irctc_cookie,
        }

        fn = tool_map.get(tool_name)
        if not fn:
            return JSONResponse(
                {"error": f"Unknown tool: {tool_name}. Available: {list(tool_map.keys())}"},
                status_code=404,
            )

        try:
            result = await fn(**args)
            return JSONResponse({"result": result})
        except TypeError as e:
            return JSONResponse({"error": f"Invalid arguments for {tool_name}: {e}"}, status_code=422)
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return JSONResponse({"error": f"Tool execution failed: {e}"}, status_code=500)

    # Serve chat UI at root
    static_dir = Path(__file__).parent / "static"
    async def http_chat_ui(request: Request) -> FileResponse:
        return FileResponse(str(static_dir / "index.html"))

    mcp_app = mcp.sse_app()
    combined = Starlette(routes=[
        Route("/set-cookie", http_set_cookie, methods=["POST"]),
        Route("/health", http_health, methods=["GET"]),
        Route("/api/tool", http_tool_call, methods=["POST"]),
        Route("/chat", http_chat_ui, methods=["GET"]),
        Mount("/static", StaticFiles(directory=str(static_dir)), name="static"),
        Mount("/", mcp_app),
    ])

    port = int(os.environ.get("PORT", 8001))
    logger.info("Starting IRCTC Vacancy MCP server on port %d ...", port)
    logger.info("Chat UI: http://localhost:%d/chat", port)
    uvicorn.run(combined, host="0.0.0.0", port=port, log_level="info")