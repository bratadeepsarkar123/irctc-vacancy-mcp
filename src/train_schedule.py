"""
train_schedule.py — Train schedule with station names, timings, position & LIVE delay
=====================================================================================
Primary: runningstatus.in for per-station schedule AND live running status.
Fallback: confirmtkt.com for schedule, erail.in for metadata.
Live: Parses runningstatus.in HTML for real-time delay, actual times,
      last reported station, and upcoming station info.

No external auth/API keys needed — all sources are public.
"""

import logging
import re
import time
import json as _json
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as cffi_requests
    _HAVE_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore[no-redef]
    _HAVE_CFFI = False

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# In-memory cache: {train_no: (timestamp, schedule_list)}
_SCHEDULE_CACHE: dict = {}
_CACHE_TTL_SECS = 3600  # 1 hour — static schedule never changes day-to-day

# Live running status cache: {train_no: (timestamp, live_dict)}
# Short TTL because live data changes every few minutes
_LIVE_CACHE: dict = {}
_LIVE_CACHE_TTL_SECS = 60  # 60 seconds — live data refreshes frequently


# ── Schedule Sources ──────────────────────────────────────────────────────────


def _fetch_runningstatus_schedule(train_no: str) -> list:
    """
    Scrape per-station schedule from runningstatus.in.

    URL pattern confirmed from HAR: /status/{train_no}-on-{YYYYMMDD}
    e.g. https://runningstatus.in/status/12184-on-20260604

    The page also contains JSON-LD structured data:
      {"trainNumber":"12184","departureStation":{"identifier":"MBDP"},
       "arrivalStation":{"identifier":"BPL"},"date":"2026-06-04","status":"Not_running"}

    Returns list of dicts: [{code, name, arrival, departure, halt, distance_km, day}, ...]
    Uses a 1-hour in-memory cache (static schedule never changes).
    """
    cached = _SCHEDULE_CACHE.get(train_no)
    if cached:
        ts, data = cached
        if time.time() - ts < _CACHE_TTL_SECS:
            logger.info("Schedule cache hit for train %s", train_no)
            return data

    try:
        from datetime import date as _date
        today_str = _date.today().strftime("%Y%m%d")  # e.g. 20260604

        # Exact URL pattern confirmed from HAR capture
        url = f"https://runningstatus.in/status/{train_no}-on-{today_str}"
        kwargs = {"impersonate": "chrome124"} if _HAVE_CFFI else {}
        r = cffi_requests.get(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "identity",
                "Referer": "https://runningstatus.in/",  # HAR showed Referer is always sent
            },
            timeout=8,
            **kwargs,
        )
        if r.status_code != 200:
            logger.warning("runningstatus.in returned %d for %s", r.status_code, train_no)
            return []

        text = r.text

        # Fast-path: JSON-LD structured data in <script type="application/ld+json">
        # Gives us departure/arrival station identifiers confirmed from HAR
        try:
            import json as _json
            jsonld_match = re.search(
                r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL
            )
            if jsonld_match:
                jsonld = _json.loads(jsonld_match.group(1))
                dep_stn = jsonld.get("departureStation", {}).get("identifier", "")
                arr_stn = jsonld.get("arrivalStation", {}).get("identifier", "")
                train_date_str = jsonld.get("date", "")
                logger.info(
                    "runningstatus JSON-LD: train %s | %s → %s | date %s",
                    train_no, dep_stn, arr_stn, train_date_str,
                )
        except Exception:
            train_date_str = today_str

        # Parse train departure date for day-calculation
        try:
            from datetime import datetime as _dt
            dep_date = _dt.strptime(train_date_str, "%Y-%m-%d").date()
        except Exception:
            dep_date = _date.today()

        def to_24h(h_m: str, ampm: str) -> str:
            """Convert 'HH:MM' + 'AM'/'PM' to 24-hour 'HH:MM'."""
            try:
                h, m = h_m.split(":")
                h = int(h)
                if ampm == "PM" and h != 12:
                    h += 12
                elif ampm == "AM" and h == 12:
                    h = 0
                return f"{h:02d}:{m}"
            except Exception:
                return ""

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.DOTALL)

        stations = []
        for row in rows:
            clean = re.sub(r"<[^>]+>", " ", row)
            clean = re.sub(r"\s+", " ", clean).strip()
            if not clean:
                continue

            # Station code is in parentheses: "STATION NAME (CODE)"
            code_match = re.search(r"\(([A-Z]{2,6})\)", clean)
            if not code_match:
                continue
            code = code_match.group(1)

            # Station name is before the parenthesis
            name_match = re.match(r"^(.+?)\s*\(" + re.escape(code) + r"\)", clean)
            name = name_match.group(1).strip().title() if name_match else code

            # Times: "HH:MM AM" or "HH:MM PM"
            times = re.findall(r"(\d{1,2}:\d{2})\s*(AM|PM)", clean)

            # Day: parse actual date strings e.g. "04-Jun" "05-Jun" vs dep_date
            dates_found = re.findall(r"(\d{2})-([A-Za-z]{3})", clean)
            day = 1
            if dates_found:
                from datetime import datetime as _dt2
                for d_str, m_str in dates_found:
                    try:
                        row_date = _dt2.strptime(f"{d_str}-{m_str}-{dep_date.year}", "%d-%b-%Y").date()
                        diff = (row_date - dep_date).days
                        if diff >= 0:
                            day = diff + 1
                            break
                    except ValueError:
                        pass

            arr_raw = times[0] if times else None
            dep_raw = times[1] if len(times) > 1 else None
            arr = to_24h(*arr_raw) if arr_raw else ""
            dep = to_24h(*dep_raw) if dep_raw else ""

            stations.append({
                "code": code,
                "name": name,
                "arrival": arr,
                "departure": dep or arr,
                "halt": "",
                "distance_km": 0.0,
                "day": day,
            })

        if stations:
            _SCHEDULE_CACHE[train_no] = (time.time(), stations)
            logger.info("runningstatus: Cached %d stations for train %s", len(stations), train_no)

        return stations

    except Exception as e:
        logger.warning("runningstatus schedule fetch failed: %s", e)
        return []


def _fetch_confirmtkt_schedule(train_no: str) -> list:
    """
    Scrape per-station schedule from confirmtkt.com (fallback).
    Returns list of dicts:
      [{code, name, arrival, departure, halt, distance_km, day}, ...]
    sorted in route order.
    Uses a 1-hour in-memory cache so repeat calls don't hit the network.
    """
    # Check cache first
    cached = _SCHEDULE_CACHE.get(train_no)
    if cached:
        ts, data = cached
        if time.time() - ts < _CACHE_TTL_SECS:
            logger.info("Schedule cache hit for train %s", train_no)
            return data

    try:
        url = f"https://www.confirmtkt.com/train-schedule/{train_no}"
        kwargs = {"impersonate": "chrome124"} if _HAVE_CFFI else {}
        r = cffi_requests.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html", "Accept-Encoding": "identity"},
            timeout=8,  # Shorter timeout — fail fast, erail is the backup
            **kwargs,
        )
        if r.status_code != 200:
            return []

        text = r.text

        # Find schedule table
        tables = re.findall(r'<table[^>]*>(.*?)</table>', text, re.DOTALL)
        schedule_table = None
        for t in tables:
            if re.search(r'href="/station/[A-Z]', t):
                schedule_table = t
                break

        if not schedule_table:
            return []

        # Extract rows
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', schedule_table, re.DOTALL)
        stations = []

        for row in rows:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(tds) < 5:
                continue

            # Clean HTML from each td
            cleaned = []
            for td in tds:
                clean = re.sub(r'<[^>]+>', ' ', td).strip()
                clean = re.sub(r'\s+', ' ', clean)
                cleaned.append(clean)

            # Extract station code from link
            stn_match = re.search(r'href="/station/([A-Z0-9]+)"', tds[1], re.IGNORECASE)
            if not stn_match:
                continue

            code = stn_match.group(1).upper()

            # Extract station name (before the " - CODE" part)
            name_match = re.search(r'>\s*([^<]+?)\s*-\s*[A-Z0-9]+\s*<', tds[1])
            name = name_match.group(1).strip() if name_match else code

            # Parse fields: serial, station, arrival, departure, scheduled_dep, halt, distance, platform, day
            arrival = cleaned[2].strip() if len(cleaned) > 2 else ""
            departure = cleaned[3].strip() if len(cleaned) > 3 else ""
            halt = cleaned[5].strip() if len(cleaned) > 5 else ""
            distance = cleaned[6].strip() if len(cleaned) > 6 else ""
            day = cleaned[8].strip() if len(cleaned) > 8 else "1"

            # Normalize "Start" and "End"
            if arrival.lower() == "start":
                arrival = departure  # source station has no arrival
            if departure.lower() == "end":
                departure = ""  # dest station has no departure

            # Parse distance
            dist_km = 0.0
            dist_match = re.search(r'([\d.]+)', distance)
            if dist_match:
                dist_km = float(dist_match.group(1))

            stations.append({
                "code": code,
                "name": name,
                "arrival": arrival,
                "departure": departure,
                "halt": halt,
                "distance_km": dist_km,
                "day": int(day) if day.isdigit() else 1,
            })

        # Store in cache
        if stations:
            _SCHEDULE_CACHE[train_no] = (time.time(), stations)
            logger.info("Cached schedule for train %s (%d stations)", train_no, len(stations))

        return stations

    except Exception as e:
        logger.warning("confirmtkt schedule fetch failed: %s", e)
        return []


def _fetch_erail_metadata(train_no: str) -> Optional[dict]:
    """
    Fetch basic train metadata from erail.in.
    Returns dict with train_name, source, dest, times, running_days.
    """
    try:
        url = f"https://erail.in/rail/getTrains.aspx?TrainNo={train_no}&DataSource=0&Language=0&Cache=true"
        kwargs = {"impersonate": "chrome124"} if _HAVE_CFFI else {}
        r = cffi_requests.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Referer": "https://erail.in/"},
            timeout=15, **kwargs,
        )
        if r.status_code != 200 or not r.text or len(r.text) < 20:
            return None

        parts = r.text.strip().split("^")
        if len(parts) < 2:
            return None
        fields = parts[1].split("~")
        if len(fields) < 14:
            return None

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        running_days_str = fields[13] if len(fields) > 13 else ""
        running_days = [day_names[i] for i, ch in enumerate(running_days_str[:7]) if ch == "1"]

        return {
            "train_name": fields[1],
            "source_code": fields[3],
            "dest_code": fields[5],
            "departure_time": fields[10].replace(".", ":") if len(fields) > 10 else "",
            "arrival_time": fields[11].replace(".", ":") if len(fields) > 11 else "",
            "journey_hours": fields[12].replace(".", ":") if len(fields) > 12 else "",
            "running_days": running_days,
        }
    except Exception as e:
        logger.warning("erail metadata fetch failed: %s", e)
        return None

# ── Live Running Status ───────────────────────────────────────────────────────


def _fetch_live_running_status(train_no: str) -> Optional[dict]:
    """
    Fetch LIVE running status from runningstatus.in HTML.

    Extracts:
    - live_text: Human-readable status (e.g., "Departed from Shahbad Marknda at ...")
    - last_station: Last reported station name
    - last_station_code: Last reported station code (if parseable)
    - next_stoppage: Next stoppage station name
    - delay_mins: Current delay in minutes (from latest delay cell)
    - delay_status: "late" | "on_time" | "unknown"
    - per_station_delays: list of {code, arr_delay, dep_delay, actual_arr, actual_dep, status}
    - updated_at: IST timestamp of data

    Uses a 60-second in-memory cache.
    """
    cached = _LIVE_CACHE.get(train_no)
    if cached:
        ts, data = cached
        if time.time() - ts < _LIVE_CACHE_TTL_SECS:
            logger.info("Live status cache hit for train %s", train_no)
            return data

    try:
        from datetime import date as _date
        today_str = _date.today().strftime("%Y%m%d")
        url = f"https://runningstatus.in/status/{train_no}-on-{today_str}"
        kwargs = {"impersonate": "chrome124"} if _HAVE_CFFI else {}
        r = cffi_requests.get(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "identity",
                "Referer": "https://runningstatus.in/",
            },
            timeout=10,
            **kwargs,
        )
        if r.status_code != 200:
            logger.warning("runningstatus.in live status returned %d", r.status_code)
            return None

        text = r.text
        result: dict = {"updated_at": _now_ist().strftime("%H:%M IST")}

        # ── 1. Live banner text ──
        banner = re.search(
            r'class="[^"]*live-tracker-banner[^"]*"[^>]*>(.*?)</div>\s*</div>',
            text, re.DOTALL,
        )
        if banner:
            live_text = re.sub(r'<[^>]+>', ' ', banner.group(1))
            live_text = re.sub(r'\s+', ' ', live_text).strip()
            # Remove "Live Status" prefix
            live_text = re.sub(r'^Live\s+Status\s*', '', live_text, flags=re.IGNORECASE).strip()
            result["live_text"] = live_text

            # Parse: "Departed from <STATION> at <DATE>..."
            departed = re.search(r'Departed from\s+(.+?)\s+at\s+(\S+)', live_text, re.IGNORECASE)
            arrived = re.search(r'Arrived at\s+(.+?)\s+at\s+(\S+)', live_text, re.IGNORECASE)
            if departed:
                result["last_station"] = departed.group(1).strip()
            elif arrived:
                result["last_station"] = arrived.group(1).strip()

            next_stop = re.search(r'Next stoppage station is\s+([^.]+)', live_text, re.IGNORECASE)
            if next_stop:
                result["next_stoppage"] = next_stop.group(1).strip().rstrip('.')

            upcoming = re.search(r'Upcoming station is\s+([^.]+)', live_text, re.IGNORECASE)
            if upcoming:
                result["upcoming_station"] = upcoming.group(1).strip().rstrip('.')

        # ── 2. Current location row ──
        current_row = re.search(
            r'id="current-location-row"[^>]*>(.*?)</tr>', text, re.DOTALL
        )
        if current_row:
            row_text = re.sub(r'<[^>]+>', ' ', current_row.group(1))
            row_text = re.sub(r'\s+', ' ', row_text).strip()
            code_match = re.search(r'\(([A-Z]{2,6})\)', row_text)
            if code_match:
                result["last_station_code"] = code_match.group(1)

        # ── 3. Per-station delay data ──
        table_match = re.search(
            r'<table[^>]*class="table table-hover[^"]*"[^>]*>(.*?)</table>',
            text, re.DOTALL,
        )
        if table_match:
            table_html = table_match.group(1)
            station_rows = re.findall(
                r'<tr[^>]*class="(row-\w+)"[^>]*>(.*?)</tr>',
                table_html, re.DOTALL,
            )

            per_station = []
            latest_delay = None
            for row_cls, row_html in station_rows:
                code_m = re.search(r'\(([A-Z]{2,6})\)', row_html)
                if not code_m:
                    continue
                code = code_m.group(1)

                stn_info = {
                    "code": code,
                    "status": row_cls.replace("row-", ""),
                }

                # Actual times
                actual_times = re.findall(
                    r'class="text-dark fw-bold">(\d{1,2}:\d{2}\s*[AP]M\s+\d{2}-[A-Za-z]{3})',
                    row_html,
                )
                if actual_times:
                    stn_info["actual_arr"] = actual_times[0].strip()
                    if len(actual_times) > 1:
                        stn_info["actual_dep"] = actual_times[1].strip()

                # Delay cells
                delays = re.findall(
                    r"<div class='delay-cell (delay-\w+)'>(.*?)</div>",
                    row_html, re.DOTALL,
                )
                for delay_cls, delay_content in delays:
                    clean = re.sub(r'<[^>]+>', '', delay_content).strip()
                    if "Arr:" in clean:
                        stn_info["arr_delay"] = clean.replace("Arr:", "").strip()
                    elif "Dep:" in clean:
                        stn_info["dep_delay"] = clean.replace("Dep:", "").strip()

                # Track latest delay for "passed" stations
                if row_cls == "row-passed":
                    dep_delay = stn_info.get("dep_delay", stn_info.get("arr_delay", ""))
                    if dep_delay and dep_delay != "RT":
                        try:
                            latest_delay = int(re.search(r'(\d+)', dep_delay).group(1))
                        except (AttributeError, ValueError):
                            pass
                    elif dep_delay == "RT":
                        latest_delay = 0

                per_station.append(stn_info)

            result["per_station_delays"] = per_station
            result["delay_mins"] = latest_delay if latest_delay is not None else None

            if latest_delay is not None:
                result["delay_status"] = "on_time" if latest_delay == 0 else "late"
            else:
                result["delay_status"] = "unknown"

        # ── 4. Train status from JSON-LD ──
        jsonld_match = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL
        )
        if jsonld_match:
            try:
                jsonld = _json.loads(jsonld_match.group(1))
                result["train_status"] = jsonld.get("status", "")
            except Exception:
                pass

        _LIVE_CACHE[train_no] = (time.time(), result)
        logger.info(
            "Live status fetched for train %s: %s, delay=%s",
            train_no,
            result.get("delay_status", "?"),
            result.get("delay_mins", "?"),
        )
        return result

    except Exception as e:
        logger.warning("Live running status fetch failed for %s: %s", train_no, e)
        return None


# ── Position Estimation ───────────────────────────────────────────────────────


def _now_ist() -> datetime:
    """Current time in IST (naive datetime)."""
    return datetime.now(IST).replace(tzinfo=None)


def _time_to_minutes(time_str: str, day: int = 1) -> Optional[int]:
    """Convert HH:MM string + day number to minutes from start of day 1."""
    m = re.match(r'(\d{1,2}):(\d{2})', time_str)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2)) + (day - 1) * 24 * 60


def estimate_position(schedule: list, now: Optional[datetime] = None) -> dict:
    """
    Estimate current train position from per-station schedule + current time.
    
    Returns dict with:
      status: "not_started" | "en_route" | "at_station" | "arrived" | "unknown"
      current_station: station code  
      current_name: station name
      next_station: next station code (or None)
      next_name: next station name (or None)
      progress_pct: journey progress %
      eta_next_mins: estimated minutes to next station
    """
    if not schedule or len(schedule) < 2:
        return {"status": "unknown", "error": "insufficient schedule data"}

    if now is None:
        now = _now_ist()

    now_minutes = now.hour * 60 + now.minute

    # Get source departure and dest arrival in minutes
    first = schedule[0]
    last = schedule[-1]
    dep_mins = _time_to_minutes(first.get("departure", ""), first.get("day", 1))
    arr_mins = _time_to_minutes(last.get("arrival", ""), last.get("day", 1))

    if dep_mins is None or arr_mins is None:
        return {"status": "unknown", "error": "could not parse schedule times"}

    # Current time relative to journey
    # For overnight trains (dep evening, arr next morning):
    #   - If it's early AM (e.g., 3 AM) and train departed last evening → day 2
    #   - If it's afternoon (e.g., 3 PM) before departure (7 PM) → NOT day 2
    current_mins = now_minutes
    if current_mins < dep_mins and dep_mins > 720:
        # We're before departure time. But is it "afternoon before tonight's train"
        # or "early morning on day 2 of an overnight journey"?
        hours_before_dep = (dep_mins - current_mins) / 60
        if hours_before_dep > 6:
            # More than 6 hours before departure AND departure is PM
            # → likely early morning, day 2 of overnight train
            current_mins += 24 * 60
        # else: it's a few hours before departure → genuinely not started

    # Before departure
    if current_mins < dep_mins:
        return {
            "status": "not_started",
            "current_station": first["code"],
            "current_name": first["name"],
            "next_station": schedule[1]["code"] if len(schedule) > 1 else None,
            "next_name": schedule[1]["name"] if len(schedule) > 1 else None,
            "departs_in_mins": dep_mins - current_mins,
            "progress_pct": 0,
        }

    # After arrival
    if current_mins >= arr_mins:
        return {
            "status": "arrived",
            "current_station": last["code"],
            "current_name": last["name"],
            "next_station": None,
            "next_name": None,
            "progress_pct": 100,
        }

    # En route — find which two stations we're between
    for i in range(len(schedule) - 1):
        stn_dep = _time_to_minutes(
            schedule[i].get("departure", "") or schedule[i].get("arrival", ""),
            schedule[i].get("day", 1)
        )
        next_arr = _time_to_minutes(
            schedule[i + 1].get("arrival", ""),
            schedule[i + 1].get("day", 1)
        )

        if stn_dep is None or next_arr is None:
            continue

        # Check if currently at a station (between arrival and departure)
        stn_arr = _time_to_minutes(schedule[i].get("arrival", ""), schedule[i].get("day", 1))
        if stn_arr is not None and stn_arr <= current_mins <= stn_dep:
            return {
                "status": "at_station",
                "current_station": schedule[i]["code"],
                "current_name": schedule[i]["name"],
                "next_station": schedule[i + 1]["code"],
                "next_name": schedule[i + 1]["name"],
                "eta_next_mins": next_arr - current_mins,
                "progress_pct": round(
                    (current_mins - dep_mins) / (arr_mins - dep_mins) * 100, 1
                ),
            }

        # Between two stations
        if stn_dep <= current_mins < next_arr:
            eta = next_arr - current_mins
            return {
                "status": "en_route",
                "current_station": schedule[i]["code"],
                "current_name": schedule[i]["name"],
                "next_station": schedule[i + 1]["code"],
                "next_name": schedule[i + 1]["name"],
                "eta_next_mins": eta,
                "progress_pct": round(
                    (current_mins - dep_mins) / (arr_mins - dep_mins) * 100, 1
                ),
            }

    # Fallback: linear estimation
    total = arr_mins - dep_mins
    elapsed = current_mins - dep_mins
    progress = elapsed / total if total > 0 else 0
    est_idx = min(int(progress * (len(schedule) - 1)), len(schedule) - 2)

    return {
        "status": "en_route",
        "current_station": schedule[est_idx]["code"],
        "current_name": schedule[est_idx]["name"],
        "next_station": schedule[est_idx + 1]["code"],
        "next_name": schedule[est_idx + 1]["name"],
        "progress_pct": round(progress * 100, 1),
    }


# ── Main Entry Point ─────────────────────────────────────────────────────────


def get_full_schedule(train_no: str, irctc_station_list: list) -> str:
    """
    Build a complete train schedule string for the AI.
    
    Combines:
    1. runningstatus.in — per-station schedule with timings (PRIMARY, fastest)
    2. confirmtkt.com — per-station schedule with timings (FALLBACK)
    3. erail.in — basic metadata (train name, running days)
    4. IRCTC station list — last resort if all above fail
    5. runningstatus.in LIVE — real-time delay, actual times, last reported station
    
    Returns a human-readable schedule with position estimate AND live delay.
    """
    # Source 1: runningstatus.in (primary — fast, reliable, all 56 stations)
    schedule = _fetch_runningstatus_schedule(train_no)

    # Source 2: confirmtkt (fallback if runningstatus fails)
    if not schedule or len(schedule) < 4:
        schedule = _fetch_confirmtkt_schedule(train_no)

    # Source 3: erail metadata (train name, running days)
    erail = _fetch_erail_metadata(train_no)

    # Source 4: LIVE running status (delay, last station, actual times)
    live = _fetch_live_running_status(train_no)
    
    lines = []
    
    # Header
    train_name = erail["train_name"] if erail else ""
    if schedule and not train_name:
        train_name = f"Train {train_no}"
    if erail:
        lines.append(f"Train {train_no} — {erail['train_name']}")
        if schedule:
            lines.append(f"Route: {schedule[0]['name']} ({schedule[0]['code']}) -> {schedule[-1]['name']} ({schedule[-1]['code']})")
        lines.append(f"Departs: {erail['departure_time']}  |  Arrives: {erail['arrival_time']}  |  Duration: {erail['journey_hours']}")
        lines.append(f"Runs on: {', '.join(erail['running_days']) if erail['running_days'] else 'Daily'}")
    elif schedule:
        lines.append(f"Train {train_no}")
        lines.append(f"Route: {schedule[0]['name']} ({schedule[0]['code']}) -> {schedule[-1]['name']} ({schedule[-1]['code']})")
    else:
        lines.append(f"Train {train_no}")
    
    lines.append("")

    # ── LIVE RUNNING STATUS (if available) ──
    if live:
        lines.append("=" * 60)
        lines.append("LIVE RUNNING STATUS (real-time from runningstatus.in)")
        lines.append("=" * 60)

        if live.get("live_text"):
            lines.append(f"  Status: {live['live_text']}")

        if live.get("last_station"):
            code_str = f" ({live['last_station_code']})" if live.get("last_station_code") else ""
            lines.append(f"  Last reported at: {live['last_station']}{code_str}")

        if live.get("next_stoppage"):
            lines.append(f"  Next stop: {live['next_stoppage']}")

        delay_mins = live.get("delay_mins")
        if delay_mins is not None:
            if delay_mins == 0:
                lines.append(f"  Delay: Running ON TIME (Right Time)")
            else:
                lines.append(f"  Delay: LATE by {delay_mins} minutes")
        else:
            lines.append(f"  Delay: Data not available")

        lines.append(f"  Updated: {live.get('updated_at', '?')}")
        lines.append("")

        # Per-station delay summary (last 5 passed stations)
        per_stn = live.get("per_station_delays", [])
        passed = [s for s in per_stn if s.get("status") == "passed"]
        if passed:
            recent = passed[-5:]  # last 5 stations the train passed through
            lines.append("Recent stations (actual times):")
            for s in recent:
                arr = s.get("actual_arr", "--")
                dep = s.get("actual_dep", "--")
                arr_d = s.get("arr_delay", "")
                dep_d = s.get("dep_delay", "")
                delay_str = ""
                if arr_d:
                    delay_str += f" [Arr: {arr_d}]"
                if dep_d:
                    delay_str += f" [Dep: {dep_d}]"
                lines.append(f"  {s['code']:6s}  Arr: {arr}  Dep: {dep}{delay_str}")
            lines.append("")
    
    # Station schedule table
    if schedule:
        lines.append(f"Schedule ({len(schedule)} stations):")
        lines.append(f"  {'#':>3s}  {'Code':<6s}  {'Station Name':<30s}  {'Arr':>5s}  {'Dep':>5s}  {'Day':>3s}  {'Dist':>7s}")
        lines.append(f"  {'---':3s}  {'------':6s}  {'------------------------------':30s}  {'-----':5s}  {'-----':5s}  {'---':3s}  {'-------':7s}")
        for i, stn in enumerate(schedule, 1):
            arr = stn.get("arrival", "--")
            dep = stn.get("departure", "--") or "--"
            day_str = f"D{stn.get('day', 1)}"
            dist_str = f"{stn['distance_km']:.0f} km" if stn.get("distance_km") else "--"
            lines.append(f"  {i:3d}  {stn['code']:<6s}  {stn['name']:<30s}  {arr:>5s}  {dep:>5s}  {day_str:>3s}  {dist_str:>7s}")
    elif irctc_station_list:
        lines.append(f"Stations ({len(irctc_station_list)} stops):")
        for i, stn in enumerate(irctc_station_list, 1):
            lines.append(f"  {i:3d}. {stn}")
    
    # Position estimate
    lines.append("")

    # Use LIVE data for position if available, otherwise fall back to timetable estimate
    if live and live.get("last_station_code"):
        # We have real live data — use it instead of timetable estimation
        lines.append(f"LIVE POSITION (IST {_now_ist().strftime('%H:%M')}):")
        code_str = f" ({live['last_station_code']})" if live.get('last_station_code') else ""
        lines.append(f"  Last reported: {live.get('last_station', '?')}{code_str}")
        if live.get("next_stoppage"):
            lines.append(f"  Next stoppage: {live['next_stoppage']}")
        delay = live.get('delay_mins')
        if delay is not None and delay > 0:
            lines.append(f"  Running LATE by {delay} minutes")
        elif delay == 0:
            lines.append(f"  Running ON TIME")

        # Also provide the timetable-estimated position for context
        if schedule:
            pos = estimate_position(schedule)
            lines.append(f"  Schedule-based progress: {pos.get('progress_pct', '?')}%")
            if pos.get("next_station"):
                lines.append(f"  current_station: {live.get('last_station_code', pos.get('current_station', '?'))}")
                lines.append(f"  next_station: {pos.get('next_station', '?')}")
    elif schedule:
        pos = estimate_position(schedule)
        if pos.get("status") == "en_route":
            lines.append(f"ESTIMATED POSITION (based on current IST time {_now_ist().strftime('%H:%M')}):")
            lines.append(f"  Currently between: {pos['current_name']} ({pos['current_station']})")
            if pos.get("next_station"):
                eta = pos.get("eta_next_mins", "?")
                lines.append(f"  Next station: {pos['next_name']} ({pos['next_station']}) -- ETA ~{eta} min")
            lines.append(f"  Journey progress: {pos['progress_pct']}%")
        elif pos.get("status") == "at_station":
            lines.append(f"ESTIMATED POSITION (IST {_now_ist().strftime('%H:%M')}):")
            lines.append(f"  Currently at: {pos['current_name']} ({pos['current_station']})")
            if pos.get("next_station"):
                lines.append(f"  Next station: {pos['next_name']} ({pos['next_station']}) -- ETA ~{pos.get('eta_next_mins', '?')} min")
            lines.append(f"  Journey progress: {pos['progress_pct']}%")
        elif pos.get("status") == "not_started":
            lines.append(f"Train has NOT departed yet. Departs in ~{pos.get('departs_in_mins', '?')} minutes.")
            lines.append(f"  Source: {pos.get('current_name', '')} ({pos.get('current_station', '')})")
        elif pos.get("status") == "arrived":
            lines.append(f"Train has reached destination: {pos.get('current_name', '')} ({pos.get('current_station', '')})")
    elif erail:
        now = _now_ist()
        dep_str = erail.get("departure_time", "")
        arr_str = erail.get("arrival_time", "")
        lines.append(f"Position estimate unavailable (no per-station schedule).")
        lines.append(f"Train departs at {dep_str}, arrives at {arr_str}. Current IST: {now.strftime('%H:%M')}.")
    
    lines.append("")
    if live:
        lines.append("Data source: runningstatus.in (LIVE). Delay and position are real-time.")
    else:
        lines.append("Note: Position is ESTIMATED from scheduled times. Live delay data was unavailable.")
    lines.append("To check vacant berths, use: check_irctc_vacancy(train_no, date, current_station, next_station)")
    
    return "\n".join(lines)
