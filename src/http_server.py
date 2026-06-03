"""
http_server.py — FastAPI HTTP MCP connector
============================================
Perplexity custom connector + any HTTP-based AI frontend.

Run:
  cd irctc-vacancy-mcp
  uvicorn src.http_server:app --host 0.0.0.0 --port 8000 --reload

For public HTTPS (Perplexity connector):
  cloudflared tunnel --url http://localhost:8000
  → paste the generated https://xxx.trycloudflare.com URL into Perplexity

Endpoints:
  GET  /          → health check
  GET  /health    → detailed health
  GET  /tools     → Perplexity tool manifest
  POST /run       → execute check_irctc_vacancy tool
  GET  /openapi   → OpenAPI 3.1 spec (alternative connector format)
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from main_tool import find_vacant_berths

app = FastAPI(
    title="IRCTC Vacancy MCP",
    description="Check for vacant berths on Indian Railways trains by journey segment.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Tool manifest (Perplexity connector format)
# ---------------------------------------------------------------------------

TOOL_MANIFEST = {
    "schema_version": "v1",
    "name": "irctc_vacancy",
    "description": (
        "Checks for vacant/unoccupied berths on an Indian Railways train for a given "
        "journey segment. Call this when the user is on a train and asks about empty "
        "seats or berths they could move to."
    ),
    "tools": [
        {
            "name": "check_irctc_vacancy",
            "description": (
                "Returns pre-filtered vacant berths for the user's travel segment. "
                "Only berths with a vacant window that fully covers the user's journey "
                "(boarded_from → travel_to) are returned. "
                "The AI should NOT analyse raw data — all filtering is done in code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "train_no": {
                        "type": "string",
                        "description": "Train number as string, e.g. '12302'",
                    },
                    "journey_date": {
                        "type": "string",
                        "description": "Date of journey in YYYY-MM-DD or DD-MM-YYYY",
                    },
                    "boarded_from": {
                        "type": "string",
                        "description": "Station code where the user boarded, e.g. 'NDLS'",
                    },
                    "travel_to": {
                        "type": "string",
                        "description": "Destination station code, e.g. 'CNB'",
                    },
                    "cls": {
                        "type": "string",
                        "description": "Optional coach class: SL, 3A, 2A, 1A, CC, EC, 2S",
                    },
                },
                "required": ["train_no", "journey_date", "boarded_from", "travel_to"],
            },
        }
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_date(raw: str) -> str:
    """Accept DD-MM-YYYY or YYYY-MM-DD; always return YYYY-MM-DD."""
    raw = raw.strip()
    if len(raw) == 10 and raw[2] == "-" and raw[5] == "-":
        try:
            return datetime.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok", "service": "irctc-vacancy-mcp", "version": "2.0.0"}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "irctc_cookie_set": bool(os.environ.get("IRCTC_COOKIE")),
        "version": "2.0.0",
    }


@app.get("/tools")
async def list_tools():
    return JSONResponse(content=TOOL_MANIFEST)


class RunRequest(BaseModel):
    tool: str
    parameters: dict


@app.post("/run")
async def run_tool(req: RunRequest):
    if req.tool != "check_irctc_vacancy":
        raise HTTPException(status_code=404, detail=f"Unknown tool: {req.tool!r}")

    p = req.parameters
    required = ["train_no", "journey_date", "boarded_from", "travel_to"]
    missing = [k for k in required if k not in p]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing parameters: {missing}")

    jdate = _normalise_date(str(p["journey_date"]))

    try:
        result = await find_vacant_berths(
            train_no=str(p["train_no"]),
            jdate=jdate,
            boarded_from=str(p["boarded_from"]),
            travel_to=str(p["travel_to"]),
            class_filter=p.get("cls"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"result": result}


@app.get("/openapi")
async def openapi_spec():
    """OpenAPI 3.1 spec — alternative Perplexity connector format."""
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "IRCTC Vacancy MCP",
            "version": "2.0.0",
            "description": "Check vacant berths on Indian Railways trains for a travel segment.",
        },
        "paths": {
            "/run": {
                "post": {
                    "operationId": "check_irctc_vacancy",
                    "summary": "Find vacant berths for a journey segment",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["tool", "parameters"],
                                    "properties": {
                                        "tool": {
                                            "type": "string",
                                            "enum": ["check_irctc_vacancy"],
                                        },
                                        "parameters": {
                                            "type": "object",
                                            "required": [
                                                "train_no", "journey_date",
                                                "boarded_from", "travel_to",
                                            ],
                                            "properties": {
                                                "train_no": {"type": "string"},
                                                "journey_date": {
                                                    "type": "string",
                                                    "format": "date",
                                                },
                                                "boarded_from": {"type": "string"},
                                                "travel_to": {"type": "string"},
                                                "cls": {"type": "string"},
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Filtered vacant berth list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "result": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    return JSONResponse(content=spec)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
