"""
chart_worker.py — IRCTC Chart Worker (Residential IP)
======================================================
A lightweight FastAPI server that exposes the IRCTC chart API calls from
a residential IP. This runs on the user's home machine (PC/laptop/Pi)
and is connected to the cloud control plane via a reverse tunnel
(Cloudflare Tunnel, Tailscale, etc.).

Run:
  cd irctc-mcp-live
  uvicorn src.chart_worker:app --host 0.0.0.0 --port 8001

For tunnel exposure:
  cloudflared.exe tunnel --url http://localhost:8001

The cloud control plane (http_server.py) calls these endpoints through
the tunnel when IRCTC_WORKER_URL is set.

Authentication:
  Set IRCTC_WORKER_SECRET env var on both worker and control plane.
  Requests must include X-Worker-Secret header.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


# ── Auth ──────────────────────────────────────────────────────────────────────

def _verify_secret(request: Request) -> None:
    """Verify the shared secret header.

    Local requests may run without a secret for development. Non-local access
    through a public tunnel must either configure IRCTC_WORKER_SECRET or
    explicitly opt into insecure mode.
    """
    secret = os.environ.get("IRCTC_WORKER_SECRET", "").strip()
    if not secret:
        if os.environ.get("IRCTC_WORKER_ALLOW_INSECURE", "").strip() == "1":
            return
        client_host = getattr(getattr(request, "client", None), "host", "")
        if client_host in {"127.0.0.1", "::1", "localhost", "testclient"}:
            return
        raise HTTPException(status_code=401, detail="IRCTC_WORKER_SECRET is required for non-local access")
    header = request.headers.get("X-Worker-Secret", "")
    if header != secret:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Worker-Secret")


@contextmanager
def _local_irctc_calls():
    """Prevent the worker process from recursively calling itself."""
    worker_url = os.environ.pop("IRCTC_WORKER_URL", None)
    try:
        yield
    finally:
        if worker_url is not None:
            os.environ["IRCTC_WORKER_URL"] = worker_url


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="IRCTC Chart Worker",
    description="Residential-IP IRCTC chart API proxy for the split MCP architecture.",
    version="1.0.0",
)


# ── Request models ────────────────────────────────────────────────────────────

class TrainCompositionRequest(BaseModel):
    trainNo: str
    jDate: str
    boardingStation: str


class CoachCompositionRequest(BaseModel):
    trainNo: str
    jDate: str
    boardingStation: str
    coach: str
    cls: str


class VacantBerthRequest(BaseModel):
    trainNo: str
    jDate: str
    boardingStation: str
    remoteStation: str
    trainSourceStation: str
    coach: Optional[str] = None
    clse: Optional[str] = None
    cls: Optional[str] = None
    trainStartDate: Optional[str] = None
    chartType: int = 2


class SetCookieRequest(BaseModel):
    cookie: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/chart/health")
async def health(request: Request):
    """Liveness and readiness check for the chart worker."""
    _verify_secret(request)
    cookie_set = bool(os.environ.get("IRCTC_COOKIE", "").strip())
    return {
        "status": "ok",
        "service": "irctc-chart-worker",
        "version": "1.0.0",
        "cookie_set": cookie_set,
        "timestamp": time.time(),
    }


@app.post("/chart/trainComposition")
async def train_composition(req: TrainCompositionRequest, request: Request):
    """Proxy trainComposition through the residential IP."""
    _verify_secret(request)
    try:
        with _local_irctc_calls():
            from irctc_api import train_composition as _tc
            result = _tc(req.trainNo, req.jDate, req.boardingStation)
        return JSONResponse(content=result)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IRCTC upstream error: {e}")


@app.post("/chart/coachComposition")
async def coach_composition(req: CoachCompositionRequest, request: Request):
    """Proxy coachComposition through the residential IP."""
    _verify_secret(request)
    try:
        with _local_irctc_calls():
            from irctc_api import coach_composition as _cc
            result = _cc(req.trainNo, req.jDate, req.boardingStation, req.coach, req.cls)
        return JSONResponse(content=result)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IRCTC upstream error: {e}")


@app.post("/chart/vacantBerth")
async def vacant_berth(req: VacantBerthRequest, request: Request):
    """Proxy vacantBerth through the residential IP."""
    _verify_secret(request)
    try:
        with _local_irctc_calls():
            from irctc_api import vacant_berth as _vb
            result = _vb(
                train_no=req.trainNo,
                jdate=req.jDate,
                boarding=req.boardingStation,
                remote=req.remoteStation,
                source=req.trainSourceStation,
                train_start_date=req.trainStartDate or req.jDate,
                coach=req.coach or "",
                cls=req.clse or req.cls or "",
                chart_type=str(req.chartType),
            )
        return JSONResponse(content=result)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IRCTC upstream error: {e}")


@app.post("/chart/set-cookie")
async def set_cookie(req: SetCookieRequest, request: Request):
    """Update the IRCTC cookie on the worker."""
    _verify_secret(request)
    if not req.cookie:
        raise HTTPException(status_code=422, detail="cookie must not be empty")
    os.environ["IRCTC_COOKIE"] = req.cookie
    return {"status": "ok", "cookie_length": len(req.cookie)}


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("IRCTC_WORKER_PORT", "8001"))
    print(f"Starting IRCTC Chart Worker on port {port}...")
    print("Set IRCTC_WORKER_SECRET env var for authentication.")
    print("Expose via: cloudflared.exe tunnel --url http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
