# AGENTS.md — IRCTC Vacancy MCP

## Project Purpose

MCP server that fetches IRCTC train chart vacancy data, filters it in pure Python
(no AI analysis), and returns pre-filtered berth availability as a compact string
to an AI assistant (Perplexity, Claude, Cursor, etc.).

**The AI never sees raw chart data. It only receives the final filtered result.**

## Architecture

```
User query → AI extracts [train_no, date, boarded_from, travel_to, class]
                    ↓
          MCP tool call: find_vacant_berths()
                    ↓
          irctc_api.py:
            1. POST /api/trainComposition  → coach list (cdd key)
            2. GET  /trnscheduleenquiry/   → full station order
            3. POST /api/coachComposition  → per-berth data (bdd/bsd keys) [PARALLEL]
                    ↓
          vacancy_filter.py:
            - vacant_windows_from_bsd()   → parse bsd segments (occupancy bool)
            - window_covers()              → user segment overlap check
            - filter_vacant()             → aggregate all coaches
            - format_result()             → compact string for AI
                    ↓
          "Coach S4: Berth 32 (LB) free NDLS→CNB"  ← AI gets THIS
```

## Confirmed API Endpoints (HAR + JS bundle analysis)

| Endpoint | Method | Key params | Key in response |
|---|---|---|---|
| `/online-charts/api/trainComposition` | POST | trainNo, jDate, boardingStation | `cdd[]` → coaches |
| `/online-charts/api/coachComposition` | POST | trainNo, jDate, boardingStation, coach, cls | `bdd[]` → berths |
| `/online-charts/api/vacantBerth` | POST | trainNo, jDate, boardingStation, coach, clse | fallback |
| `/eticketing/protected/mapps1/trnscheduleenquiry/{no}` | GET | — | `trainScheduleDetails[]` |

## Real bsd Structure (confirmed from JS bundle)

```json
"bsd": [
  {
    "occupancy": true,    // bool — True=occupied, False=vacant
    "from": "NDLS",       // station code
    "to":   "CNB",        // station code
    "quota": "GN"         // GN, GNRS, LD, DMGD, ...
  }
]
```

**Berth states:**
- `bsd == []` → VACANT (free for full route)
- All `occupancy=True` → FULL
- Mixed → PARTIAL (check gaps)
- `quota == "GNRS"` on vacant segment → skip (reserved)
- `quota == "DMGD"` → damaged berth (skip)
- `enable == false` → under repair (skip)

## File Structure

```
src/
  irctc_api.py       — API client (correct POST endpoints, retry, parallel async)
  vacancy_filter.py  — Pure-Python filtering (bsd structure, window math)
  main_tool.py       — Orchestrator (single entry point for both servers)
  mcp_server.py      — STDIO MCP server (Claude Desktop / Cursor / AI IDEs)
  http_server.py     — FastAPI HTTP server (Perplexity connector)
  irctc_scraper.py   — DEPRECATED shim (re-exports from main_tool)
  session_helper.py  — Playwright cookie extractor (use if Akamai blocks)
tests/
  test_vacancy_filter.py  — Unit tests (pytest)
```

## Known TODO (for Jules / future agents)

1. Live-test the `coachComposition` endpoint on a real charted train and log one
   full `bdd` response to confirm key names match this AGENTS.md
2. Live-test `vacantBerth` endpoint and document its response key structure
3. Add Playwright session refresh to `session_helper.py` using CDP (like NotebookLM MCP pattern)
4. Add Redis cookie caching with 25-min TTL for production deployment
5. Dockerize with `mcr.microsoft.com/playwright/python` base image for Azure Container Apps
6. Add integration test with a mock IRCTC server

## Do NOT Change

- `vacant_windows_from_bsd()` logic — confirmed from JS bundle
- `bsd.occupancy` boolean interpretation — confirmed correct
- `format_result()` output format — AI prompt depends on this structure
- The pipeline order in `main_tool.find_vacant_berths()`
