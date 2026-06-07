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
from urllib.parse import quote, urlparse
from uuid import uuid4


STATE_OBJECT = os.environ.get("STATE_OBJECT", "worker-state.json")
JOB_OBJECT = os.environ.get("JOB_OBJECT", "job-state.json")
STATE_BUCKET = os.environ["STATE_BUCKET"]
REGISTER_TOKEN = os.environ["REGISTER_TOKEN"]
STALE_AFTER_SECS = int(os.environ.get("STALE_AFTER_SECS", "300"))
JOB_TIMEOUT_SECS = int(os.environ.get("JOB_TIMEOUT_SECS", "110"))


def _metadata_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    payload = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
    return payload["access_token"]


def _gcs_url(name: str) -> str:
    return f"https://storage.googleapis.com/upload/storage/v1/b/{STATE_BUCKET}/o?uploadType=media&name={quote(name, safe='')}"


def _gcs_get_url(name: str) -> str:
    return f"https://storage.googleapis.com/storage/v1/b/{STATE_BUCKET}/o/{quote(name, safe='')}?alt=media"


def save_json(name: str, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _gcs_url(name),
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {_metadata_token()}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(request, timeout=10).read()


def load_json(name: str) -> dict:
    request = urllib.request.Request(
        _gcs_get_url(name),
        headers={"Authorization": f"Bearer {_metadata_token()}"},
    )
    try:
        return json.loads(urllib.request.urlopen(request, timeout=10).read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def save_state(state: dict) -> None:
    save_json(STATE_OBJECT, state)


def load_state() -> dict:
    return load_json(STATE_OBJECT)


def save_job(job: dict) -> None:
    save_json(JOB_OBJECT, job)


def load_job() -> dict:
    return load_json(JOB_OBJECT)


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
            job = load_job()
            last_seen = float(state.get("last_seen", 0) or 0)
            age = int(time.time() - last_seen) if last_seen else None
            _json_response(self, 200, {
                "status": "healthy",
                "service": "irctc-cloud-control",
                "worker_registered": bool(state.get("worker_url")),
                "worker_age_secs": age,
                "worker_fresh": bool(last_seen and age is not None and age <= STALE_AFTER_SECS),
                "poll_worker_seen": bool(state.get("poll_last_seen")),
                "job_status": job.get("status"),
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

            if path == "/worker/next":
                token = self.headers.get("X-Register-Token", "")
                if token != REGISTER_TOKEN:
                    _json_response(self, 401, {"error": "invalid worker token"})
                    return
                state = load_state()
                state["poll_last_seen"] = time.time()
                save_state(state)
                job = load_job()
                if job.get("status") == "pending":
                    job["status"] = "running"
                    job["started_at"] = time.time()
                    save_job(job)
                    _json_response(self, 200, {
                        "job": {
                            "job_id": job.get("job_id"),
                            "request": job.get("request") or {},
                        }
                    })
                    return
                _json_response(self, 200, {"job": None})
                return

            if path == "/worker/complete":
                token = self.headers.get("X-Register-Token", "")
                if token != REGISTER_TOKEN:
                    _json_response(self, 401, {"error": "invalid worker token"})
                    return
                body = self._read_json()
                job = load_job()
                if body.get("job_id") != job.get("job_id"):
                    _json_response(self, 409, {"error": "job_id mismatch"})
                    return
                job["status"] = "completed"
                job["completed_at"] = time.time()
                job["result"] = body.get("result")
                save_job(job)
                _json_response(self, 200, {"status": "ok"})
                return

            if path == "/run":
                current = load_job()
                if current.get("status") in {"pending", "running"}:
                    _json_response(self, 200, {
                        "result": (
                            "IRCTC worker is busy with another request. "
                            "Please retry in a few seconds."
                        ),
                        "worker_status": "busy",
                    })
                    return
                job = {
                    "job_id": str(uuid4()),
                    "status": "pending",
                    "created_at": time.time(),
                    "request": self._read_json(),
                }
                save_job(job)
                deadline = time.time() + JOB_TIMEOUT_SECS
                while time.time() < deadline:
                    latest = load_job()
                    if latest.get("job_id") == job["job_id"] and latest.get("status") == "completed":
                        _json_response(self, 200, latest.get("result") or {"error": "empty worker result"})
                        return
                    time.sleep(1)
                _json_response(self, 200, {
                    "result": (
                        "IRCTC residential worker did not respond before timeout. "
                        "Cannot verify vacant/free chart windows right now."
                    ),
                    "worker_status": "timeout",
                })
                return

            _json_response(self, 404, {"detail": "not found"})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)[:500]})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
