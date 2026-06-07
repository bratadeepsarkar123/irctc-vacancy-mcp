#!/usr/bin/env python
"""Small Android/Termux chart worker without FastAPI/Pydantic.

This avoids pydantic-core/Rust builds on Termux while keeping the same HTTP
surface as src.chart_worker for the control plane.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


@contextmanager
def local_irctc_calls():
    worker_url = os.environ.pop("IRCTC_WORKER_URL", None)
    try:
        yield
    finally:
        if worker_url is not None:
            os.environ["IRCTC_WORKER_URL"] = worker_url


class ChartWorkerHandler(BaseHTTPRequestHandler):
    server_version = "TermuxIRCTCChartWorker/1.0"

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

    def _verify_secret(self) -> bool:
        secret = os.environ.get("IRCTC_WORKER_SECRET", "").strip()
        if not secret:
            return True
        return self.headers.get("X-Worker-Secret", "") == secret

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/chart/health":
            self._send_json(404, {"detail": "not found"})
            return
        if not self._verify_secret():
            self._send_json(401, {"detail": "Invalid or missing X-Worker-Secret"})
            return
        self._send_json(200, {
            "status": "ok",
            "service": "irctc-chart-worker",
            "runtime": "termux-stdlib",
            "cookie_set": bool(os.environ.get("IRCTC_COOKIE", "").strip()),
            "timestamp": time.time(),
        })

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._verify_secret():
            self._send_json(401, {"detail": "Invalid or missing X-Worker-Secret"})
            return
        try:
            body = self._read_json()
            with local_irctc_calls():
                from irctc_api import coach_composition, train_composition, vacant_berth

                if path == "/chart/trainComposition":
                    result = train_composition(
                        str(body.get("trainNo", "")),
                        str(body.get("jDate", "")),
                        str(body.get("boardingStation", "")),
                    )
                elif path == "/chart/coachComposition":
                    result = coach_composition(
                        str(body.get("trainNo", "")),
                        str(body.get("jDate", "")),
                        str(body.get("boardingStation", "")),
                        str(body.get("coach", "")),
                        str(body.get("cls", "")),
                    )
                elif path == "/chart/vacantBerth":
                    result = vacant_berth(
                        train_no=str(body.get("trainNo", "")),
                        jdate=str(body.get("jDate", "")),
                        boarding=str(body.get("boardingStation", "")),
                        remote=str(body.get("remoteStation", "")),
                        source=str(body.get("trainSourceStation", "")),
                        train_start_date=str(body.get("trainStartDate") or body.get("jDate", "")),
                        coach=str(body.get("coach", "")),
                        cls=str(body.get("clse") or body.get("cls", "")),
                        chart_type=str(body.get("chartType", 2)),
                    )
                elif path == "/chart/set-cookie":
                    cookie = str(body.get("cookie", "")).strip()
                    if not cookie:
                        self._send_json(422, {"detail": "cookie must not be empty"})
                        return
                    os.environ["IRCTC_COOKIE"] = cookie
                    result = {"status": "ok", "cookie_length": len(cookie)}
                else:
                    self._send_json(404, {"detail": "not found"})
                    return
            self._send_json(200, result)
        except PermissionError as exc:
            self._send_json(403, {"detail": str(exc)})
        except Exception as exc:
            self._send_json(502, {"detail": f"IRCTC upstream error: {exc}"})


def main() -> None:
    port = int(os.environ.get("IRCTC_WORKER_PORT", "8001"))
    server = ThreadingHTTPServer(("127.0.0.1", port), ChartWorkerHandler)
    print(f"Termux IRCTC chart worker listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
