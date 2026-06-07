#!/usr/bin/env python
"""Dependency-free ChatGPT/OpenAPI connector for Termux.

This is intentionally stdlib-only because 32-bit Android/Termux cannot build
FastAPI/Pydantic reliably. It exposes the same minimal HTTP surface ChatGPT
needs: /health, /openapi.json, and /run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def normalise_date(raw: str) -> str:
    raw = str(raw).strip()
    if len(raw) == 10 and raw[2] == "-" and raw[5] == "-":
        try:
            return datetime.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def openapi_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "IRCTC Termux Connector",
            "version": "1.0.0",
            "description": (
                "Run IRCTC chart-vacancy tools from the Termux residential worker. "
                "Vacancy output is chart-vacant evidence only, not a booking guarantee."
            ),
        },
        "paths": {
            "/run": {
                "post": {
                    "operationId": "run_irctc_termux_tool",
                    "summary": "Run an IRCTC/NTES tool",
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
                                            "enum": [
                                                "check_irctc_vacancy",
                                                "find_emergency_seats",
                                                "set_irctc_cookie",
                                                "health",
                                            ],
                                        },
                                        "parameters": {
                                            "type": "object",
                                            "properties": {
                                                "train_no": {"type": "string"},
                                                "journey_date": {"type": "string"},
                                                "boarded_from": {"type": "string"},
                                                "travel_to": {"type": "string"},
                                                "cls": {"type": "string"},
                                                "chart_mode": {"type": "string"},
                                                "historical_confirmed": {"type": "boolean"},
                                                "boarding_station": {"type": "string"},
                                                "destination_station": {"type": "string"},
                                                "date": {"type": "string"},
                                                "max_changes": {"type": "integer"},
                                                "cookie": {"type": "string"},
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Tool result",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            }
        },
    }


def run_tool(tool: str, params: dict) -> dict:
    if tool == "health":
        return {"result": health_payload()}

    if tool == "set_irctc_cookie":
        cookie = str(params.get("cookie", "")).replace("\r", "").replace("\n", "").strip()
        if not cookie:
            return {"error": "cookie must not be empty"}
        os.environ["IRCTC_COOKIE"] = cookie
        return {"result": {"status": "ok", "cookie_length": len(cookie)}}

    if tool == "check_irctc_vacancy":
        from main_tool import find_vacant_berths

        required = ["train_no", "journey_date", "boarded_from", "travel_to"]
        missing = [key for key in required if not params.get(key)]
        if missing:
            return {"error": f"Missing parameters: {missing}"}
        result = asyncio.run(find_vacant_berths(
            train_no=str(params["train_no"]),
            jdate=normalise_date(str(params["journey_date"])),
            boarded_from=str(params["boarded_from"]),
            travel_to=str(params["travel_to"]),
            class_filter=params.get("cls"),
            chart_mode=str(params.get("chart_mode", "current")),
            historical_confirmed=bool(params.get("historical_confirmed", False)),
        ))
        return {"result": result}

    if tool == "find_emergency_seats":
        from main_tool import find_emergency_seats

        required = ["boarding_station", "destination_station", "date"]
        missing = [key for key in required if not params.get(key)]
        if missing:
            return {"error": f"Missing parameters: {missing}"}
        result = asyncio.run(find_emergency_seats(
            src=str(params["boarding_station"]),
            dst=str(params["destination_station"]),
            jdate=normalise_date(str(params["date"])),
            max_changes=int(params.get("max_changes", 0)),
            cls=params.get("cls"),
        ))
        return {"result": result}

    return {"error": f"Unsupported tool on Termux connector: {tool}"}


def health_payload() -> dict:
    return {
        "status": "healthy",
        "service": "irctc-termux-connector",
        "irctc_cookie_set": bool(os.environ.get("IRCTC_COOKIE", "").strip()),
        "worker_secret_set": bool(os.environ.get("IRCTC_WORKER_SECRET", "").strip()),
    }


class ConnectorHandler(BaseHTTPRequestHandler):
    server_version = "TermuxIRCTCConnector/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/health"}:
            self._send_json(200, health_payload())
        elif path in {"/openapi", "/openapi.json"}:
            self._send_json(200, openapi_spec())
        else:
            self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/run":
            self._send_json(404, {"detail": "not found"})
            return
        try:
            body = self._read_json()
            payload = run_tool(str(body.get("tool", "")), body.get("parameters") or {})
            self._send_json(200, payload)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)[:500]})


def main() -> None:
    port = int(os.environ.get("IRCTC_CONNECTOR_PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), ConnectorHandler)
    print(f"Termux connector listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
