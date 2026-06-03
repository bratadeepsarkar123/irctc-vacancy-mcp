"""
http_server.py

FastAPI HTTP MCP connector for Perplexity (and any HTTP-based AI frontend).

Run:
  uvicorn src.http_server:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
  GET  /          → health check
  GET  /tools     → tool manifest
  POST /run       → execute a tool
  GET  /openapi   → OpenAPI spec for Perplexity connector
"""

import os
import sys
from pathlib import Path

# Allow importing irctc_scraper from the same src/ folder
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import asyncio

from irctc_scraper import find_vacant_berths

app = FastAPI(
    title="IRCTC Vacancy MCP",
    description="Check for vacant berths on Indian Railways trains by segment.",
    version="1.0.0",
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
        "Checks for vacant/unoccupied berths on an Indian Railways train for a given journey segment. "
        "Call this when the user is on a train and asks about empty seats or berths they could move to."
    ),
    "tools": [
        {
            "name": "check_irctc_vacancy",
            "description": (
                "Returns pre-filtered vacant berths for the user's travel segment. "
                "Only berths with a vacant window that fully covers the user's journey "
                "(boarded_from → travel_to) are returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "train_no": {
                        "type": "string",
                        "description": "Train number as a string, e.g. '12302'",
                    },
                    "journey_date": {
                        "type": "string",
                        "description": "Date of journey in YYYY-MM-DD format",
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


@app.get("/")
async def health():
    return {"status": "ok", "service": "irctc-vacancy-mcp"}


@app.get("/tools")
async def list_tools():
    return JSONResponse(content=TOOL_MANIFEST)


# ---------------------------------------------------------------------------
# Tool execution endpoint
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    tool: str
    parameters: dict


@app.post("/run")
async def run_tool(req: RunRequest):
    if req.tool != "check_irctc_vacancy":
        raise HTTPException(status_code=404, detail=f"Unknown tool: {req.tool}")

    p = req.parameters
    required = ["train_no", "journey_date", "boarded_from", "travel_to"]
    missing = [k for k in required if k not in p]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing parameters: {missing}")

    try:
        result = await find_vacant_berths(
            train_no=str(p["train_no"]),
            journey_date=str(p["journey_date"]),
            boarded_from=str(p["boarded_from"]),
            travel_to=str(p["travel_to"]),
            cls=p.get("cls"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"result": result}


# ---------------------------------------------------------------------------
# OpenAPI-based tool spec (alternative Perplexity connector format)
# ---------------------------------------------------------------------------

@app.get("/openapi")
async def openapi_spec():
    """Return OpenAPI 3.1 spec describing the /run endpoint as an AI action."""
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "IRCTC Vacancy MCP",
            "version": "1.0.0",
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
                                    "properties": {
                                        "tool": {"type": "string", "enum": ["check_irctc_vacancy"]},
                                        "parameters": {
                                            "type": "object",
                                            "properties": {
                                                "train_no": {"type": "string"},
                                                "journey_date": {"type": "string", "format": "date"},
                                                "boarded_from": {"type": "string"},
                                                "travel_to": {"type": "string"},
                                                "cls": {"type": "string"},
                                            },
                                            "required": ["train_no", "journey_date", "boarded_from", "travel_to"],
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
                                        "properties": {"result": {"type": "string"}},
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
