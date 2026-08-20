"""Minimal in-process metrics — no external collector dependency.

Counters and a latency histogram are accumulated per process and rendered
as Prometheus text format on ``GET /metrics``.  With multiple workers each
process exposes its own numbers (scrape them per worker or front with a
summing collector when that matters).
"""

import threading
import uuid

_LOCK = threading.Lock()

_counters: dict[str, dict[str, float]] = {}
_histogram_seconds: float = 0.0
_histogram_count: int = 0


def new_request_id() -> str:
    return uuid.uuid4().hex


def _bucket(name: str, labels: dict[str, str], value: float = 1.0) -> None:
    with _LOCK:
        _counters.setdefault(name, {}).setdefault(_key(labels), 0.0)
        _counters[name][_key(labels)] += value


_KEY_SEP = "\x00"


def _key(labels: dict[str, str]) -> str:
    return _KEY_SEP.join(f"{k}={v}" for k, v in sorted(labels.items()))


def inc_requests(status_class: str) -> None:
    _bucket("http_requests_total", {"status_class": status_class})


def inc(name: str, **labels: str) -> None:
    _bucket(name, labels)


def observe_request(duration_seconds: float) -> None:
    global _histogram_seconds, _histogram_count
    with _LOCK:
        _histogram_seconds += duration_seconds
        _histogram_count += 1


Buckets = tuple[tuple[str, float], ...]
# le label is in milliseconds
_LATENCY_BUCKETS: Buckets = (
    ("5", 5), ("10", 10), ("25", 25), ("50", 50),
    ("100", 100), ("250", 250), ("500", 500), ("1000", 1000),
    ("2500", 2500), ("5000", 5000), ("10000", 10000), ("30000", 30000),
)
# thresholds (ms) kept in lockstep with the le labels above
_bucket_values: list[float] = [ms for _, ms in _LATENCY_BUCKETS]
_latency_bucket_hits: list[int] = [0] * len(_bucket_values)


def observe_latency_ms(duration_ms: float) -> None:
    """Feed the fixed-bucket histogram (each sample lands in exactly one
    bucket; exposition renders them cumulatively)."""
    with _LOCK:
        for i, threshold in enumerate(_bucket_values):
            if duration_ms <= threshold:
                _latency_bucket_hits[i] += 1
                break


def render_prometheus() -> str:
    """Render all metrics in Prometheus exposition text format."""
    lines = [
        "# HELP http_requests_total Total HTTP requests by status class.",
        "# TYPE http_requests_total counter",
    ]
    with _LOCK:
        for name, _series in sorted(_counters.items()):
            if name == "http_requests_total":
                continue
            lines.append(f"# HELP {name} {name}")
            lines.append(f"# TYPE {name} counter")
        for k, v in sorted(_counters.get("http_requests_total", {}).items()):
            lines.append(f"http_requests_total{_suffix(_unkey(k))} {v:g}")
        for name, series in sorted(_counters.items()):
            if name == "http_requests_total":
                continue
            for k, v in sorted(series.items()):
                lines.append(f"{name}{_suffix(_unkey(k))} {v:g}")

        lines += [
            "# HELP http_request_duration_seconds Cumulative request time.",
            "# TYPE http_request_duration_seconds summary",
            f"http_request_duration_seconds_sum {_histogram_seconds:.6f}",
            f"http_request_duration_seconds_count {_histogram_count}",
        ]
        lines.append(
            "# HELP http_request_duration_ms_requests_total "
            "Request latency histogram (milliseconds, cumulative buckets)."
        )
        lines.append(
            "# TYPE http_request_duration_ms_requests_total histogram"
        )
        total = 0
        for (name, _ms), hits in zip(_LATENCY_BUCKETS, _latency_bucket_hits, strict=True):
            total += hits
            lines.append(
                f"http_request_duration_ms_requests_bucket{{le=\"{name}\"}} {total}"
            )
        lines.append(f"http_request_duration_ms_requests_bucket{{le=\"+Inf\"}} {total}")
        lines.append(
            f"http_request_duration_ms_requests_total {total}"
        )
    return "\n".join(lines) + "\n"


def _suffix(labels_str: str) -> str:
    return f"{{{labels_str}}}" if labels_str else ""


def _unkey(k: str) -> str:
    parts = k.split(_KEY_SEP)
    return ", ".join(
        f'{p.split("=", 1)[0]}="{p.split("=", 1)[1]}"' if "=" in p else p
        for p in parts
    )


def reset() -> None:
    """Clear all metrics (tests only)."""
    global _histogram_seconds, _histogram_count
    with _LOCK:
        _counters.clear()
        _histogram_seconds = 0.0
        _histogram_count = 0.0
        for i in range(len(_latency_bucket_hits)):
            _latency_bucket_hits[i] = 0


def status_class_of(status_code: int) -> str:
    return f"{status_code // 100}xx"
