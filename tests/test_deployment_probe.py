import os
import sys
import importlib
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

deployment_probe = importlib.import_module("deployment_probe")


def test_classify_probe_failure_identifies_waf_and_timeout():
    assert deployment_probe.classify_probe_failure(status_code=403) == "blocked_or_auth"
    assert deployment_probe.classify_probe_failure("Akamai challenge page") == "blocked_or_auth"
    assert deployment_probe.classify_probe_failure("Operation timed out after 10000ms") == "timeout"
    assert deployment_probe.classify_probe_failure("unexpected json shape") == "schema_drift"


def test_probe_http_json_ok_and_schema_drift():
    ok_response = SimpleNamespace(
        status_code=200,
        text='{"ok": true}',
        json=lambda: {"ok": True},
    )
    bad_response = SimpleNamespace(
        status_code=200,
        text='{"unexpected": true}',
        json=lambda: {"unexpected": True},
    )

    ok = deployment_probe.probe_http_json("unit", lambda: ok_response, lambda payload: payload.get("ok") is True)
    bad = deployment_probe.probe_http_json("unit", lambda: bad_response, lambda payload: payload.get("ok") is True)

    assert ok["ok"] is True
    assert ok["status"] == "ok"
    assert bad["ok"] is False
    assert bad["status"] == "schema_drift"


def test_run_readiness_probe_summarizes_blockers(monkeypatch):
    monkeypatch.setattr(deployment_probe, "probe_ntes", lambda: {"name": "ntes", "ok": True, "status": "ok"})
    monkeypatch.setattr(deployment_probe, "probe_irctc_schedule", lambda: {"name": "schedule", "ok": True, "status": "ok"})
    monkeypatch.setattr(
        deployment_probe,
        "probe_irctc_charts_landing_text",
        lambda: {"name": "charts", "ok": False, "status": "blocked_or_auth"},
    )

    result = deployment_probe.run_readiness_probe()

    assert result["ok"] is False
    assert result["summary"] == "candidate_host_not_ready"
    assert "browser/proxy fallback" in result["recommendation"]


def test_optional_irctc_schedule_does_not_block_host(monkeypatch):
    monkeypatch.setattr(deployment_probe, "probe_ntes", lambda: {"name": "ntes", "ok": True, "status": "ok", "required": True})
    monkeypatch.setattr(
        deployment_probe,
        "probe_irctc_schedule",
        lambda: {"name": "schedule", "ok": False, "status": "schema_drift", "required": False},
    )
    monkeypatch.setattr(
        deployment_probe,
        "probe_irctc_charts_landing_text",
        lambda: {"name": "charts", "ok": True, "status": "ok", "required": True},
    )

    result = deployment_probe.run_readiness_probe()

    assert result["ok"] is True
    assert result["summary"] == "candidate_host_ready"
    skipped = [check for check in result["checks"] if check["status"] == "skipped"]
    assert skipped[0]["required"] is False
