# -*- coding: utf-8 -*-
"""
Empirical Stress Test Suite for Milestone 2: Hashigo Intent Concurrency, Ring Buffer & Latency.
Role: Challenger 2 (Empirical Challenger)
Target: analytics.py, moat_intelligence.py
"""

import unittest
import time
import datetime
import threading
import concurrent.futures
import random
import tracemalloc
import sys
from collections import defaultdict

import analytics
import moat_intelligence


class TestHashigoIntentEmpiricalStress(unittest.TestCase):

    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_01_concurrent_access_and_thread_safety(self):
        """
        16並行スレッドから track と get_lead_demand_heat を同時に各250回（計4,000回）呼び出し、
        競合エラー・例外・デッドロックが0件であり、データの整合性が保持されることを実証。
        """
        errors = []
        areas = ["shibuya", "shinjuku", "umeda", "namba", "sakae", "fukuoka", "unknown_area"]
        audiences = ["men", "women"]
        party_sizes = ["solo", "pair", "group"]

        total_threads = 16
        iterations_per_thread = 250
        total_calls = total_threads * iterations_per_thread  # 4,000回

        def writer_worker(thread_id):
            try:
                for i in range(iterations_per_thread):
                    area = random.choice(areas)
                    aud = random.choice(audiences)
                    ps = random.choice(party_sizes)
                    lbl = f"{area}:{aud}:{ps}"
                    res = analytics.track("hashigo_intent", lbl)
                    if not res:
                        errors.append(f"Thread {thread_id} track returned False for valid label {lbl}")
            except Exception as e:
                errors.append(f"Writer thread {thread_id} raised: {type(e).__name__}: {e}")

        def reader_worker(thread_id):
            try:
                for i in range(iterations_per_thread):
                    area = random.choice(areas + [None])
                    hours = random.choice([1, 2, 3, None])
                    heat = analytics.get_lead_demand_heat(area=area, hours=hours)
                    # 戻り値の契約整合性チェック
                    if not isinstance(heat, dict):
                        errors.append(f"Reader thread {thread_id} received non-dict: {type(heat)}")
                        continue
                    tot = heat.get("total_intents", 0)
                    by_aud = heat.get("by_audience", {})
                    by_ps = heat.get("by_party_size", {})
                    # 監査: 性別合計とパーティーサイズ合計の一致性
                    aud_sum = by_aud.get("men", 0) + by_aud.get("women", 0)
                    ps_sum = by_ps.get("solo", 0) + by_ps.get("pair", 0) + by_ps.get("group", 0)
                    if tot != aud_sum:
                        errors.append(f"Reader thread {thread_id}: total_intents ({tot}) != aud_sum ({aud_sum})")
                    if tot != ps_sum:
                        errors.append(f"Reader thread {thread_id}: total_intents ({tot}) != ps_sum ({ps_sum})")
                    wr = heat.get("women_ratio_pct", 50)
                    if not (0 <= wr <= 100):
                        errors.append(f"Reader thread {thread_id}: invalid women_ratio_pct: {wr}")
            except Exception as e:
                errors.append(f"Reader thread {thread_id} raised: {type(e).__name__}: {e}")

        start_time = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=total_threads) as executor:
            futures = []
            for tid in range(total_threads):
                if tid % 2 == 0:
                    futures.append(executor.submit(writer_worker, tid))
                else:
                    futures.append(executor.submit(reader_worker, tid))
            
            # タイムアウト15秒以内の完了を検証（デッドロックの排除）
            for fut in concurrent.futures.as_completed(futures, timeout=15.0):
                fut.result()

        elapsed = time.monotonic() - start_time

        self.assertEqual(len(errors), 0, f"Concurrency errors detected ({len(errors)}): {errors[:5]}")
        print(f"\n[STRESS TEST 1] Concurrency Test Passed: {total_calls} calls across {total_threads} threads in {elapsed:.3f}s (0 errors)")

    def test_02_ring_buffer_bounds_and_rotation(self):
        """
        maxlen=2000 を超える 10,000 件の track を実行し、
        1) バッファ長が常に 2000 以下であること
        2) 古いデータが先頭から押し出され、最新の2000件が正しく保持されること
        3) メモリリークが発生しないこと
        を実証。
        """
        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()

        total_inserts = 10000
        now_base = datetime.datetime(2026, 11, 10, 20, 0, 0, tzinfo=analytics._JST)

        for i in range(total_inserts):
            t_offset = datetime.timedelta(seconds=i)
            cur_t = now_base + t_offset
            aud = "women" if i % 2 == 0 else "men"
            label = f"shibuya:{aud}:pair"
            analytics.track("hashigo_intent", label, _now_jst=cur_t)

            # 途中経過でのバッファ長チェック
            if i % 1000 == 0:
                with analytics._lock:
                    self.assertLessEqual(len(analytics._recent_hashigo_intents), 2000)

        # 完了後の検証
        with analytics._lock:
            buf_len = len(analytics._recent_hashigo_intents)
            self.assertEqual(buf_len, 2000, f"Ring buffer length should be exactly 2000, got {buf_len}")
            
            # 最古要素と最新要素のタイムスタンプ検証
            oldest_entry = analytics._recent_hashigo_intents[0]
            newest_entry = analytics._recent_hashigo_intents[-1]
            
            # 10,000件投入したので、最古は 8000件目 (i=8000), 最新は 9999件目 (i=9999)
            expected_oldest_ts = (now_base + datetime.timedelta(seconds=8000)).timestamp()
            expected_newest_ts = (now_base + datetime.timedelta(seconds=9999)).timestamp()
            
            self.assertAlmostEqual(oldest_entry[0], expected_oldest_ts, delta=1.0)
            self.assertAlmostEqual(newest_entry[0], expected_newest_ts, delta=1.0)

        snapshot_end = tracemalloc.take_snapshot()
        top_stats = snapshot_end.compare_to(snapshot_start, 'lineno')
        tracemalloc.stop()

        # メモリ増加量が極小（数MB未満）であることを確認
        total_diff_kb = sum(stat.size_diff for stat in top_stats) / 1024.0
        print(f"\n[STRESS TEST 2] Ring Buffer Rotation Test Passed: 10,000 inserts -> buffer bounded at 2000. Memory net diff: {total_diff_kb:.2f} KB")
        self.assertLess(total_diff_kb, 5000.0, f"Memory leak suspected: net diff is {total_diff_kb} KB")

    def test_03_latency_benchmark_lead_demand_heat(self):
        """
        バッファが満杯（2,000件）の状態で get_lead_demand_heat を 2,000 回実行し、
        レイテンシ（P50, P90, P95, P99, Mean）が 0.5ms 以下であることを実証。
        """
        # バッファを2000件で満たす
        now_dt = datetime.datetime(2026, 11, 10, 22, 0, 0, tzinfo=analytics._JST)
        for i in range(2000):
            aud = "women" if i % 3 == 0 else "men"
            area = "shibuya" if i % 2 == 0 else "shinjuku"
            analytics.track("hashigo_intent", f"{area}:{aud}:pair", _now_jst=now_dt)

        # 2,000回の測定
        num_runs = 2000
        latencies_sec = []

        # ウォームアップ
        for _ in range(50):
            analytics.get_lead_demand_heat(area="shibuya", hours=2, _now_jst=now_dt)

        for _ in range(num_runs):
            t0 = time.perf_counter()
            res = analytics.get_lead_demand_heat(area="shibuya", hours=2, _now_jst=now_dt)
            t1 = time.perf_counter()
            latencies_sec.append(t1 - t0)

        latencies_ms = [t * 1000.0 for t in latencies_sec]
        latencies_ms.sort()

        p50 = latencies_ms[int(num_runs * 0.50)]
        p90 = latencies_ms[int(num_runs * 0.90)]
        p95 = latencies_ms[int(num_runs * 0.95)]
        p99 = latencies_ms[int(num_runs * 0.99)]
        mean_val = sum(latencies_ms) / num_runs
        max_val = max(latencies_ms)

        print(f"\n[STRESS TEST 3] Latency Benchmark (2,000 runs, 2,000 buffered items):")
        print(f"  Mean: {mean_val*1000:.2f} µs ({mean_val:.4f} ms)")
        print(f"  P50 : {p50*1000:.2f} µs ({p50:.4f} ms)")
        print(f"  P90 : {p90*1000:.2f} µs ({p90:.4f} ms)")
        print(f"  P95 : {p95*1000:.2f} µs ({p95:.4f} ms)")
        print(f"  P99 : {p99*1000:.2f} µs ({p99:.4f} ms)")
        print(f"  Max : {max_val*1000:.2f} µs ({max_val:.4f} ms)")

        # 要件: 0.5ms 以下
        self.assertLess(mean_val, 0.50, f"Mean latency ({mean_val:.4f} ms) exceeds 0.5ms target")
        self.assertLess(p95, 0.50, f"P95 latency ({p95:.4f} ms) exceeds 0.5ms target")

    def test_04_throughput_and_cache_in_enrich_stores(self):
        """
        moat_intelligence.enrich_stores_with_moat_signals において、
        複数エリアの多数店舗（60店舗）を一括処理した際の所要時間および
        lead_heat_cache によるスループット維持を実証。
        """
        # バッファにデータを投入
        now_dt = datetime.datetime(2026, 11, 10, 23, 0, 0, tzinfo=analytics._JST)
        for i in range(500):
            aud = "women" if i % 2 == 0 else "men"
            for a in ["shibuya", "shinjuku", "umeda", "namba", "sakae", "fukuoka"]:
                analytics.track("hashigo_intent", f"{a}:{aud}:pair", _now_jst=now_dt)

        # 60店舗のダミーデータ作成
        stores = []
        area_names = ["渋谷", "新宿", "梅田", "なんば", "栄", "福岡天神"]
        for i in range(60):
            area_name = area_names[i % len(area_names)]
            stores.append({
                "name": f"オリエンタルラウンジ{area_name}{i}",
                "women": 25 + (i % 15),
                "men": 20 + (i % 10),
                "is_open_now": True,
                "women_diff": 2,
                "men_diff": 1,
            })

        t0 = time.perf_counter()
        enriched = moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=now_dt)
        elapsed_sec = time.perf_counter() - t0

        self.assertEqual(len(enriched), 60)
        # 全店舗に先行需要シグナルが付与されているか
        for s in enriched:
            fc = s.get("forecast_60m") or {}
            self.assertIn("lead_intent_heat", fc)
            self.assertIn("lead_intent_bias_pt", fc)
            self.assertIn("lead_demand_heat", s)

        per_store_ms = (elapsed_sec * 1000.0) / 60.0
        print(f"\n[STRESS TEST 4] Store Enrichment Throughput: 60 stores enriched in {elapsed_sec*1000:.2f} ms ({per_store_ms:.2f} ms/store)")
        # 1店舗あたりの処理時間が極小であることを実証
        self.assertLess(elapsed_sec, 0.5, f"Enriching 60 stores took too long: {elapsed_sec:.3f}s")

    def test_05_adversarial_and_boundary_resilience(self):
        """
        悪意ある入力（XSS、インジェクション、不正型、不正時間パラメータ）を
        並行大量投入してもシステムがクラッシュせず正常防衛することを実証。
        """
        malicious_labels = [
            "=1+1",
            "shibuya:<script>alert(1)</script>:pair",
            "shinjuku:men:' OR '1'='1",
            "umeda:women:" + "A" * 5000,
            "::::",
            "",
            None,
            12345,
            "shibuya:women:group\nDROP TABLE stores;--",
        ]

        # 異常系ラベルの大量投入
        for lbl in malicious_labels * 50:
            try:
                analytics.track("hashigo_intent", lbl)
            except Exception as e:
                self.fail(f"analytics.track crashed on label {repr(lbl)}: {e}")

        # 異常系パラメータでの get_lead_demand_heat 呼び出し
        invalid_params = [
            {"area": None, "hours": -1},
            {"area": "", "hours": 0},
            {"area": "unknown_mars_colony", "hours": 999999},
            {"area": "<script>", "hours": 2},
            {"area": "shibuya", "hours": None},
        ]
        for p in invalid_params:
            try:
                res = analytics.get_lead_demand_heat(**p)
                self.assertIsInstance(res, dict)
                self.assertIn("total_intents", res)
                self.assertIn("lead_signal", res)
            except Exception as e:
                self.fail(f"analytics.get_lead_demand_heat crashed on params {p}: {e}")

        print(f"\n[STRESS TEST 5] Adversarial & Boundary Resilience Test Passed (0 crashes)")

    def test_06_fallback_scan_latency_under_heavy_pending(self):
        """
        バッファ内に対象エリアが存在せず、かつ _pending に 2,000 件のイベントが存在する
        最悪シナリオにおいて、フォールバック走査のレイテンシが 0.5ms 以下に収まるかを検証。
        """
        now_dt = datetime.datetime(2026, 11, 10, 22, 0, 0, tzinfo=analytics._JST)
        # _pending に 2,000 件の他イベント（page_view, cushion_click など）を投入
        for i in range(2000):
            ev = "page_view" if i % 2 == 0 else "cushion_click"
            analytics.track(ev, f"label_{i % 50}", _now_jst=now_dt)

        # 対象エリア（存在しないエリア）へのクエリレイテンシ測定
        num_runs = 500
        latencies_ms = []
        for _ in range(num_runs):
            t0 = time.perf_counter()
            res = analytics.get_lead_demand_heat(area="non_existent_area", hours=2, _now_jst=now_dt)
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)

        latencies_ms.sort()
        mean_val = sum(latencies_ms) / num_runs
        p95 = latencies_ms[int(num_runs * 0.95)]
        max_val = max(latencies_ms)

        print(f"\n[STRESS TEST 6] Fallback Scan Latency (worst-case with 2,000 pending items):")
        print(f"  Mean: {mean_val*1000:.2f} µs ({mean_val:.4f} ms)")
        print(f"  P95 : {p95*1000:.2f} µs ({p95:.4f} ms)")
        print(f"  Max : {max_val*1000:.2f} µs ({max_val:.4f} ms)")

        self.assertEqual(res["total_intents"], 0)
        self.assertLess(mean_val, 0.50, f"Fallback scan mean latency ({mean_val:.4f} ms) exceeds 0.5ms target")
        self.assertLess(p95, 0.50, f"Fallback scan P95 latency ({p95:.4f} ms) exceeds 0.5ms target")

    def test_07_concurrent_track_query_and_flush_churn(self):
        """
        大量の並行 track / get_lead_demand_heat と同時に、
        バックグラウンドで flush やキャッシュリフレッシュが走るチャーン環境での
        デッドロックフリー・スレッドセーフティ実証。
        """
        stop_event = threading.Event()
        errors = []

        def worker_churn():
            try:
                while not stop_event.is_set():
                    analytics.track("hashigo_intent", "shibuya:women:solo")
                    analytics.get_lead_demand_heat(area="shibuya", hours=2)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(f"Churn worker error: {e}")

        def flush_worker():
            try:
                while not stop_event.is_set():
                    analytics._refresh_hourly_sheet_rows_async()
                    time.sleep(0.01)
            except Exception as e:
                errors.append(f"Flush worker error: {e}")

        threads = []
        for _ in range(8):
            t = threading.Thread(target=worker_churn)
            t.daemon = True
            t.start()
            threads.append(t)

        for _ in range(2):
            t = threading.Thread(target=flush_worker)
            t.daemon = True
            t.start()
            threads.append(t)

        # 1秒間高負荷チャーンを実行
        time.sleep(1.0)
        stop_event.set()

        for t in threads:
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(), "Thread deadlock detected: thread did not terminate within timeout")

        self.assertEqual(len(errors), 0, f"Errors during flush churn: {errors}")
        print(f"\n[STRESS TEST 7] Concurrent Track/Query & Flush Churn Passed (0 deadlocks, 0 errors)")



if __name__ == '__main__':
    unittest.main()
