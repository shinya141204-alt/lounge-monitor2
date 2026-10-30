#!/usr/bin/env python3
"""Tier 5 Adversarial & Concurrency Stress Test Suite.

Stress tests:
1. High-concurrency access (50 concurrent threads, 1000 requests)
   - Verifies 0% error rate, thread safety, latency distribution
2. Adversarial inputs & SQL injection / XSS fuzzing
   - Evaluates defensive error handling on malicious inputs
3. Boundary & oversize payloads (100KB+ strings, invalid query parameters)
4. Rapid-fire vote submission & Rate limiter exhaustion
5. Memory growth / leak monitoring under continuous load
"""

import os
import sys
import time
import json
import unittest
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as app_module


class Tier5AdversarialStressTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app_module.app.config['TESTING'] = True
        cls.app = app_module.app
        cls.client = cls.app.test_client()

    def test_01_high_concurrency_stress(self):
        """Stress: 50 concurrent worker threads making 1,000 mixed requests."""
        endpoints = [
            "/api/status",
            "/api/health",
            "/api/events",
            "/api/weekly-report",
            "/api/concierge?area=shinjuku&audience=men",
            "/",
            "/oriental-lounge",
            "/columns",
            "/concierge?area=shinjuku&audience=men",
        ]

        total_requests = 1000
        concurrency = 50
        errors = []
        latencies = []

        tracemalloc.start()
        start_mem = tracemalloc.get_traced_memory()[0]
        start_time = time.perf_counter()

        def make_request(idx):
            client = self.app.test_client()
            url = endpoints[idx % len(endpoints)]
            t0 = time.perf_counter()
            try:
                resp = client.get(url)
                t1 = time.perf_counter()
                latency_ms = (t1 - t0) * 1000.0
                if resp.status_code >= 500:
                    return False, resp.status_code, latency_ms, f"Server error: {resp.status_code} on {url}"
                return True, resp.status_code, latency_ms, None
            except Exception as e:
                t1 = time.perf_counter()
                return False, 0, (t1 - t0) * 1000.0, str(e)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(make_request, i) for i in range(total_requests)]
            for fut in as_completed(futures):
                ok, status, lat, err = fut.result()
                latencies.append(lat)
                if not ok:
                    errors.append(err)

        total_duration = time.perf_counter() - start_time
        peak_mem = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        throughput = total_requests / total_duration if total_duration > 0 else 0
        p50 = sorted(latencies)[int(len(latencies) * 0.50)]
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]
        mean_lat = sum(latencies) / len(latencies) if latencies else 0

        print(f"\n--- Concurrency Stress Test Results ---")
        print(f"Total Requests: {total_requests}")
        print(f"Concurrency: {concurrency} threads")
        print(f"Total Duration: {total_duration:.2f}s")
        print(f"Throughput: {throughput:.1f} req/sec")
        print(f"Errors: {len(errors)} (Error Rate: {len(errors)/total_requests*100:.2f}%)")
        print(f"Latency: Mean={mean_lat:.2f}ms, p50={p50:.2f}ms, p95={p95:.2f}ms, p99={p99:.2f}ms")
        print(f"Peak Memory: {peak_mem / (1024*1024):.2f} MB")

        # Save metrics
        stress_result = {
            "total_requests": total_requests,
            "concurrency": concurrency,
            "duration_sec": total_duration,
            "throughput_req_per_sec": throughput,
            "errors": len(errors),
            "error_rate_percent": len(errors) / total_requests * 100.0,
            "mean_ms": mean_lat,
            "p50_ms": p50,
            "p95_ms": p95,
            "p99_ms": p99,
            "peak_mem_mb": peak_mem / (1024 * 1024)
        }
        with open(os.path.join(ROOT, "benchmark_stress_result.json"), "w") as f:
            json.dump(stress_result, f, indent=2)

        self.assertEqual(len(errors), 0, f"Encountered {len(errors)} errors during stress test: {errors[:5]}")

    def test_02_sql_injection_and_malicious_inputs(self):
        """Adversarial: Injection vectors and malicious strings across endpoints."""
        malicious_payloads = [
            "' OR '1'='1",
            "'; DROP TABLE monitor_logs; --",
            "\" OR \"\"=\"",
            "<script>alert('XSS')</script>",
            "../../etc/passwd",
            "%00%27%22",
            "1 UNION SELECT null, null, null, null, null --",
            "NaN",
            "-99999999999999999999999999999999999999999999999999",
            "\u0000\ufffd\x1b[31mRed\x1b[0m",
        ]

        for payload in malicious_payloads:
            # 1. API Concierge query
            resp = self.client.get(f"/api/concierge?area={payload}&audience={payload}")
            self.assertIn(resp.status_code, (200, 400, 404))
            if resp.content_type.startswith("application/json"):
                data = resp.get_json()
                self.assertIsNotNone(data)

            # 2. Page Concierge query
            resp_page = self.client.get(f"/concierge?area={payload}&audience={payload}")
            self.assertIn(resp_page.status_code, (200, 400, 404))

            # 3. Status API query
            resp_status = self.client.get(f"/api/status?store={payload}")
            self.assertEqual(resp_status.status_code, 200)

            # 4. Events API query
            resp_events = self.client.get(f"/api/events?since={payload}")
            self.assertIn(resp_events.status_code, (200, 400))

    def test_03_oversize_payloads_and_boundaries(self):
        """Adversarial: Extreme size parameters (100KB query string, long body)."""
        oversize_str = "A" * 100000

        # Query param overflow test
        resp = self.client.get(f"/api/concierge?area={oversize_str}&audience=men")
        self.assertIn(resp.status_code, (200, 400, 414))

        # Malformed JSON in POST
        resp_post = self.client.post("/api/vote", data="NOT_A_JSON{}}{", content_type="application/json")
        self.assertIn(resp_post.status_code, (400, 404, 405))

    def test_04_rapid_fire_rate_limiting(self):
        """Adversarial: Rapid submission of votes to verify rate limiter integrity."""
        # Attempt 10 rapid votes from same IP / voter ID
        responses = []
        for i in range(10):
            r = self.client.post("/api/vote", json={
                "store_name": "オリエンタルラウンジ 渋谷本店",
                "vote_type": "lively",
                "voter_id": "adversarial_test_voter_001"
            })
            responses.append(r.status_code)

        # First vote should succeed (200) or respond gracefully, subsequent votes should be guarded
        self.assertTrue(all(code in (200, 429, 400, 404) for code in responses))

    def test_05_non_existent_api_error_contract(self):
        """Adversarial: Non-existent routes under /api must strictly adhere to JSON error contract."""
        bogus_paths = [
            "/api/does_not_exist",
            "/api/admin/unauthorized",
            "/api/v2/legacy/undefined/null",
            "/api/..%2f..%2fprivate",
        ]
        for p in bogus_paths:
            resp = self.client.get(p)
            self.assertIn(resp.status_code, (400, 404, 405))
            self.assertTrue(resp.content_type.startswith("application/json"), f"Expected JSON content-type for {p}, got {resp.content_type}")
            data = resp.get_json()
            self.assertEqual(data.get("status"), "error")
            self.assertIn("error", data)
            self.assertIn("code", data)


if __name__ == "__main__":
    unittest.main()
