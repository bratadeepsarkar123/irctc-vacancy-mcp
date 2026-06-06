import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import chart_worker


class FakeRequest:
    def __init__(self, host="203.0.113.10", headers=None):
        self.client = SimpleNamespace(host=host)
        self.headers = headers or {}


def test_worker_secret_required_for_non_local_access(monkeypatch):
    monkeypatch.delenv("IRCTC_WORKER_SECRET", raising=False)
    monkeypatch.delenv("IRCTC_WORKER_ALLOW_INSECURE", raising=False)

    with pytest.raises(HTTPException) as exc:
        chart_worker._verify_secret(FakeRequest())

    assert exc.value.status_code == 401
    assert "IRCTC_WORKER_SECRET" in exc.value.detail


def test_worker_allows_local_dev_without_secret(monkeypatch):
    monkeypatch.delenv("IRCTC_WORKER_SECRET", raising=False)
    monkeypatch.delenv("IRCTC_WORKER_ALLOW_INSECURE", raising=False)

    chart_worker._verify_secret(FakeRequest(host="127.0.0.1"))
    chart_worker._verify_secret(FakeRequest(host="testclient"))


def test_worker_secret_header_must_match(monkeypatch):
    monkeypatch.setenv("IRCTC_WORKER_SECRET", "expected-secret")

    with pytest.raises(HTTPException) as exc:
        chart_worker._verify_secret(FakeRequest(headers={"X-Worker-Secret": "wrong"}))

    assert exc.value.status_code == 401
    chart_worker._verify_secret(FakeRequest(headers={"X-Worker-Secret": "expected-secret"}))


def test_worker_train_composition_disables_recursive_worker_url(monkeypatch):
    import irctc_api

    monkeypatch.setenv("IRCTC_WORKER_URL", "https://worker.example")
    monkeypatch.delenv("IRCTC_WORKER_SECRET", raising=False)

    def fake_train_composition(train_no, jdate, boarding):
        return {
            "trainNo": train_no,
            "jDate": jdate,
            "boardingStation": boarding,
            "worker_url_visible_during_call": "IRCTC_WORKER_URL" in os.environ,
        }

    monkeypatch.setattr(irctc_api, "train_composition", fake_train_composition)

    response = TestClient(chart_worker.app).post(
        "/chart/trainComposition",
        json={"trainNo": "12004", "jDate": "2026-06-06", "boardingStation": "NDLS"},
    )

    assert response.status_code == 200
    assert response.json()["worker_url_visible_during_call"] is False
    assert os.environ["IRCTC_WORKER_URL"] == "https://worker.example"
