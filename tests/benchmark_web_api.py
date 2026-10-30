#!/usr/bin/env python3
"""Web & API Latency and Cache Performance Benchmark.

Measures:
- Latency (TTFB / response time) for primary APIs and Web pages:
  - APIs: /api/status, /api/health, /api/events, /api/weekly-report, /api/concierge
  - Pages: /, /oriental-lounge, /columns, /concierge
- Cache efficacy (Cold/Miss vs Warm/Hit) for in-memory LRU caches
- Edge cache headers verification (Cache-Control: s-maxage=15, stale-while-revalidate=45)
"""

import os
import sys
import time
import json
import statistics

# Ensure project root is in path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Enable edge cache header for verification
os.environ["ENABLE_PUBLIC_EDGE_CACHE"] = "1"
os.environ["SECRET_KEY"] = "benchmark-secret-key-test"
os.environ["STAFF_PASSCODE"] = "bench-staff"
os.environ["MASTER_PASSCODE"] = "bench-master"

import app as app_module

TARGET_ENDPOINTS = [
    # APIs
    {"name": "/api/status", "type": "api", "path": "/api/status"},
    {"name": "/api/health", "type": "api", "path": "/api/health"},
    {"name": "/api/events", "type": "api", "path": "/api/events"},
    {"name": "/api/weekly-report", "type": "api", "path": "/api/weekly-report"},
    {"name": "/api/concierge", "type": "api", "path": "/api/concierge?area=shinjuku&audience=men"},
    # Pages
    {"name": "/", "type": "page", "path": "/"},
    {"name": "/oriental-lounge", "type": "page", "path": "/oriental-lounge"},
    {"name": "/columns", "type": "page", "path": "/columns"},
    {"name": "/concierge", "type": "page", "path": "/concierge?area=shinjuku&audience=men"},
]


def measure_endpoint(client, path, iterations=30):
    # 1. Cold / Miss request
    t0 = time.perf_counter()
    resp_cold = client.get(path)
    t1 = time.perf_counter()
    cold_ms = (t1 - t0) * 1000.0

    status_code = resp_cold.status_code
    content_length = len(resp_cold.data)
    cache_control = resp_cold.headers.get("Cache-Control", "")
    content_type = resp_cold.headers.get("Content-Type", "")

    # 2. Warm / Cached requests
    warm_times = []
    for _ in range(iterations):
        t_start = time.perf_counter()
        r = client.get(path)
        t_end = time.perf_counter()
        warm_times.append((t_end - t_start) * 1000.0)

    mean_warm = statistics.mean(warm_times)
    median_warm = statistics.median(warm_times)
    p95_warm = sorted(warm_times)[int(len(warm_times) * 0.95)]
    min_warm = min(warm_times)
    max_warm = max(warm_times)

    speedup = ((cold_ms - mean_warm) / cold_ms * 100.0) if cold_ms > 0 else 0

    return {
        "status_code": status_code,
        "content_length_bytes": content_length,
        "content_type": content_type,
        "cache_control": cache_control,
        "cold_ms": cold_ms,
        "warm_mean_ms": mean_warm,
        "warm_median_ms": median_warm,
        "warm_p95_ms": p95_warm,
        "warm_min_ms": min_warm,
        "warm_max_ms": max_warm,
        "cache_speedup_percent": speedup
    }


def run_benchmarks():
    flask_app = app_module.app
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()

    print("=" * 80)
    print("🚀 RUNNING WEB & API RESPONSE BENCHMARKS")
    print("=" * 80)

    results = {}
    for target in TARGET_ENDPOINTS:
        name = target["name"]
        path = target["path"]
        print(f"Benchmarking {name} ({path})...")
        metrics = measure_endpoint(client, path, iterations=50)
        results[name] = metrics

    print("\n" + "=" * 92)
    print("📊 API & WEB LATENCY BENCHMARK RESULTS")
    print("=" * 92)
    print(f"| {'Endpoint':<22} | {'Type':<5} | {'Cold (Miss)':<11} | {'Warm (Mean)':<11} | {'p95':<8} | {'Speedup':<8} | {'Bytes':<9} |")
    print("|" + "-" * 24 + "|" + "-" * 7 + "|" + "-" * 13 + "|" + "-" * 13 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 11 + "|")

    for target in TARGET_ENDPOINTS:
        name = target["name"]
        t_type = target["type"]
        m = results[name]
        print(f"| {name:<22} | {t_type:<5} | {m['cold_ms']:>8.2f} ms | {m['warm_mean_ms']:>8.2f} ms | {m['warm_p95_ms']:>5.2f} ms | {m['cache_speedup_percent']:>6.1f}% | {m['content_length_bytes']:>7,d} B |")
    print("=" * 92)

    # Save to file
    with open(os.path.join(ROOT, "benchmark_web_api_result.json"), "w") as f:
        json.dump(results, f, indent=2)

    return results


if __name__ == "__main__":
    run_benchmarks()
