#!/usr/bin/env python
"""Poll permanent Cloud Run control plane for jobs and execute them locally."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request


CONTROL_URL = os.environ["GCP_CONTROL_URL"].rstrip("/")
TOKEN = os.environ["GCP_REGISTER_TOKEN"]
POLL_SECS = float(os.environ.get("GCP_POLL_SECS", "2"))
SSL_CONTEXT = ssl._create_unverified_context()


def network_hint() -> str:
    try:
        response = urllib.request.urlopen(f"{CONTROL_URL}/health", timeout=10, context=SSL_CONTEXT)
        body = response.read(1000).decode("utf-8", errors="ignore").lower()
    except Exception:
        return ""
    if "gateway.iitk.ac.in" in body or "fgtauth" in body or "<html" in body:
        return "network_captive_portal_or_gateway_auth_required"
    return ""


def request_json(url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-Register-Token": TOKEN},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT).read().decode("utf-8"))


def run_local(request_payload: dict) -> dict:
    data = json.dumps(request_payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/run",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode("utf-8"))


def main() -> None:
    print("cloud_poller_started", CONTROL_URL, flush=True)
    while True:
        try:
            payload = request_json(f"{CONTROL_URL}/worker/next", {}, timeout=20)
            job = payload.get("job")
            if not job:
                time.sleep(POLL_SECS)
                continue
            job_id = job["job_id"]
            print("job_started", job_id, flush=True)
            try:
                result = run_local(job.get("request") or {})
            except Exception as exc:
                result = {"error": f"local worker failed: {str(exc)[:500]}"}
            request_json(f"{CONTROL_URL}/worker/complete", {"job_id": job_id, "result": result}, timeout=30)
            print("job_completed", job_id, flush=True)
        except Exception as exc:
            hint = network_hint()
            suffix = f" {hint}" if hint else ""
            print("poll_error", type(exc).__name__, str(exc)[:250] + suffix, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
