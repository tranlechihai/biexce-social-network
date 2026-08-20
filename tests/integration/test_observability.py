"""Integration tests for /health, /ready, /metrics and the request-ID
middleware (Increment 7 — production hardening)."""
import re

import pytest

from ting_ting import metrics as metrics_mod

pytestmark = pytest.mark.integration


@pytest.fixture()
def _fresh_metrics():
    metrics_mod.reset()
    yield
    metrics_mod.reset()


def test_health_returns_ok_with_request_id(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["X-Request-ID"])


def test_request_id_echoes_client_value(client):
    resp = client.get("/health", headers={"X-Request-ID": "trace-abc123"})
    assert resp.headers["X-Request-ID"] == "trace-abc123"


def test_ready_reports_healthy_db(client, _fresh_metrics):
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "database": "ok"}


def test_ready_reports_503_when_db_down(client, _fresh_metrics, monkeypatch):
    import ting_ting.main as main_mod

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(main_mod, "get_engine", _boom)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "database": "unavailable"}


def test_metrics_expose_request_counters(client, _fresh_metrics):
    # The /metrics request itself is counted only after the body renders,
    # so the snapshot shows exactly the two /health hits.
    client.get("/health")
    client.get("/health")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert 'http_requests_total{status_class="2xx"} 2' in body
    assert "http_request_duration_ms_requests_bucket{le=\"+Inf\"} 2" in body


def test_metrics_count_login_failures(client, _fresh_metrics):
    client.post("/api/auth/login", json={"identifier": "nobody", "password": "wrong"})
    body = client.get("/metrics").text
    assert "auth_login_failures_total 1" in body
    assert 'http_requests_total{status_class="4xx"} 1' in body


def test_metrics_count_404s(client, _fresh_metrics):
    client.get("/api/nonexistent")
    body = client.get("/metrics").text
    assert 'http_requests_total{status_class="4xx"} 1' in body
    assert 'http_requests_total{status_class="5xx"}' not in body


def test_high_volume_login_stays_observable(client, _fresh_metrics):
    # (Rate limiting is disabled in tests.) Every failed-login response must
    # carry a request id and the metrics must count it (middleware is
    # outermost, so even limiter short-circuits would be observed).
    for _ in range(20):
        resp = client.post(
            "/api/auth/login", json={"identifier": "nobody", "password": "x"},
        )
        assert resp.status_code == 401
        assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["X-Request-ID"])
    body = client.get("/metrics").text
    assert 'http_requests_total{status_class="4xx"} 20' in body
    assert "auth_login_failures_total 20" in body
