# IRCTC Vacancy MCP

An MCP (Model Context Protocol) server that lets you ask natural-language questions about vacant berths on Indian Railway trains — backed by real-time data from IRCTC's chart vacancy APIs.

## What it does

You say:
> "I'm on train 12302, boarded at NDLS, going to CNB. Any seat free?"

The server:
1. Fetches the train composition (list of coaches) from IRCTC
2. Fetches vacant berth data per coach (parallel requests)
3. Filters berths where the **vacant window overlaps** your journey segment
4. Returns a compact, pre-filtered result — no raw data dump to the AI

The AI (Perplexity / Claude) only sees the filtered output, never the raw chart.

## Architecture

```
User query
  → AI extracts: trainNo, boardedFrom, travelTo, cls
  → MCP tool called
    → irctc_scraper.py
      1. POST /online-charts/api/trainComposition
      2. POST /online-charts/api/vacantBerth  (per coach, parallel)
      3. compute_vacant_windows()  — pure Python, no AI
      4. station_in_range()        — index comparison
    → compact string returned to AI
  → AI narrates best option to user
```

## Real API Endpoints (from browser HAR)

| Purpose | Method | URL |
|---|---|---|
| Train composition | POST | `https://www.irctc.co.in/online-charts/api/trainComposition` |
| Vacant berths | POST | `https://www.irctc.co.in/online-charts/api/vacantBerth` |
| Coach composition | POST | `https://www.irctc.co.in/online-charts/api/coachComposition` |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
# For session helper (optional but recommended):
pip install playwright && playwright install chromium
```

### 2. Mode A — Claude Desktop (STDIO MCP)

Copy `mcp_config.json` content into `~/.claude/claude_desktop_config.json`.
Update the path to match your local clone.

### 3. Mode B — Perplexity Connector (HTTP)

```bash
uvicorn src.http_server:app --host 0.0.0.0 --port 8000 --reload
```

Then add `http://localhost:8000` as a custom connector in Perplexity.

### 4. Deploy to Render / Railway (for persistent connector)

Set environment variable `PORT` and deploy `src/http_server.py` as a web service.
Add your deployed URL as the Perplexity connector endpoint.

## Usage Examples

**Claude Desktop / Perplexity:**
- "Train 12302, I got on at NDLS, going to CNB. Any SL berth free?"
- "12951 Rajdhani, boarded Mumbai Central, going to Delhi. Any 1A berth available from BCT to NDLS?"

## Cookie / Auth Note

IRCTC may return 403 for repeated unauthenticated requests. Run `session_helper.py` once to capture a browser session cookie, then set it as the `IRCTC_COOKIE` environment variable.

```bash
python src/session_helper.py
# Prints: export IRCTC_COOKIE="...your cookie string..."
```

Then:
```bash
export IRCTC_COOKIE="..."
uvicorn src.http_server:app --port 8000
```

## Project Structure

```
irctc-vacancy-mcp/
  src/
    irctc_scraper.py     ← Core: fetch + filter logic
    mcp_server.py        ← STDIO MCP for Claude Desktop
    http_server.py       ← FastAPI HTTP connector (Perplexity)
    session_helper.py    ← Playwright cookie extractor
  demo/
    tester.html          ← Browser UI for local testing
  README.md
  requirements.txt
  mcp_config.json
```

## License

MIT
