#!/usr/bin/env python
"""Permanent ChatGPT connector front door for the Termux IRCTC worker.

Cloud Run serves a stable /openapi.json and /run URL. The tablet periodically
registers its current quick-tunnel URL via /register.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


STATE_OBJECT = os.environ.get("STATE_OBJECT", "worker-state.json")
STATE_BUCKET = os.environ["STATE_BUCKET"]
REGISTER_TOKEN = os.environ["REGISTER_TOKEN"]
STALE_AFTER_SECS = int(os.environ.get("STALE_AFTER_SECS", "300"))


def _metadata_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    payload = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
    return payload["access_token"]


def _gcs_url() -> str:
    return f"https://storage.googleapis.com/upload/storage/v1/b/{STATE_BUCKET}/o?uploadType=media&name={STATE_OBJECT}"


def _gcs_get_url() -> str:
    return f"https://storage.googleapis.com/storage/v1/b/{STATE_BUCKET}/o/{STATE_OBJECT}?alt=media"


def save_state(state: dict) -> None:
    data = json.dumps(state, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _gcs_url(),
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {_metadata_token()}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(request, timeout=10).read()


def load_state() -> dict:
    request = urllib.request.Request(
        _gcs_get_url(),
        headers={"Authorization": f"Bearer {_metadata_token()}"},
    )
    try:
        return json.loads(urllib.request.urlopen(request, timeout=10).read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def openapi_spec(base_url: str) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "IRCTC Vacancy Permanent Connector",
            "version": "1.0.0",
            "description": (
                "Stable ChatGPT connector. Requests are forwarded to the latest "
                "registered residential Termux worker. Vacancies are chart-vacant "
                "windows only, not booking guarantees."
            ),
        },
        "servers": [{"url": base_url.rstrip("/")}],
        "paths": {
            "/run": {
                "post": {
                    "operationId": "run_irctc_tool",
                    "summary": "Run IRCTC/NTES passenger tools",
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
                                        "parameters": {"type": "object"},
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


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "IRCTCCloudControl/1.0"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _base_url(self) -> str:
        proto = self.headers.get("X-Forwarded-Proto", "https")
        host = self.headers.get("Host", "")
        return f"{proto}://{host}"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/openapi", "/openapi.json"}:
            _json_response(self, 200, openapi_spec(self._base_url()))
            return
        if path in {"/", "/health"}:
            state = load_state()
            last_seen = float(state.get("last_seen", 0) or 0)
            age = int(time.time() - last_seen) if last_seen else None
            _json_response(self, 200, {
                "status": "healthy",
                "service": "irctc-cloud-control",
                "worker_registered": bool(state.get("worker_url")),
                "worker_age_secs": age,
                "worker_fresh": bool(last_seen and age is not None and age <= STALE_AFTER_SECS),
            })
            return
        _json_response(self, 404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/register":
                token = self.headers.get("X-Register-Token", "")
                if token != REGISTER_TOKEN:
                    _json_response(self, 401, {"error": "invalid registration token"})
                    return
                body = self._read_json()
                worker_url = str(body.get("worker_url", "")).strip().rstrip("/")
                if not worker_url.startswith("https://"):
                    _json_response(self, 422, {"error": "worker_url must be https"})
                    return
                state = {
                    "worker_url": worker_url,
                    "last_seen": time.time(),
                    "status": str(body.get("status", "online")),
                }
                save_state(state)
                _json_response(self, 200, {"status": "ok", "worker_url": worker_url})
                return

            if path == "/run":
                state = load_state()
                worker_url = str(state.get("worker_url", "")).rstrip("/")
                last_seen = float(state.get("last_seen", 0) or 0)
                age = int(time.time() - last_seen) if last_seen else None
                if not worker_url or not last_seen or age is None or age > STALE_AFTER_SECS:
                    _json_response(self, 200, {
                        "result": (
                            "IRCTC residential worker is offline or stale. "
                            "Cannot verify vacant/free chart windows right now."
                        ),
                        "worker_status": "offline",
                        "worker_age_secs": age,
                    })
                    return
                body_bytes = json.dumps(self._read_json()).encode("utf-8")
                request = urllib.request.Request(
                    f"{worker_url}/run",
                    data=body_bytes,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                response = urllib.request.urlopen(request, timeout=90)
                payload = json.loads(response.read().decode("utf-8"))
                _json_response(self, 200, payload)
                return

            _json_response(self, 404, {"detail": "not found"})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)[:500]})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
