@echo off
:: IRCTC Vacancy MCP — Local Server Starter
:: Double-click this to start the MCP server + ngrok tunnel.
:: Window stays open showing logs. Close it to stop.

title IRCTC MCP Server

cd /d "C:\Users\brata\Downloads\irctc-vacancy-mcp\irctc-mcp-live"

echo.
echo ============================================
echo   IRCTC MCP Server Starting...
echo ============================================
echo.
echo   Permanent URL (use this in Perplexity):
echo   https://ammonium-atop-smith.ngrok-free.dev/sse
echo.
echo   Starting Python MCP server on port 8001...
echo.

:: Start MCP server in background
start "IRCTC MCP Python" /min python src\mcp_server.py

:: Give Python 3 seconds to start
timeout /t 3 /nobreak >nul

:: Start ngrok tunnel (this window shows the tunnel status)
echo   Starting ngrok tunnel...
.\ngrok.exe start --config=ngrok.yml --all
