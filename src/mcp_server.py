"""
mcp_server.py

STDIO MCP server for Claude Desktop.
Drop the path into ~/.claude/claude_desktop_config.json (see mcp_config.json).

MCP tool exposed:
  check_irctc_vacancy(train_no, journey_date, boarded_from, travel_to, cls)
"""

import asyncio
import json
import sys
from irctc_scraper import find_vacant_berths

# ---------------------------------------------------------------------------
# Minimal STDIO MCP implementation (no external MCP SDK dependency)
# Follows the MCP JSON-RPC 2.0 protocol over stdin/stdout
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "check_irctc_vacancy",
        "description": (
            "Check for vacant/unoccupied berths on an Indian Railways train for a given journey segment. "
            "Returns a pre-filtered list of coaches and berths that are free for the user's travel segment. "
            "Use this when the user mentions being on a train and asks about empty seats or berths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "train_no": {
                    "type": "string",
                    "description": "Train number, e.g. '12302'",
                },
                "journey_date": {
                    "type": "string",
                    "description": "Journey date in YYYY-MM-DD format",
                },
                "boarded_from": {
                    "type": "string",
                    "description": "Station code where the user boarded, e.g. 'NDLS'",
                },
                "travel_to": {
                    "type": "string",
                    "description": "Station code of user's destination, e.g. 'CNB'",
                },
                "cls": {
                    "type": "string",
                    "description": "Coach class filter: SL, 3A, 2A, 1A, CC, EC, 2S. Omit for all classes.",
                },
            },
            "required": ["train_no", "journey_date", "boarded_from", "travel_to"],
        },
    }
]


async def handle_request(request: dict) -> dict:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "irctc-vacancy", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "check_irctc_vacancy":
            try:
                result_text = await find_vacant_berths(
                    train_no=args["train_no"],
                    journey_date=args["journey_date"],
                    boarded_from=args["boarded_from"],
                    travel_to=args["travel_to"],
                    cls=args.get("cls"),
                )
            except Exception as e:
                result_text = f"Error: {e}"

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }

    # notifications (no id) — no response needed
    if req_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def main():
    """Read JSON-RPC requests from stdin, write responses to stdout."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    w_transport, w_protocol = await loop.connect_write_pipe(
        asyncio.BaseProtocol, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)

    while True:
        try:
            line = await reader.readline()
            if not line:
                break
            request = json.loads(line.decode())
            response = await handle_request(request)
            if response is not None:
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except json.JSONDecodeError:
            continue
        except Exception as e:
            sys.stderr.write(f"MCP error: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
