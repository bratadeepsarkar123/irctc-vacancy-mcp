# IRCTC Vacancy MCP

> Ask Perplexity or Claude: *"I'm on train 12302, boarded NDLS, going to CNB — any free Sleeper berths?"*
> Get: a filtered list of vacant berths for exactly your segment. No AI analysis of raw data.

## How It Works

```
User query
    │
    ▼
AI extracts: train_no, date, boarded_from, travel_to, class
    │
    ▼
MCP tool call → Python does ALL the work:
  1. POST /api/trainComposition    → coach list (cdd key)
  2. GET  /trnscheduleenquiry/     → full station order
  3. POST /api/coachComposition    → per-berth bsd data [PARALLEL, semaphore-5]
  4. vacant_windows_from_bsd()     → find gaps (occupancy=False segments)
  5. window_covers()               → does user segment fit?
  6. format_result()               → compact 10-line string
    │
    ▼
AI receives THIS (never raw data):
  "Train 12302 (KOLKATA MAIL) | 2026-06-04
   Your segment: NDLS → CNB
   Vacant berths found: 3

   ── Coach S4 (SL) ──
     Berth  32 [LB]  free NDLS → CNB
     Berth  47 [UB]  free NDLS → HWH
   ── Coach S6 (SL) ──
     Berth  12 [SL]  free NDLS → CNB"
```

## Confirmed API Endpoints (from HAR + JS bundle analysis)

| Endpoint | Method | Auth? |
|---|---|---|
| `/online-charts/api/trainComposition` | POST | None |
| `/online-charts/api/coachComposition` | POST | None |
| `/online-charts/api/vacantBerth` | POST | None (fallback) |
| `/eticketing/protected/mapps1/trnscheduleenquiry/{no}` | GET | None |

> Verified: no cookies or tokens needed for chart pages (confirmed incognito + HAR).
> **However:** Akamai WAF blocks datacenter IPs (Azure/GCP/AWS). Run locally.

## Quick Start

```bash
git clone https://github.com/bratadeepsarkar123/irctc-vacancy-mcp
cd irctc-vacancy-mcp
pip install -r requirements.txt

# HTTP server (Perplexity connector)
cd src
uvicorn http_server:app --port 8000 --reload

# For public HTTPS URL (paste into Perplexity as custom connector):
cloudflared tunnel --url http://localhost:8000
```

## MCP Setup (Claude Desktop / Cursor / Windsurf)

Copy `mcp_config.json` content into your IDE's MCP config. Replace `PATH_TO_REPO`:

```json
{
  "mcpServers": {
    "irctc-vacancy": {
      "command": "python",
      "args": ["/path/to/irctc-vacancy-mcp/src/mcp_server.py"]
    }
  }
}
```

Then ask: *"Check train 12302 for vacant berths, I boarded NDLS going to CNB"*

## Deployment

| Option | Works? | Notes |
|---|---|---|
| Local machine | ✅ Always | Residential IP passes Akamai |
| Cloudflare Tunnel + local | ✅ Best | Public HTTPS, runs locally |
| Oracle Cloud Free Tier | ✅ Usually | Less aggressively blocked |
| Azure / GCP / AWS | ❌ Blocked | Akamai WAF flags datacenter IPs |

## File Structure

```
src/
  irctc_api.py       — API client (POST endpoints, retry, parallel async)
  vacancy_filter.py  — Pure-Python filtering (real bsd structure)
  main_tool.py       — Orchestrator (single entry point for all servers)
  mcp_server.py      — STDIO MCP (Claude Desktop / Cursor / AI IDEs)
  http_server.py     — FastAPI HTTP (Perplexity connector)
  session_helper.py  — Playwright cookie extractor (use if Akamai blocks)
tests/
  test_vacancy_filter.py  — Unit tests (pytest)
AGENTS.md          — Architecture docs for Jules / AI agents
```

## Run Tests

```bash
pip install pytest
pytest tests/ -v
```

## If You Get Blocked (403 from Akamai)

Run `session_helper.py` once to grab cookies via Playwright:

```bash
pip install playwright
playwright install chromium
python src/session_helper.py
# Copy the printed cookie string, then:
export IRCTC_COOKIE="your_cookie_string_here"
```

Then restart the server. The cookie is injected automatically.
