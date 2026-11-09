# -*- coding: utf-8 -*-
"""
tests/test_challenger_m3_it3_verification.py

Milestone 3 Iteration 3: Empirical Challenger による独立実証検証テストスイート
1. 都市イベント複数ハブ重複所属（OLG 新宿）における重要度優先マージ（verified_surge > post_window > pre_announce）の完全検証
2. 非数値・無限大・NaN・多倍長整数入力に対する完全耐性とフォールバック検証
3. 64スレッド超並行下でのスレッドセーフティ・レースコンディション・データ破損ゼロ実証
4. 時刻境界（13:59 vs 14:00, 19:59 vs 20:00, 23:59 vs 翌02:00）における相転移の正確性検証
"""

import concurrent.futures
import datetime
import math
import time
import unittest

import arena_calendar
import moat_intelligence


class TestArenaHubPriorityMerging(unittest.TestCase):
    """
    タスク2実証検証:
    都市イベント複数ハブ重複所属（OLG 新宿, JIS SHINJUKU, ASOBIBAR 新宿）における
    重要度優先マージ（verified_surge > post_window > pre_announce）の検証
    """

    def setUp(self):
        self.jst = moat_intelligence.JST
        self.test_date_str = "2026-11-15"
        self.test_date = datetime.date(2026, 11, 15)

    def test_priority_order_verified_surge_beats_pre_announce(self):
        """
        verified_surge (優先度3) が後続の pre_announce (優先度1) に上書きされないことの実証。
        - 18:30 JST (事後検知窓外 19:00〜22:50 外のため意図しない野良サージ発動を遮断)
        - tokyo_dome_budokan (先頭): 終演19時見込み (18:30は終演窓内・実測2店舗急増あり -> verified_surge: prio 3)
        - meiji_jingu_stadium (後続): 終演21時見込み (18:30は事前告知帯 14<=t<20 -> pre_announce: prio 1)
        OLG 新宿 のシグナルが verified_surge (tokyo_dome_budokan) として維持されることを確認。
        """
        now_dt = datetime.datetime(2026, 11, 15, 18, 30, tzinfo=self.jst)

        live_stores = [
            {"name": "OLG 新宿", "women": 35, "men": 30, "women_diff": 8, "usual_w_diff": 12.0, "is_open_now": True},
            {"name": "OLG 上野", "women": 30, "men": 25, "women_diff": 7, "usual_w_diff": 11.0, "is_open_now": True},
            {"name": "OLG 渋谷本店", "women": 10, "men": 10, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
        ]

        mock_events = {
            "tokyo_dome_budokan": [{
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "event_title": "ドーム夕方公演",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "meiji_jingu_stadium": [{
                "venue_id": "meiji_jingu_stadium",
                "venue_name": "明治神宮野球場",
                "event_title": "ナイトゲーム",
                "est_end_hour": 21,
                "is_explicit_schedule": True,
            }],
        }

        orig_get_events = arena_calendar.get_arena_events_for_date
        try:
            arena_calendar.get_arena_events_for_date = lambda d, *args, **kwargs: mock_events

            surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=now_dt)

            self.assertIn("OLG 新宿", surges)
            shinjuku_sig = surges["OLG 新宿"]
            # tokyo_dome_budokan (verified_surge: prio 3) が meiji_jingu_stadium (pre_announce: prio 1) に上書きされずに残ること
            self.assertEqual(shinjuku_sig["phase"], "verified_surge")
            self.assertTrue(shinjuku_sig["is_surge_now"])
            self.assertEqual(shinjuku_sig["venue_id"], "tokyo_dome_budokan")

        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events

    def test_priority_order_verified_surge_beats_post_window(self):
        """
        verified_surge (優先度3) が後続の post_window (優先度2) に上書きされないことの実証。
        - 18:30 JST
        - tokyo_dome_budokan (先頭): 終演19時、実測2店舗急増 -> verified_surge (prio 3)
        - meiji_jingu_stadium (後続): 終演19時、実測急増不足（神宮は8店舗中2店舗必要だが新宿の1店のみ） -> post_window (prio 2)
        OLG 新宿 のシグナルが verified_surge (tokyo_dome_budokan) として維持されることを確認。
        """
        now_dt = datetime.datetime(2026, 11, 15, 18, 30, tzinfo=self.jst)

        live_stores = [
            {"name": "OLG 新宿", "women": 35, "men": 30, "women_diff": 8, "usual_w_diff": 12.0, "is_open_now": True},
            {"name": "OLG 上野", "women": 30, "men": 25, "women_diff": 7, "usual_w_diff": 11.0, "is_open_now": True},
            {"name": "OLG 渋谷本店", "women": 10, "men": 10, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
        ]

        mock_events = {
            "tokyo_dome_budokan": [{
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "event_title": "ドーム夕方公演",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "meiji_jingu_stadium": [{
                "venue_id": "meiji_jingu_stadium",
                "venue_name": "明治神宮野球場",
                "event_title": "神宮夕方ゲーム",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
        }

        orig_get_events = arena_calendar.get_arena_events_for_date
        try:
            arena_calendar.get_arena_events_for_date = lambda d, *args, **kwargs: mock_events

            surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=now_dt)

            self.assertIn("OLG 新宿", surges)
            shinjuku_sig = surges["OLG 新宿"]
            # verified_surge が post_window に上書きされずに残ること
            self.assertEqual(shinjuku_sig["phase"], "verified_surge")
            self.assertEqual(shinjuku_sig["venue_id"], "tokyo_dome_budokan")

        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events

    def test_priority_upgrade_from_pre_to_post_to_verified(self):
        """
        後から評価されるハブの優先度が高い場合、低優先度シグナルから正しく昇格（Upgrade）することの実証。
        tokyo_dome_budokan (pre_announce: prio 1) -> meiji_jingu_stadium (post_window: prio 2) -> japan_national_stadium (verified_surge: prio 3)
        """
        now_dt = datetime.datetime(2026, 11, 15, 18, 30, tzinfo=self.jst)

        # 新宿と渋谷（japan_national_stadium の店舗群）で急増
        live_stores = [
            {"name": "OLG 新宿", "women": 40, "men": 35, "women_diff": 10, "usual_w_diff": 15.0, "is_open_now": True},
            {"name": "OLG 渋谷本店", "women": 35, "men": 30, "women_diff": 8, "usual_w_diff": 12.0, "is_open_now": True},
            {"name": "OLG 上野", "women": 10, "men": 10, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
        ]

        mock_events = {
            "tokyo_dome_budokan": [{
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "event_title": "深夜公演",
                "est_end_hour": 23,
                "is_explicit_schedule": True,
            }],
            "meiji_jingu_stadium": [{
                "venue_id": "meiji_jingu_stadium",
                "venue_name": "明治神宮野球場",
                "event_title": "神宮夕方ゲーム",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "japan_national_stadium": [{
                "venue_id": "japan_national_stadium",
                "venue_name": "国立競技場",
                "event_title": "サッカー国際親善試合",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
        }

        orig_get_events = arena_calendar.get_arena_events_for_date
        try:
            arena_calendar.get_arena_events_for_date = lambda d, *args, **kwargs: mock_events

            surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=now_dt)

            self.assertIn("OLG 新宿", surges)
            shinjuku_sig = surges["OLG 新宿"]
            # 最終的に japan_national_stadium の verified_surge (prio 3) に昇格していること
            self.assertEqual(shinjuku_sig["phase"], "verified_surge")
            self.assertEqual(shinjuku_sig["venue_id"], "japan_national_stadium")
            self.assertTrue(shinjuku_sig["is_surge_now"])
        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events

    def test_post_window_beats_pre_announce(self):
        """
        post_window (優先度2) が後続の pre_announce (優先度1) より優先されることの実証。
        """
        now_dt = datetime.datetime(2026, 11, 15, 18, 30, tzinfo=self.jst)

        live_stores = [
            {"name": "OLG 新宿", "women": 20, "men": 20, "women_diff": 1, "usual_w_diff": 1.0, "is_open_now": True},
            {"name": "OLG 上野", "women": 15, "men": 15, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
        ]

        # tokyo_dome_budokan: 終演19時、実測急増なし -> post_window (prio 2)
        # meiji_jingu_stadium: 終演21時 -> pre_announce (prio 1)
        mock_events = {
            "tokyo_dome_budokan": [{
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "event_title": "ドーム公演",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "meiji_jingu_stadium": [{
                "venue_id": "meiji_jingu_stadium",
                "venue_name": "明治神宮野球場",
                "event_title": "ナイトゲーム",
                "est_end_hour": 21,
                "is_explicit_schedule": True,
            }],
        }

        orig_get_events = arena_calendar.get_arena_events_for_date
        try:
            arena_calendar.get_arena_events_for_date = lambda d, *args, **kwargs: mock_events

            surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=now_dt)

            self.assertIn("OLG 新宿", surges)
            shinjuku_sig = surges["OLG 新宿"]
            # post_window が pre_announce に上書きされずに残ること
            self.assertEqual(shinjuku_sig["phase"], "post_window")
            self.assertEqual(shinjuku_sig["venue_id"], "tokyo_dome_budokan")
        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events


class TestAdversarialExtremeTypesAndStress(unittest.TestCase):
    """
    非数値・無限大・NaN・多倍長整数に対する防護性能と、
    64スレッド超並行下でのスレッドセーフティ実証
    """

    def test_non_numeric_and_fuzzing_in_enrich_stores(self):
        """
        客数等の数値項目に多種多様な異常データ（NaN, Inf, 巨大整数, 文字列）を同時投入した際の
        enrich_stores_with_moat_signals の堅牢性
        """
        fuzz_stores = [
            {"name": "OLG 新宿", "women": float("nan"), "men": float("inf"), "is_open_now": True},
            {"name": "JIS 難波", "women": 10**350, "men": -10**350, "women_diff": "error", "men_diff": None},
            {"name": "ASOBIBAR 梅田", "women": "-50", "men": "-30", "is_open_now": False},
            {"name": "OLG 渋谷本店", "women": "not_a_number", "men": "unknown", "is_open_now": True},
            {"name": "OLG 上野", "women": None, "men": None, "is_open_now": True},
        ]

        try:
            enriched = moat_intelligence.enrich_stores_with_moat_signals(fuzz_stores, allow_weather_blocking=False)
            self.assertEqual(len(enriched), len(fuzz_stores))
            for st in enriched:
                self.assertIn("turnover_momentum", st)
                self.assertIn("weather_brief", st)
                self.assertIsInstance(st["turnover_momentum"], dict)
                self.assertIsInstance(st["weather_brief"], dict)
        except Exception as e:
            self.fail(f"enrich_stores_with_moat_signals raised unexpected exception: {type(e).__name__}: {e}")

    def test_non_string_store_name_vulnerability_discovery(self):
        """
        【境界ケース防護検証】
        store['name'] に非文字列（数値 12345 等）が渡された場合でも、
        AttributeError でクラッシュせず安全にフォールバックして処理完走することを実証
        """
        malformed_name_stores = [
            {"name": 12345, "women": 20, "men": 20, "is_open_now": True},
        ]
        res = moat_intelligence.detect_arena_event_surges(malformed_name_stores)
        self.assertIsInstance(res, dict)

    def test_64_threads_ultra_concurrency(self):
        """
        64スレッド超並行下で全モート知能エンジンを同時連打し、
        競合、デッドロック、データ破壊が一切発生しないことを実証
        """
        sample_batch = [
            {"name": "OLG 新宿", "women": 40, "men": 35, "women_diff": 5, "men_diff": 2, "is_open_now": True},
            {"name": "JIS 難波", "women": 50, "men": 45, "women_diff": -3, "men_diff": 4, "is_open_now": True, "pos_telemetry": {"is_reverse_waiting": True}},
            {"name": "ASOBIBAR 梅田", "women": 30, "men": 30, "is_open_now": True},
            {"name": "OLG 札幌", "women": 0, "men": 0, "is_open_now": False},
        ]

        num_threads = 64
        iters_per_thread = 20
        all_errors = []

        def worker(tid):
            t_errs = []
            try:
                for i in range(iters_per_thread):
                    # 1. カレンダー
                    ev = arena_calendar.get_arena_events_for_date("2026-07-25")
                    if not isinstance(ev, dict):
                        t_errs.append(f"T{tid}: calendar returned non-dict")

                    # 2. エンリッチ
                    batch = [dict(s) for s in sample_batch]
                    res = moat_intelligence.enrich_stores_with_moat_signals(batch, allow_weather_blocking=False)
                    if len(res) != len(sample_batch):
                        t_errs.append(f"T{tid}: enrich count mismatch")

                    # 3. 席回転計算
                    tm = moat_intelligence.calculate_turnover_momentum_index(
                        store_name="OLG 新宿", women=35 + (i % 5), men=30, is_open_now=True
                    )
                    if tm.get("score") is None:
                        t_errs.append(f"T{tid}: turnover score missing")

                    # 4. 都市イベントサージ検知
                    surges = moat_intelligence.detect_arena_event_surges(batch)
                    if not isinstance(surges, dict):
                        t_errs.append(f"T{tid}: surges not dict")

            except Exception as e:
                t_errs.append(f"T{tid} crashed: {type(e).__name__}: {e}")
            return t_errs

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker, tid) for tid in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures, timeout=15.0):
                all_errors.extend(fut.result())
        t_elapsed = time.perf_counter() - t_start

        print(f"\n[ULTRA CONCURRENCY] 64 threads x {iters_per_thread} iterations ({num_threads * iters_per_thread} total ops):")
        print(f"  Elapsed: {t_elapsed:.3f} s | Errors: {len(all_errors)}")

        self.assertEqual(len(all_errors), 0, f"Errors in 64-thread test: {all_errors[:10]}")
        self.assertLess(t_elapsed, 10.0, f"64 threads took too long: {t_elapsed:.2f}s")


class TestPhaseTransitionBoundaries(unittest.TestCase):
    """
    時刻境界における相転移（Phase Transition）の厳密検証
    13:59 (告知前) -> 14:00 (pre_announce) -> 19:59 (pre_announce) -> 20:00 (post_window) -> 23:59 (post_window) -> 翌02:00 (窓外)
    """

    def setUp(self):
        self.jst = moat_intelligence.JST

    def test_schedule_phase_timeline_transitions(self):
        """
        終演21時見込みのイベントに対して時刻推移に応じたフェーズ遷移を検証：
        - 13:59: フェーズなし (None)
        - 14:00: pre_announce (is_surge_now=False)
        - 19:59: win_start は max(18, 21-1) = 20時 なので pre_announce
        - 20:00: 終演窓突入 (win_start=20) -> post_window (実測なし)
        - 22:00: 終演窓内 -> post_window
        - 23:59: win_end_ext は 21+2=23時 (hour=23) なので post_window
        - 翌日 02:00: eff_hr = 2 + 24 = 26 > 23 なので窓終了 -> なし (None)
        """
        mock_events = {
            "tokyo_dome_budokan": [{
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "event_title": "ドームライブ",
                "est_end_hour": 21,
                "is_explicit_schedule": True,
            }],
        }
        live_stores = [
            {"name": "OLG 新宿", "women": 20, "men": 20, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
            {"name": "OLG 上野", "women": 20, "men": 20, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
        ]

        test_timeline = [
            (datetime.time(13, 59), None),
            (datetime.time(14, 0), "pre_announce"),
            (datetime.time(19, 59), "pre_announce"),
            (datetime.time(20, 0), "post_window"),
            (datetime.time(22, 0), "post_window"),
            (datetime.time(23, 59), "post_window"),
            (datetime.time(2, 0), None),  # 翌日2時（eff_hr=26 > 23）
        ]

        orig_get_events = arena_calendar.get_arena_events_for_date
        try:
            arena_calendar.get_arena_events_for_date = lambda d, *args, **kwargs: mock_events

            for t_val, expected_phase in test_timeline:
                day_offset = 1 if t_val.hour < 9 else 0
                test_d = datetime.date(2026, 11, 15) + datetime.timedelta(days=day_offset)
                dt = datetime.datetime.combine(test_d, t_val).replace(tzinfo=self.jst)
                surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=dt)
                sig = surges.get("OLG 新宿")

                if expected_phase is None:
                    self.assertIsNone(sig, f"At {t_val}, expected None but got {sig}")
                else:
                    self.assertIsNotNone(sig, f"At {t_val}, expected {expected_phase} but got None")
                    self.assertEqual(sig["phase"], expected_phase, f"At {t_val}, expected phase {expected_phase} but got {sig['phase']}")
        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events


if __name__ == "__main__":
    unittest.main()
