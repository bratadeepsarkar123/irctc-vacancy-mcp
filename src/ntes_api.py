"""
ntes_api.py - in-house NTES mobile endpoint client.

The public NTES web/app surface is not a documented third-party API. The Android
app currently talks to AppServAnd with an AES-CBC encrypted query string and an
MD5 signature prefix. This module keeps that unstable contract isolated from the
rest of the MCP server and returns small normalized shapes for app code.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import requests

try:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import pad, unpad
except ImportError:  # pragma: no cover - depends on installed package namespace
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad

logger = logging.getLogger(__name__)


class NTESError(Exception):
    """Raised when NTES request, crypto, or response parsing fails."""


class NTESCrypto:
    """AES-CBC + MD5 signature codec used by the NTES Android endpoint."""

    def __init__(self) -> None:
        self.key = b"8EA4DB2CC1EB3DC5"
        self.iv = b"7DC5EB3BB4DB6EA8"
        self.secret = "645fbc1e56e23365f2f3c204ae0899f6"

    def _encrypt(self, data: str) -> str:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        encrypted = cipher.encrypt(pad(data.encode("utf-8"), 16))
        return binascii.hexlify(base64.b64encode(encrypted)).decode("ascii").upper()

    def _decode_layers(self, enc: str) -> bytes:
        try:
            return base64.b64decode(binascii.unhexlify(enc).decode("ascii"))
        except Exception as exc:
            raise NTESError(f"NTES encoding error: {exc}") from exc

    def decode(self, enc: str) -> Any:
        if not enc:
            raise NTESError("NTES encrypted payload is empty")

        if "#" in enc:
            _signature, enc = enc.split("#", 1)

        try:
            cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
            text = unpad(cipher.decrypt(self._decode_layers(enc)), 16).decode("utf-8")
        except Exception as exc:
            raise NTESError(f"NTES decryption failed: {exc}") from exc

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _hash(self, data: str) -> str:
        return hashlib.md5((data + self.secret).encode("utf-8")).hexdigest().upper()

    def build(self, data: str) -> str:
        if not data:
            raise NTESError("NTES request payload is empty")
        return f"{self._hash(data)}#{self._encrypt(data)}"


def _first(data: dict, keys: Iterable[str], default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _clean_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _looks_like_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"source", "destination", "start", "end", "--"}:
        return ""
    m = re.search(r"(\d{1,2})[:.](\d{2})", text)
    if not m:
        return text
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def format_ntes_date(raw: str = "") -> str:
    """Return NTES date format, e.g. 02-May-2026."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return raw


def normalize_schedule(raw: Any) -> list[dict]:
    """Normalize common NTES schedule response shapes to train_schedule.py rows."""
    if not isinstance(raw, dict):
        return []

    stations = (
        raw.get("StationList")
        or raw.get("stationList")
        or raw.get("stations")
        or raw.get("vStationList")
        or raw.get("TrainRoute")
        or raw.get("STNS")
        or raw.get("route")
        or []
    )
    normalized: list[dict] = []

    for idx, station in enumerate(_as_list(stations), 1):
        if not isinstance(station, dict):
            continue
        code = _clean_code(_first(
            station,
            ("StnCode", "StationCode", "stationCode", "code", "stnCode", "station", "SC"),
        ))
        if not code:
            continue

        arrival = _looks_like_time(_first(
            station,
            ("ArrTime", "STA", "ArrivalTime", "arrival", "arr", "schArr", "ETA"),
        ))
        departure = _looks_like_time(_first(
            station,
            ("DepTime", "STD", "DepartureTime", "departure", "dep", "schDep", "ETD"),
        ))
        normalized.append({
            "code": code,
            "name": _clean_name(_first(
                station,
                ("StnName", "StationName", "stationName", "name", "station", "SN", "SHN"),
                code,
            )),
            "arrival": arrival or departure,
            "departure": departure or arrival,
            "halt": str(_first(station, ("Halt", "halt", "HaltTime"), "")),
            "distance_km": _to_float(_first(station, ("Distance", "distance", "Dist", "DIST"), 0)),
            "day": _to_int(_first(station, ("Day", "day", "JourneyDay"), 1), 1),
            "sequence": _to_int(_first(station, ("SNo", "serialNumber", "seqNo", "order", "Sr", "SrWTT"), idx), idx),
            "source": "ntes",
        })

    return sorted(normalized, key=lambda item: item.get("sequence", 0))


def _delay_to_minutes(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"rt", "right time", "on time", "ontime", "no delay"}:
        return 0
    m = re.search(r"(-?\d+)", text)
    return int(m.group(1)) if m else None


def normalize_live_status(raw: Any) -> dict:
    """Normalize NTES ShowFullRunJson responses to the current live-status shape."""
    if not isinstance(raw, dict):
        return {}

    current_code = _clean_code(_first(raw, (
        "CurrentStation", "currentStation", "CurrentStationCode", "currentStationCode",
        "LastReportedStation", "lastStationCode", "LSTN",
    )))
    current_name = _clean_name(_first(raw, (
        "CurrentStationName", "currentStationName", "LastReportedStationName",
        "currentStation", "CurrentStation", "LSTNN", "LSTNNH",
    )))
    next_code = _clean_code(_first(raw, (
        "NextStationCode", "nextStationCode", "NextStnCode", "NPSTN", "NSTN",
    )))
    next_name = _clean_name(_first(raw, (
        "NextStationName", "nextStationName", "NextStnName", "NPSTNN", "NSTNN", "NPSTNNH", "NSTNNH",
    )))

    last_update = _clean_name(_first(raw, (
        "LastUpdate", "lastUpdate", "LastUpdated", "trainPosition", "TrainPosition",
        "Status", "LUPDFULL", "TRUNST", "AlertMsg",
    )))
    delay_mins = _delay_to_minutes(_first(raw, (
        "DelayDep", "delayDep", "DelayArr", "delayArr", "Delay", "delay",
        "ExpectedDelay", "LDEL",
    )))

    per_station = []
    rows = (
        raw.get("StationList")
        or raw.get("stationList")
        or raw.get("stations")
        or raw.get("vStationList")
        or raw.get("TrainRoute")
        or raw.get("STNS")
        or []
    )
    for row in _as_list(rows):
        if not isinstance(row, dict):
            continue
        code = _clean_code(_first(row, (
            "StnCode", "StationCode", "stationCode", "code", "station", "SC",
        )))
        if not code:
            continue
        status = str(_first(row, ("Status", "status", "trainStatus"), "")).lower()
        if not status:
            if row.get("ISD") or row.get("ISA"):
                status = "passed"
            elif row.get("ETA") or row.get("ETD"):
                status = "expected"
            else:
                status = "unknown"
        arr_delay = _first(row, ("DelayArr", "ArrDelay", "delayArr", "arr_delay", "DARR"), "")
        dep_delay = _first(row, ("DelayDep", "DepDelay", "delayDep", "dep_delay", "DDEP", "DF"), "")
        per_station.append({
            "code": code,
            "name": _clean_name(_first(row, ("StnName", "StationName", "name", "SN", "SHN"), code)),
            "status": status,
            "actual_arr": _first(row, ("ETA", "ActualArr", "ActualArrival", "actual_arr", "STA"), ""),
            "actual_dep": _first(row, ("ETD", "ActualDep", "ActualDeparture", "actual_dep", "STD"), ""),
            "arr_delay": arr_delay,
            "dep_delay": dep_delay,
            "platform": _first(row, ("Platform", "PF", "platform"), ""),
        })

    if delay_mins is None:
        for row in reversed(per_station):
            delay_mins = _delay_to_minutes(row.get("dep_delay") or row.get("arr_delay"))
            if delay_mins is not None:
                break

    return {
        "source": "ntes",
        "live_text": last_update,
        "last_station": current_name or current_code,
        "last_station_code": current_code,
        "next_stoppage": next_name or next_code,
        "next_station_code": next_code,
        "delay_mins": delay_mins,
        "delay_status": "unknown" if delay_mins is None else ("on_time" if delay_mins == 0 else "late"),
        "per_station_delays": per_station,
        "platform": _first(raw, ("Platform", "PF", "platform", "CPOS"), ""),
        "train_status": _first(raw, ("trainStatus", "TrainStatus", "Status", "TRUNST"), ""),
        "updated_at": _clean_name(_first(raw, ("LastUpdated", "LastUpdateTime", "UpdatedAt", "LUPDT", "LASTUPD"), "")),
        "raw": raw,
    }


def normalize_station_live(raw: Any) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    trains = raw.get("TrainsAtStation") or raw.get("trainsAtStation") or raw.get("trains") or []
    out = []
    for train in _as_list(trains):
        if not isinstance(train, dict):
            continue
        train_no = _clean_code(_first(train, ("TrainNumber", "TrainNo", "trainNo", "num")))
        if not train_no:
            continue
        out.append({
            "train_no": train_no,
            "name": _clean_name(_first(train, ("TrainName", "trainName", "name"), train_no)),
            "eta": _first(train, ("ETA", "eta", "ArrTime"), ""),
            "etd": _first(train, ("ETD", "etd", "DepTime"), ""),
            "platform": _first(train, ("Platform", "PF", "platform"), ""),
            "arr_delay": _first(train, ("DelayArr", "delayArr"), ""),
            "dep_delay": _first(train, ("DelayDep", "delayDep"), ""),
            "source": _clean_code(_first(train, ("Source", "source"), "")),
            "source_name": _clean_name(_first(train, ("SourceName", "sourceName"), "")),
            "destination": _clean_code(_first(train, ("Destination", "destination"), "")),
            "destination_name": _clean_name(_first(train, ("DestinationName", "destinationName"), "")),
            "train_type": _first(train, ("TrainType", "trainType"), ""),
            "train_type_desc": _first(train, ("TrainTypeDesc", "trainTypeDesc"), ""),
            "source_api": "ntes_station_live",
            "raw": train,
        })
    return out


def normalize_station_timetable(raw: Any) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    trains = raw.get("vTrains") or raw.get("trains") or raw.get("Trains") or []
    out = []
    for train in _as_list(trains):
        if not isinstance(train, dict):
            continue
        train_no = _clean_code(_first(train, ("TrainNumber", "TrainNo", "trainNo", "num")))
        if not train_no:
            continue
        out.append({
            "train_no": train_no,
            "name": _clean_name(_first(train, ("TrainName", "trainName", "name"), train_no)),
            "arrival_time": _first(train, ("STA", "ArrTime", "arrival_time"), ""),
            "departure_time": _first(train, ("STD", "DepTime", "departure_time"), ""),
            "platform": _first(train, ("Platform", "PF", "platform"), ""),
            "source": _clean_code(_first(train, ("Source", "source"), "")),
            "source_name": _clean_name(_first(train, ("SourceName", "sourceName"), "")),
            "destination": _clean_code(_first(train, ("Destination", "destination"), "")),
            "destination_name": _clean_name(_first(train, ("DestinationName", "destinationName"), "")),
            "running_days_str": _first(train, ("DaysOfDep", "DaysOfRun", "RunsOn"), ""),
            "train_type": _first(train, ("TrainType", "trainType"), ""),
            "train_type_desc": _first(train, ("TrainTypeDesc", "trainTypeDesc"), ""),
            "arrival_cancelled": bool(train.get("ArrCancel")),
            "departure_cancelled": bool(train.get("DepCancel")),
            "source_api": "ntes_station_timetable",
            "raw": train,
        })
    return out


def normalize_trains_between(raw: Any) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    trains = raw.get("Trains") or raw.get("trains") or raw.get("TrainList") or []
    out = []
    for train in _as_list(trains):
        if not isinstance(train, dict):
            continue
        train_no = _clean_code(_first(train, ("TrainNumber", "TrainNo", "trainNo", "num")))
        if not train_no:
            continue
        out.append({
            "train_no": train_no,
            "name": _clean_name(_first(train, ("TrainName", "trainName", "name"), train_no)),
            "from_station": _clean_code(_first(train, ("FromStation", "fromStation", "from_station"), "")),
            "from_station_name": _clean_name(_first(train, ("FromStationName", "fromStationName", "from_station_name"), "")),
            "to_station": _clean_code(_first(train, ("toStation", "ToStation", "to_station"), "")),
            "to_station_name": _clean_name(_first(train, ("ToStationName", "toStationName", "to_station_name"), "")),
            "source": _clean_code(_first(train, ("Source", "source"), "")),
            "source_name": _clean_name(_first(train, ("SourceName", "sourceName"), "")),
            "destination": _clean_code(_first(train, ("Destination", "destination"), "")),
            "destination_name": _clean_name(_first(train, ("DestinationName", "destinationName"), "")),
            "departure_time": _first(train, ("DepTime", "DepTimeFrom", "STD", "departure_time", "st", "FromStnDep"), ""),
            "arrival_time": _first(train, ("ArrTime", "ArrTimeTo", "STA", "arrival_time", "dt", "ToStnArr"), ""),
            "travel_time": _first(train, ("TravelTime", "Duration", "travel_time", "tt"), ""),
            "running_days_str": _first(train, ("RunsOn", "DaysOfRun", "running_days_str", "dy"), ""),
            "class_of_travel": _first(train, ("ClassOfTravel", "classOfTravel", "classes"), ""),
            "train_type": _first(train, ("TrainType", "trainType"), ""),
            "train_type_desc": _clean_name(_first(train, ("TrainTypeDesc", "trainTypeDesc"), "")),
            "source_api": "ntes_trains_between",
            "raw": train,
        })
    return out


class NTESClient:
    BASE_URL = "https://enquiry.indianrail.gov.in/crisns/AppServAnd"

    def __init__(self, timeout: int = 10, retries: int = 2, cache_ttl: int = 60) -> None:
        self.timeout = timeout
        self.retries = retries
        self.cache_ttl = cache_ttl
        self.crypto = NTESCrypto()
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "charset": "utf-8",
            "User-Agent": "Dalvik/2.1.0 (Linux; Android 11)",
        })
        try:
            from cache_policy import TTLCache
            self._cache = TTLCache(default_ttl=cache_ttl)
        except ImportError:
            self._cache = None
        self._fallback_cache: dict[str, tuple[float, Any]] = {}

    def _payload(self, sub_service: str, **params: Any) -> str:
        pairs = {"service": "TrainRunningMob", "subService": sub_service}
        pairs.update({k: v for k, v in params.items() if v is not None})
        return urlencode(pairs)

    def request_payload(self, payload: str, *, skip_cache: bool = False) -> Any:
        try:
            from upstreams import registry, STATE_DOWN, TIER_OFFICIAL_LIVE, TIER_OFFICIAL_STATIC_CACHE
            node = registry.get("ntes_appservand")
            if node.get_state() == STATE_DOWN:
                raise NTESError("Circuit breaker open for NTES AppServAnd")
        except ImportError:
            node = None
            TIER_OFFICIAL_LIVE = "official_live"
            TIER_OFFICIAL_STATIC_CACHE = "official_static_cache"

        if not skip_cache:
            if self._cache:
                cached_val, fetched_time, is_stale = self._cache.get(payload, max_stale_ttl=86400)
                if cached_val is not None:
                    from datetime import datetime
                    import copy
                    res = copy.deepcopy(cached_val)
                    if isinstance(res, dict):
                        res["_source_metadata"] = {
                            "source_tier": TIER_OFFICIAL_STATIC_CACHE if is_stale else TIER_OFFICIAL_LIVE,
                            "source_name": "NTES AppServAnd",
                            "fetched_at_ist": datetime.fromtimestamp(fetched_time).strftime("%Y-%m-%d %H:%M:%S IST"),
                            "is_stale": is_stale,
                            "fallback_used": False,
                            "confidence": "high",
                        }
                    return res
            else:
                cached = self._fallback_cache.get(payload)
                if cached and time.time() - cached[0] < self.cache_ttl:
                    return cached[1]

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.post(
                    self.BASE_URL,
                    json={"jsonIn": self.crypto.build(payload)},
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    raise NTESError(f"NTES returned HTTP {response.status_code}")
                if not response.text.strip():
                    raise NTESError("NTES returned an empty response")

                try:
                    data = response.json()
                except ValueError as exc:
                    raise NTESError("NTES returned invalid JSON") from exc

                decoded = self.crypto.decode(data["jsonIn"]) if isinstance(data, dict) and data.get("jsonIn") else data
                error = self._extract_error(decoded)
                if error:
                    raise NTESError(error)

                if self._cache:
                    self._cache.set(payload, decoded)
                else:
                    self._fallback_cache[payload] = (time.time(), decoded)

                if node: node.record_success()

                # Attach live metadata
                from datetime import datetime
                import copy
                res = copy.deepcopy(decoded)
                if isinstance(res, dict):
                    res["_source_metadata"] = {
                        "source_tier": TIER_OFFICIAL_LIVE,
                        "source_name": "NTES AppServAnd",
                        "fetched_at_ist": datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S IST"),
                        "is_stale": False,
                        "fallback_used": False,
                        "confidence": "high",
                    }
                return res
            except (requests.RequestException, NTESError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 4))

        if node: node.record_failure()
        raise NTESError(f"NTES request failed: {last_error}")

    def request(self, sub_service: str, *, skip_cache: bool = False, **params: Any) -> Any:
        return self.request_payload(self._payload(sub_service, **params), skip_cache=skip_cache)

    @staticmethod
    def _extract_error(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        alert = str(_first(data, ("AlertMsg", "alertMsg", "AlertMsgHindi", "alertMsgHindi"), "")).strip()
        if alert.lower().startswith("no exceptional details"):
            return ""
        return alert

    def search(self, query: str) -> Any:
        return self.request("FindTrainJson", trainNo=query)

    def train_info(self, train_no: str) -> Any:
        return self.request("GetTrainInstance", trainNo=train_no)

    def schedule(self, train_no: str, start_date: str = "") -> Any:
        return self.request("GetTrainSchedule", trainNo=train_no, startDate=format_ntes_date(start_date))

    def station_live(self, station_code: str, hours: int = 2, to_station: str = "") -> Any:
        return self.request(
            "TrainsAtStationJson",
            jStation=station_code.upper(),
            nHr=hours,
            jToStation=to_station.upper(),
        )

    def station_timetable(self, station_code: str, train_type: str = "XXX") -> Any:
        station_code = station_code.upper()
        return self.request(
            "GetStationTimeTable",
            jStation=station_code,
            stnCode=station_code,
            stationCode=station_code,
            trainType=train_type,
        )

    def trains_between(self, from_station: str, to_station: str, train_type: str = "XXX") -> Any:
        return self.request(
            "TrainBtwStnJson",
            stnFrom=from_station.upper(),
            stnTo=to_station.upper(),
            trainType=train_type,
        )

    def live_status(self, train_no: str, start_date: str) -> Any:
        return self.request(
            "ShowFullRunJson",
            trainNo=train_no,
            startDate=format_ntes_date(start_date),
            skip_cache=True,
        )

    def exceptions(self, train_no: str) -> Any:
        return self.request("TrainExcpInfo", trainNo=train_no)

    def get_train_route_normalized(self, train_no: str, start_date: str = "") -> list[dict]:
        return normalize_schedule(self.schedule(train_no, start_date))


_CLIENT: Optional[NTESClient] = None


def get_client() -> NTESClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = NTESClient()
    return _CLIENT
