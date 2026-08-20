"""Unit tests for the in-process metrics registry."""
import re

import pytest

from ting_ting import metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()
    yield
    metrics.reset()


def test_status_class_of():
    assert metrics.status_class_of(200) == "2xx"
    assert metrics.status_class_of(302) == "3xx"
    assert metrics.status_class_of(404) == "4xx"
    assert metrics.status_class_of(429) == "4xx"
    assert metrics.status_class_of(500) == "5xx"


def test_new_request_id_is_32_hex():
    rid = metrics.new_request_id()
    assert re.fullmatch(r"[0-9a-f]{32}", rid)


def test_request_counters_by_status_class():
    metrics.inc_requests("2xx")
    metrics.inc_requests("2xx")
    metrics.inc_requests("4xx")
    out = metrics.render_prometheus()
    assert 'http_requests_total{status_class="2xx"} 2' in out
    assert 'http_requests_total{status_class="4xx"} 1' in out


def test_named_counter_render():
    metrics.inc("auth_login_failures_total")
    out = metrics.render_prometheus()
    assert "auth_login_failures_total 1" in out
    assert "# TYPE auth_login_failures_total counter" in out


def test_latency_histogram_is_cumulative():
    metrics.observe_latency_ms(5)   # le=5
    metrics.observe_latency_ms(1500)  # le=2500
    out = metrics.render_prometheus()
    assert 'http_request_duration_ms_requests_bucket{le="5"} 1' in out
    assert 'http_request_duration_ms_requests_bucket{le="2500"} 2' in out
    assert 'http_request_duration_ms_requests_bucket{le="+Inf"} 2' in out


def test_summary_seconds_and_count():
    metrics.observe_request(0.25)
    out = metrics.render_prometheus()
    assert "http_request_duration_seconds_count 1" in out
    assert "http_request_duration_seconds_sum 0.250000" in out


def test_reset_clears_everything():
    metrics.inc_requests("2xx")
    metrics.observe_latency_ms(1)
    metrics.reset()
    out = metrics.render_prometheus()
    assert "http_requests_total{" not in out
    assert "http_request_duration_seconds_count 0" in out
