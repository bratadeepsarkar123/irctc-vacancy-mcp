# IRCTC Vacancy MCP

> Ask Perplexity: *"I'm on train 12302, boarded NDLS, going to CNB, any free Sleeper berths?"*
> Get: a filtered list of vacant berths for exactly your segment.

## Architecture

```
User query (natural language)
      |
      v
AI extracts: train_no, date, boarded_from, travel_to, class
      |
      v
POST /check_irctc_vacancy
      |
      v
irctc_api.py:
  1. POST /api/trainComposition           -> coach list + train meta
  2. GET  /trnscheduleenquiry/{train_no}  -> full ordered station list
  3. POST /api/vacantBerth (x N parallel) -> one per coach
      |
      v
vacancy_filter.py:
  - vacant_windows() per berth  (find gaps between booked segments)
  - window_covers()             (does user segment fit in this gap?)
  - format_result()             (compact 10-line string)
      |
      v
AI receives compact summary — never raw chart data
```

## Confirmed API Endpoints (HAR + JS bundle analysis)

| Endpoint | Method | Auth? |
|---|---|---|
| `/online-charts/api/trainComposition` | POST | None |
| `/online-charts/api/vacantBerth` | POST | None |
| `/eticketing/protected/mapps1/trnscheduleenquiry/{no}` | GET | None |

Verified in incognito with zero cookies/tokens in requests.

**Akamai WAF blocks cloud datacenter IPs** (Azure/GCP/AWS). Run locally.

## Quick Start

```bash
pip install -r requirements.txt
cd src
uvicorn mcp_server:app --port 8000 --reload
```

Add `http://localhost:8000` as Perplexity custom connector.
For public HTTPS: use Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:8000`).

## File Structure

```
src/
  irctc_api.py       -- API client (no auth, async parallel, schedule endpoint)
  vacancy_filter.py  -- All filtering logic (vacant windows, segment match, format)
  mcp_server.py      -- FastAPI + Perplexity tool manifest
requirements.txt
README.md
```

## Deployment

| Option | Works? | Notes |
|---|---|---|
| Local machine | Always | Residential IP |
| Cloudflare Tunnel + local | Best | Public HTTPS, runs locally |
| Oracle Cloud Free | Usually | Less blocked |
| Azure / GCP / AWS | Blocked | Akamai WAF |
