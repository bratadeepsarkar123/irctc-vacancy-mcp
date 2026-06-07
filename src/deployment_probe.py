"""
deployment_probe.py - Provider/IP readiness checks for production hosting.

Run this on a candidate host (Azure VM/App Service, GCloud, Render, VPS, etc.)
before deploying the app. It answers whether official NTES/IRCTC endpoints are
reachable from that host's public IP and whether failures look like WAF/auth,
timeouts, schema drift, or normal network errors.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Callable, Dict, Optional

try:
    from curl_cffi import requests as cffi_requests
    _HAVE_CFFI = True
except ImportError:  # pragma: no cover - exercised only without curl_cffi
    import requests as cffi_requests  # type: ignore[no-redef]
    _HAVE_CFFI = False


STATUS_OK = "ok"
STATUS_BLOCKED = "blocked_or_auth"
STATUS_TIMEOUT = "timeout"
STATUS_SCHEMA = "schema_drift"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


def sanitize_probe_error(error: str) -> str:
    text = str(error)
    if "header value" in text.lower() or "IRCTC_COOKIE" in text or "Cookie" in text:
        return "request failed while preparing sanitized headers"
    return text[:500]


def classify_probe_failure(error: str = "", status_code: Optional[int] = None, body: str = "") -> str:
    text = f"{error} {body}".lower()
    if status_code in (401, 403, 429):
        return STATUS_BLOCKED
    if "forbidden" in text or "unauthorized" in text or "akamai" in text or "captcha" in text:
        return STATUS_BLOCKED
    if "timed out" in text or "timeout" in text or "operation timed out" in text:
        return STATUS_TIMEOUT
    if "json" in text or "schema" in text or "unexpected" in text or "empty response" in text:
        return STATUS_SCHEMA
    return STATUS_ERROR


def _result(
    name: str,
    status: str,
    *,
    ok: bool = False,
    required: bool = True,
    latency_ms: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "status": status,
        "required": required,
        "latency_ms": latency_ms,
        "details": details or {},
    }


def _request(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: int = 15,
) -> Any:
    kwargs: Dict[str, Any] = {"headers": headers or {}, "timeout": timeout}
    if _HAVE_CFFI:
        kwargs["impersonate"] = "chrome124"
    if method.upper() == "POST":
        return cffi_requests.post(url, json=json_body, **kwargs)
    return cffi_requests.get(url, **kwargs)


def probe_http_json(
    name: str,
    request_fn: Callable[[], Any],
    validator: Callable[[Any], bool],
) -> Dict[str, Any]:
    started = time.time()
    try:
        response = request_fn()
        latency_ms = int((time.time() - started) * 1000)
        status_code = int(getattr(response, "status_code", 0))
        text = getattr(response, "text", "") or ""
        if status_code in (401, 403, 429):
            return _result(
                name,
                STATUS_BLOCKED,
                latency_ms=latency_ms,
                details={"status_code": status_code, "body_prefix": text[:160]},
            )
        if not (200 <= status_code < 300):
            return _result(
                name,
                STATUS_ERROR,
                latency_ms=latency_ms,
                details={"status_code": status_code, "body_prefix": text[:160]},
            )
        payload = response.json()
        if not validator(payload):
            return _result(
                name,
                STATUS_SCHEMA,
                latency_ms=latency_ms,
                details={"status_code": status_code, "body_prefix": text[:240]},
            )
        return _result(name, STATUS_OK, ok=True, latency_ms=latency_ms, details={"status_code": status_code})
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return _result(
            name,
            classify_probe_failure(str(exc)),
            latency_ms=latency_ms,
            details={"error": sanitize_probe_error(str(exc))},
        )


def probe_ntes() -> Dict[str, Any]:
    from ntes_api import get_client

    started = time.time()
    try:
        result = get_client().request("FindTrainJson", skip_cache=True, trainNo="12004")
        latency_ms = int((time.time() - started) * 1000)
        trains = result.get("Trains") or result.get("trains") or result.get("data") or []
        if not trains:
            return _result(
                "ntes_appservand_search",
                STATUS_SCHEMA,
                latency_ms=latency_ms,
                details={"keys": sorted(result.keys())[:20]},
            )
        return _result(
            "ntes_appservand_search",
            STATUS_OK,
            ok=True,
            latency_ms=latency_ms,
            details={"rows": len(trains)},
        )
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return _result(
            "ntes_appservand_search",
            classify_probe_failure(str(exc)),
            latency_ms=latency_ms,
            details={"error": sanitize_probe_error(str(exc))},
        )


def probe_irctc_schedule() -> Dict[str, Any]:
    from irctc_api import train_schedule

    started = time.time()
    try:
        rows = train_schedule("12004")
        latency_ms = int((time.time() - started) * 1000)
        if not rows:
            return _result(
                "irctc_schedule",
                STATUS_SCHEMA,
                required=False,
                latency_ms=latency_ms,
                details={"rows": 0, "note": "IRCTC schedule is useful but NTES schedule is the canonical production schedule source."},
            )
        return _result("irctc_schedule", STATUS_OK, ok=True, required=False, latency_ms=latency_ms, details={"rows": len(rows)})
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return _result(
            "irctc_schedule",
            classify_probe_failure(str(exc)),
            latency_ms=latency_ms,
            details={"error": sanitize_probe_error(str(exc))},
        )


def probe_irctc_charts_landing() -> Dict[str, Any]:
    from irctc_api import HEADERS

    return probe_http_json(
        "irctc_online_charts_landing",
        lambda: _request(
            "GET",
            "https://www.irctc.co.in/online-charts/",
            headers=HEADERS,
            timeout=15,
        ),
        lambda payload: isinstance(payload, dict),  # landing may not be JSON; see wrapper below
    )


def probe_irctc_charts_landing_text() -> Dict[str, Any]:
    from irctc_api import HEADERS

    started = time.time()
    try:
        response = _request(
            "GET",
            "https://www.irctc.co.in/online-charts/",
            headers=HEADERS,
            timeout=15,
        )
        latency_ms = int((time.time() - started) * 1000)
        status_code = int(getattr(response, "status_code", 0))
        text = getattr(response, "text", "") or ""
        if status_code in (401, 403, 429):
            return _result(
                "irctc_online_charts_landing",
                STATUS_BLOCKED,
                latency_ms=latency_ms,
                details={"status_code": status_code, "body_prefix": text[:160]},
            )
        if status_code != 200:
            return _result(
                "irctc_online_charts_landing",
                STATUS_ERROR,
                latency_ms=latency_ms,
                details={"status_code": status_code, "body_prefix": text[:160]},
            )
        if "online-charts" not in text.lower() and "irctc" not in text.lower():
            return _result(
                "irctc_online_charts_landing",
                STATUS_SCHEMA,
                latency_ms=latency_ms,
                details={"status_code": status_code, "body_prefix": text[:240]},
            )
        return _result(
            "irctc_online_charts_landing",
            STATUS_OK,
            ok=True,
            latency_ms=latency_ms,
            details={"status_code": status_code},
        )
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return _result(
            "irctc_online_charts_landing",
            classify_probe_failure(str(exc)),
            latency_ms=latency_ms,
            details={"error": sanitize_probe_error(str(exc))},
        )


def run_readiness_probe(include_chart_composition: bool = False) -> Dict[str, Any]:
    checks = [
        probe_ntes(),
        probe_irctc_schedule(),
        probe_irctc_charts_landing_text(),
    ]

    if include_chart_composition:
        from irctc_api import train_composition

        started = time.time()
        try:
            payload = train_composition(
                os.environ.get("IRCTC_MCP_PROBE_TRAIN", "12004"),
                os.environ.get("IRCTC_MCP_PROBE_DATE", "2026-06-06"),
                os.environ.get("IRCTC_MCP_PROBE_BOARDING", "NDLS"),
            )
            latency_ms = int((time.time() - started) * 1000)
            coaches = payload.get("cdd") or []
            checks.append(_result(
                "irctc_train_composition",
                STATUS_OK if coaches else STATUS_SCHEMA,
                ok=bool(coaches),
                latency_ms=latency_ms,
                details={"coaches": len(coaches)},
            ))
        except Exception as exc:
            latency_ms = int((time.time() - started) * 1000)
            checks.append(_result(
                "irctc_train_composition",
                classify_probe_failure(str(exc)),
                latency_ms=latency_ms,
                details={"error": sanitize_probe_error(str(exc))},
            ))
    else:
        checks.append(_result(
            "irctc_train_composition",
            STATUS_SKIPPED,
            required=False,
            details={"reason": "Set --include-chart-composition to test chart API with current cookie/session."},
        ))

    blockers = [
        c for c in checks
        if c.get("required", True) and c["status"] in {STATUS_BLOCKED, STATUS_TIMEOUT, STATUS_SCHEMA, STATUS_ERROR}
    ]
    return {
        "ok": not blockers,
        "summary": "candidate_host_ready" if not blockers else "candidate_host_not_ready",
        "checks": checks,
        "recommendation": (
            "This host can be considered for production."
            if not blockers
            else "Do not choose this host until blocked/failed checks are resolved or a browser/proxy fallback is proven."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe cloud/VPS readiness for IRCTC + NTES MCP hosting.")
    parser.add_argument(
        "--include-chart-composition",
        action="store_true",
        help="Also call IRCTC trainComposition using env IRCTC_COOKIE if available.",
    )
    args = parser.parse_args()
    result = run_readiness_probe(include_chart_composition=args.include_chart_composition)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
