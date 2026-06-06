@echo off
REM ======================================================================
REM start_worker.bat — Start IRCTC Chart Worker + Cloudflare Tunnel
REM ======================================================================
REM
REM This starts the residential chart worker on port 8001 and exposes it
REM through a Cloudflare quick tunnel. The generated tunnel URL should be
REM set as IRCTC_WORKER_URL on the cloud control plane.
REM
REM Prerequisites:
REM   - Python with FastAPI/uvicorn installed
REM   - cloudflared.exe in the project root (already present)
REM   - IRCTC_COOKIE env var set (or use grab_cookie.py first)
REM
REM Usage:
REM   cd irctc-mcp-live
REM   start_worker.bat
REM
REM To set a shared secret (recommended for production):
REM   set IRCTC_WORKER_SECRET=your-secret-here
REM   start_worker.bat
REM ======================================================================

setlocal

set WORKER_PORT=8001
if defined IRCTC_WORKER_PORT set WORKER_PORT=%IRCTC_WORKER_PORT%

echo.
echo ============================================================
echo  IRCTC Chart Worker (Residential IP)
echo ============================================================
echo  Worker port: %WORKER_PORT%
echo.

REM Load cookie from .env if not already set
if not defined IRCTC_COOKIE (
    if exist irctc_cookie.env (
        echo Loading cookie from irctc_cookie.env...
        for /f "tokens=1,* delims==" %%a in (irctc_cookie.env) do (
            if "%%a"=="IRCTC_COOKIE" set IRCTC_COOKIE=%%b
        )
    )
)

if defined IRCTC_COOKIE (
    echo IRCTC cookie is loaded.
) else (
    echo [WARNING] IRCTC_COOKIE is not set. Use grab_cookie.py or irctc_cookie.env before live chart calls.
)

if not defined IRCTC_WORKER_SECRET (
    echo [WARNING] IRCTC_WORKER_SECRET is not set. Public tunnel requests will be rejected unless IRCTC_WORKER_ALLOW_INSECURE=1.
)

REM Start the worker server in a new window
echo Starting chart worker server on port %WORKER_PORT%...
start "IRCTC Chart Worker" cmd /k "cd /d %~dp0 && python -m uvicorn src.chart_worker:app --host 0.0.0.0 --port %WORKER_PORT%"

REM Wait for the worker to start
echo Waiting for worker to start...
timeout /t 3 /nobreak >nul

REM Start Cloudflare Tunnel in this window
echo.
echo Starting Cloudflare Tunnel...
echo The tunnel URL will appear below — set it as IRCTC_WORKER_URL on the cloud control plane.
echo.
echo ============================================================
"%~dp0cloudflared.exe" tunnel --url http://localhost:%WORKER_PORT%
