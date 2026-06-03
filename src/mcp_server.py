"""
mcp_server.py — STDIO MCP Server
=================================
Compatible with Claude Desktop, Cursor, Windsurf, and any MCP-compliant IDE.

Setup in Claude Desktop (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "irctc-vacancy": {
        "command": "python",
        "args": ["/path/to/irctc-vacancy-mcp/src/mcp_server.py"]
      }
    }
  }

Setup in Cursor (cursor settings → MCP):
  {
    "irctc-vacancy": {
      "command": "python",
      "args": ["/path/to/irctc-vacancy-mcp/src/mcp_server.py"]
    }
  }

Two tools exposed:
  find_vacant_berths       — main tool (segment-based vacancy search)
  get_train_composition    — helper (coach list + meta)
"""

import asyncio
import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent))

from main_tool import find_vacant_berths
from irctc_api import train_composition


# ---------------------------------------------------------------------------
# MCP protocol helpers (JSON-RPC 2.0)
# ---------------------------------------------------------------------------

def _ok(id_: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result})


def _err(id_: Any, code: int, message: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": message},
    })


def _tool_result(id_: Any, text: str, is_error: bool = False) -> str:
    return _ok(id_, {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    })


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "find_vacant_berths",
        "description": (
            "Check IRCTC reservation chart for vacant berths on a running or departing "
            "Indian Railways train. Returns a pre-filtered, coach-wise list of free berths "
            "with the exact station range they are free for. "
            "Use when the user says they are on a train and wants to find empty seats "
            "or berths to upgrade to. All data processing is done in code — never ask "
            "the AI to analyse raw chart data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "train_number": {
                    "type": "string",
                    "description": "5-digit train number, e.g. '12302' or '12424'",
                },
                "journey_date": {
                    "type": "string",
                    "description": (
                        "Date of travel in YYYY-MM-DD format, e.g. '2026-06-04'. "
                        "DD-MM-YYYY is also accepted."
                    ),
                },
                "boarded_from": {
                    "type": "string",
                    "description": "Station code where the user boarded, e.g. 'NDLS', 'CNB', 'HWH'",
                },
                "travel_to": {
                    "type": "string",
                    "description": "Destination station code, e.g. 'CNB', 'PNBE', 'DBRT'",
                },
                "travel_class": {
                    "type": "string",
                    "description": (
                        "Optional. Coach class to filter by: SL (Sleeper), 3A (AC 3-Tier), "
                        "2A (AC 2-Tier), 1A (First AC), CC (Chair Car), EC (Exec Chair). "
                        "If omitted, all classes are scanned."
                    ),
                },
            },
            "required": ["train_number", "journey_date", "boarded_from", "travel_to"],
        },
    },
    {
        "name": "get_train_composition",
        "description": (
            "Returns the coach layout of a train — list of coaches with their class codes "
            "and positions from the engine. Use when the user asks about the train layout, "
            "which coach is at which position, or wants to know available classes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "train_number": {
                    "type": "string",
                    "description": "5-digit train number",
                },
                "journey_date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format",
                },
                "boarding_station": {
                    "type": "string",
                    "description": "Station code for boarding (required by IRCTC API)",
                },
            },
            "required": ["train_number", "journey_date", "boarding_station"],
        },
    },
]


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

def _normalise_date(raw: str) -> str:
    raw = raw.strip()
    if len(raw) == 10 and raw[2] == "-" and raw[5] == "-":
        try:
            return datetime.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

async def handle_request(req: dict) -> str:
    method = req.get("method", "")
    id_ = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "irctc-vacancy", "version": "2.0.0"},
        })

    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        try:
            if tool_name == "find_vacant_berths":
                jdate = _normalise_date(str(args.get("journey_date", "")))
                result = await find_vacant_berths(
                    train_no=str(args["train_number"]),
                    jdate=jdate,
                    boarded_from=str(args["boarded_from"]),
                    travel_to=str(args["travel_to"]),
                    class_filter=args.get("travel_class"),
                )
                return _tool_result(id_, result)

            elif tool_name == "get_train_composition":
                jdate = _normalise_date(str(args.get("journey_date", "")))
                data = train_composition(
                    train_no=str(args["train_number"]),
                    jdate=jdate,
                    boarding=str(args["boarding_station"]),
                )
                coaches = data.get("cdd", [])
                if not coaches:
                    return _tool_result(
                        id_,
                        f"No composition data for train {args['train_number']}. "
                        "Chart may not be prepared yet.",
                        is_error=False,
                    )
                lines = [
                    f"Train {data.get('trainNo', args['train_number'])} "
                    f"({data.get('trainName', '')}) | {len(coaches)} coaches",
                    f"Route: {data.get('from', '?')} → {data.get('to', '?')}",
                    "",
                ]
                for c in coaches:
                    pos = c.get("positionFromEngine")
                    pos_str = f" pos {pos}" if pos is not None else ""
                    vacant = c.get("vacantBerths", "?")
                    lines.append(
                        f"  {c.get('coachName','?'):5s} "
                        f"({c.get('classCode','?'):3s})"
                        f"{pos_str}  — {vacant} vacant berths"
                    )
                return _tool_result(id_, "\n".join(lines))

            else:
                return _err(id_, -32601, f"Unknown tool: {tool_name!r}")

        except KeyError as e:
            return _err(id_, -32602, f"Missing required parameter: {e}")
        except Exception as e:
            return _tool_result(id_, f"Error: {e}", is_error=True)

    if method == "notifications/initialized":
        return ""  # no response needed for notifications

    return _err(id_, -32601, f"Method not supported: {method!r}")


# ---------------------------------------------------------------------------
# STDIO transport loop
# ---------------------------------------------------------------------------

async def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(_err(None, -32700, "Parse error"), flush=True)
            continue

        response = await handle_request(req)
        if response:  # skip empty (notifications)
            print(response, flush=True)


if __name__ == "__main__":
    asyncio.run(main())