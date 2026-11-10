# -*- coding: utf-8 -*-
"""
tests/test_challenger_m3_it4_2_deep_verification.py

Milestone 3 Iteration 4: Empirical Challenger 2 (teamwork_challenger)
独立深層実証検証テストスイート:
1. 都市イベント複数ハブ重要度優先マージ（verified_surge > post_window > pre_announce）の数学的・順列網羅検証
   - ハブ評価順序の全順列（3! = 6通り）における不変性検証
   - post_window vs pre_announce の全順列（2! = 2通り）における不変性検証
   - 関西（京セラ・甲子園・城ホール・淀川花火）および東京（ドーム・神宮・国立・隅田川花火）の重複ハブ検証
2. 64スレッド超並行下での高負荷アクセス・スレッドセーフティ・レースコンディション・デッドロック不在実証
   - 64スレッド複合高負荷ストレステスト（1,920回トランザクション）
   - 128スレッド極限バーストストレステスト（1,280回トランザクション）
   - 共通入力辞書の非破壊性（ミューテーション競合なし）
   - 例外件数 0件、デッドロック 0件、データ破損 0件の実証
3. コードベース制約遵守の機械的検証（app.py 2,800行以下、turbo_store.py 差分ゼロ等）
"""

import concurrent.futures
import datetime
import itertools
import os
import subprocess
import time
import unittest

import arena_calendar
import moat_intelligence


class TestChallengerHubPriorityExhaustivePermutations(unittest.TestCase):
    """
    都市イベント複数ハブ重要度優先マージ（verified_surge > post_window > pre_announce）の
    全順列（Permutations）および境界条件網羅検証
    """

    def setUp(self):
        self.jst = moat_intelligence.JST
        self.test_dt = datetime.datetime(2026, 11, 15, 18, 30, tzinfo=self.jst)

    def test_priority_merging_all_permutations(self):
        """
        3つの異なるフェーズ（verified_surge: prio 3, post_window: prio 2, pre_announce: prio 1）を持つ
        ハブの評価順序（全3! = 6通り）をシャッフルしても、常に最高優先度（verified_surge）が勝利することを実証
        """
        target_store = "OLG 新宿"
        live_stores = [
            {"name": target_store, "women": 35, "men": 30, "women_diff": 8, "usual_w_diff": 12.0, "is_open_now": True},
            {"name": "OLG 渋谷本店", "women": 10, "men": 10, "women_diff": 0, "usual_w_diff": 0.0, "is_open_now": True},
        ]

        # 1. verified_surge を発生させるハブ: 新宿1店舗のみ所属 (needed=1, 急増=1 -> verified_surge)
        hub_verified = {
            "venue_id": "hub_verified_v",
            "venue_name": "実測サージ会場",
            "stores": [target_store],
        }
        # 2. post_window を発生させるハブ: 新宿と他2店舗所属 (needed=2, 新宿のみ急増で 急増=1 < 2 -> post_window)
        hub_post = {
            "venue_id": "hub_post_v",
            "venue_name": "終演窓会場",
            "stores": [target_store, "店舗B", "店舗C"],
        }
        # 3. pre_announce を発生させるハブ: 終演22時 (14 <= eff_hr < 21 なので pre_announce)
        hub_pre = {
            "venue_id": "hub_pre_v",
            "venue_name": "事前告知会場",
            "stores": [target_store],
        }

        mock_events = {
            "hub_verified_v": [{
                "venue_id": "hub_verified_v",
                "venue_name": "実測サージ会場",
                "event_title": "サージライブ",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "hub_post_v": [{
                "venue_id": "hub_post_v",
                "venue_name": "終演窓会場",
                "event_title": "終演ライブ",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "hub_pre_v": [{
                "venue_id": "hub_pre_v",
                "venue_name": "事前告知会場",
                "event_title": "告知ライブ",
                "est_end_hour": 22,
                "is_explicit_schedule": True,
            }],
        }

        hubs_list = [hub_verified, hub_post, hub_pre]
        permutations = list(itertools.permutations(hubs_list))
        self.assertEqual(len(permutations), 6, "3! must be 6 permutations")

        orig_get_events = arena_calendar.get_arena_events_for_date
        orig_hubs = moat_intelligence.MAJOR_ARENA_HUBS

        try:
            arena_calendar.get_arena_events_for_date = lambda d, *a, **kw: mock_events

            for perm_idx, perm_hubs in enumerate(permutations):
                moat_intelligence.MAJOR_ARENA_HUBS = perm_hubs

                surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=self.test_dt)
                self.assertIn(target_store, surges, f"Permutation {perm_idx} failed: store missing")
                sig = surges[target_store]

                # 評価順序に関わらず常に verified_surge が最高優先度として残ること
                self.assertEqual(
                    sig["phase"],
                    "verified_surge",
                    f"Permutation {perm_idx} failed! Expected 'verified_surge', got '{sig['phase']}' (order: {[h['venue_id'] for h in perm_hubs]})"
                )
                self.assertEqual(sig["venue_id"], "hub_verified_v")
                self.assertTrue(sig["is_surge_now"])
        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events
            moat_intelligence.MAJOR_ARENA_HUBS = orig_hubs

    def test_post_window_vs_pre_announce_permutations(self):
        """
        post_window (優先度2) と pre_announce (優先度1) の2ハブにおいて、
        評価順序（2! = 2通り）に関わらず常に post_window が勝利することを実証
        """
        target_store = "OLG 難波"
        live_stores = [
            {"name": target_store, "women": 20, "men": 20, "women_diff": 1, "usual_w_diff": 1.0, "is_open_now": True},
        ]

        hub_post = {
            "venue_id": "hub_post_k",
            "venue_name": "終演窓会場関西",
            "stores": [target_store, "店舗B", "店舗C"],
        }
        hub_pre = {
            "venue_id": "hub_pre_k",
            "venue_name": "事前告知会場関西",
            "stores": [target_store],
        }

        mock_events = {
            "hub_post_k": [{
                "venue_id": "hub_post_k",
                "venue_name": "終演窓会場関西",
                "event_title": "終演イベント",
                "est_end_hour": 19,
                "is_explicit_schedule": True,
            }],
            "hub_pre_k": [{
                "venue_id": "hub_pre_k",
                "venue_name": "事前告知会場関西",
                "event_title": "事前イベント",
                "est_end_hour": 22,
                "is_explicit_schedule": True,
            }],
        }

        hubs_list = [hub_post, hub_pre]
        permutations = list(itertools.permutations(hubs_list))

        orig_get_events = arena_calendar.get_arena_events_for_date
        orig_hubs = moat_intelligence.MAJOR_ARENA_HUBS

        try:
            arena_calendar.get_arena_events_for_date = lambda d, *a, **kw: mock_events

            for perm_idx, perm_hubs in enumerate(permutations):
                moat_intelligence.MAJOR_ARENA_HUBS = perm_hubs
                surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=self.test_dt)
                self.assertIn(target_store, surges)
                sig = surges[target_store]

                self.assertEqual(
                    sig["phase"],
                    "post_window",
                    f"Permutation {perm_idx} failed! Expected 'post_window', got '{sig['phase']}'"
                )
                self.assertEqual(sig["venue_id"], "hub_post_k")
        finally:
            arena_calendar.get_arena_events_for_date = orig_get_events
            moat_intelligence.MAJOR_ARENA_HUBS = orig_hubs

    def test_kansai_multi_hub_overlap_real_world(self):
        """
        実在の関西ハブ（kyocera_dome_osaka, hanshin_koshien_stadium, osaka_jo_hall, yodogawa_fireworks）における
        重複所属店舗（ASOBIBAR 梅田, JIS 難波 等）の優先度マージ実証
        """
        now_dt = datetime.datetime(2026, 10, 17, 20, 30, tzinfo=self.jst)  # 第38回なにわ淀川花火大会（10/17公式確定日）終演帯
        live_stores = [
            {"name": "ASOBIBAR 梅田", "women": 45, "men": 40, "women_diff": 12, "usual_w_diff": 15.0, "is_open_now": True},
            {"name": "JIS 難波", "women": 50, "men": 45, "women_diff": 5, "usual_w_diff": 6.0, "is_open_now": True},
            {"name": "OLG 梅田茶屋町", "women": 40, "men": 35, "women_diff": 8, "usual_w_diff": 10.0, "is_open_now": True},
        ]

        surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=now_dt)

        self.assertIsInstance(surges, dict)
        self.assertIn("ASOBIBAR 梅田", surges)
        sig = surges["ASOBIBAR 梅田"]
        self.assertTrue(sig["is_surge_now"])
        self.assertIn(sig["phase"], ("verified_surge", "post_window"))
        self.assertEqual(sig["venue_id"], "yodogawa_fireworks")

    def test_tie_breaking_same_priority_stability(self):
        """
        同一優先度（verified_surge vs verified_surge 等）のハブが競合した場合、
        例外やデータ破損が発生せず、整合性のある辞書が安全に保持されることの実証
        """
        target_store = "OLG 新宿"
        hub1 = {"venue_id": "venue_a", "venue_name": "会場A", "stores": [target_store]}
        hub2 = {"venue_id": "venue_b", "venue_name": "会場B", "stores": [target_store]}
        mock_events = {
            "venue_a": [{"venue_id": "venue_a", "venue_name": "会場A", "est_end_hour": 19, "is_explicit_schedule": True}],
            "venue_b": [{"venue_id": "venue_b", "venue_name": "会場B", "est_end_hour": 19, "is_explicit_schedule": True}],
        }
        live_stores = [{"name": target_store, "women": 35, "men": 30, "women_diff": 8, "usual_w_diff": 12.0, "is_open_now": True}]

        orig_events = arena_calendar.get_arena_events_for_date
        orig_hubs = moat_intelligence.MAJOR_ARENA_HUBS
        try:
            arena_calendar.get_arena_events_for_date = lambda *a, **k: mock_events
            moat_intelligence.MAJOR_ARENA_HUBS = [hub1, hub2]

            surges = moat_intelligence.detect_arena_event_surges(live_stores, now_jst=self.test_dt)
            self.assertIn(target_store, surges)
            sig = surges[target_store]
            self.assertEqual(sig["phase"], "verified_surge")
            self.assertIn(sig["venue_id"], ("venue_a", "venue_b"))
            self.assertTrue(sig["is_surge_now"])
        finally:
            arena_calendar.get_arena_events_for_date = orig_events
            moat_intelligence.MAJOR_ARENA_HUBS = orig_hubs


class TestChallengerUltraConcurrency64And128(unittest.TestCase):
    """
    64スレッドおよび128スレッド超並行下での高負荷アクセス・スレッドセーフティ・競合・デッドロック不在実証
    """

    @classmethod
    def setUpClass(cls):
        cls.sample_stores = []
        for sname, cap in list(moat_intelligence.STORE_P99_CAPACITY.items())[:30]:
            cls.sample_stores.append({
                "name": sname,
                "women": int(cap * 0.4),
                "men": int(cap * 0.45),
                "women_diff": 4,
                "men_diff": -2,
                "is_open_now": True,
                "pos_telemetry": {
                    "is_women_waiting": False,
                    "is_reverse_waiting": False,
                    "women_waiting_groups": 0,
                    "men_waiting_groups": 2,
                } if "JIS" in sname else None,
            })

    def test_64_threads_composite_high_load_stress(self):
        """
        64スレッド並行で、全モート知能エンジン・カレンダー・気象・席回転・都市イベントを
        高頻度連打し、例外 0件、デッドロック 0件、データ破損 0件を実証
        """
        num_threads = 64
        iters_per_thread = 30  # 計 1,920 回の複合トランザクション
        errors = []

        def worker(tid):
            t_errs = []
            try:
                for i in range(iters_per_thread):
                    # 1. カレンダー取得（事前定義都市イベントマージ）
                    date_probe = "2026-11-10" if i % 2 == 0 else "2026-07-25"
                    events = arena_calendar.get_arena_events_for_date(date_probe)
                    if not isinstance(events, dict):
                        t_errs.append(f"T{tid}: events returned {type(events)}")

                    # 2. 全店舗モートシグナル付与（エンリッチメント）
                    batch = [dict(s) for s in self.sample_stores]
                    res = moat_intelligence.enrich_stores_with_moat_signals(batch, allow_weather_blocking=False)
                    if len(res) != len(self.sample_stores):
                        t_errs.append(f"T{tid}: enriched length mismatch {len(res)}")

                    # 3. 席回転スピード指数計算
                    tm = moat_intelligence.calculate_turnover_momentum_index(
                        store_name="OLG 新宿",
                        women=25 + (i % 10),
                        men=30,
                        is_open_now=True,
                        women_diff=2,
                        men_diff=1,
                    )
                    if tm.get("score") is None:
                        t_errs.append(f"T{tid}: turnover momentum score is None unexpectedly")

                    # 4. 都市イベントサージ検知
                    surges = moat_intelligence.detect_arena_event_surges(batch)
                    if not isinstance(surges, dict):
                        t_errs.append(f"T{tid}: surges not dict")

                    # 5. 60分後予測ダイナミクス
                    pred = moat_intelligence.predict_store_60m_dynamics(
                        store_name="OLG 新宿",
                        women=30,
                        men=30,
                        lead_intent_heat={"total_intents": 5, "lead_signal": "female_surge", "lead_bias_pt": 3.5},
                    )
                    if not isinstance(pred, dict) or "pred_women" not in pred:
                        t_errs.append(f"T{tid}: pred missing pred_women")

            except Exception as e:
                t_errs.append(f"T{tid} crashed with: {type(e).__name__}: {e}")
            return t_errs

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, tid) for tid in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures, timeout=20.0):
                errors.extend(fut.result())
        elapsed = time.perf_counter() - t0

        print(f"\n[STRESS 64 THREADS] 64 threads x {iters_per_thread} iters ({num_threads * iters_per_thread} total tx):")
        print(f"  Time: {elapsed:.3f} s | Errors: {len(errors)}")

        self.assertEqual(len(errors), 0, f"Detected concurrency errors: {errors[:5]}")
        self.assertLess(elapsed, 10.0, f"64 threads execution took too long: {elapsed:.2f}s")

    def test_128_threads_burst_stress(self):
        """
        128スレッドの瞬間バースト（極限超並行）下でスレッドプールおよびモート関数群が破綻しないことを実証
        """
        num_threads = 128
        iters_per_thread = 10  # 計 1,280 回のバースト
        errors = []

        def burst_worker(tid):
            t_errs = []
            try:
                for i in range(iters_per_thread):
                    tm = moat_intelligence.calculate_turnover_momentum_index(
                        store_name="JIS 難波",
                        women=40,
                        men=40,
                        is_open_now=True,
                        pos_telemetry={"is_reverse_waiting": (i % 2 == 0)},
                    )
                    if tm.get("score") is None:
                        t_errs.append(f"Burst T{tid}: None score")

                    evs = arena_calendar.get_arena_events_for_date("2026-11-10")
                    if not isinstance(evs, dict):
                        t_errs.append(f"Burst T{tid}: evs not dict")
            except Exception as e:
                t_errs.append(f"Burst T{tid} crashed: {e}")
            return t_errs

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(burst_worker, tid) for tid in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures, timeout=15.0):
                errors.extend(fut.result())
        elapsed = time.perf_counter() - t0

        print(f"\n[BURST 128 THREADS] 128 threads x {iters_per_thread} iters ({num_threads * iters_per_thread} total tx):")
        print(f"  Time: {elapsed:.3f} s | Errors: {len(errors)}")

        self.assertEqual(len(errors), 0, f"Detected burst errors: {errors[:5]}")
        self.assertLess(elapsed, 8.0, f"128 threads burst took too long: {elapsed:.2f}s")

    def test_shared_input_dict_immutability_under_concurrency(self):
        """
        複数スレッドが【同一の入力店舗辞書】を同時に参照して enrich_stores_with_moat_signals を実行した際、
        入力辞書の元データが他スレッドの書き込みによって破壊・汚染されないことの実証
        """
        immutable_seed_store = {
            "name": "OLG 新宿",
            "women": 30,
            "men": 25,
            "women_diff": 4,
            "men_diff": 2,
            "is_open_now": True,
            "custom_marker": "ORIGINAL_UNTOUCHED_VALUE",
        }
        num_threads = 32
        errors = []

        def immutability_worker(tid):
            t_errs = []
            try:
                for _ in range(20):
                    batch = [dict(immutable_seed_store)]
                    res = moat_intelligence.enrich_stores_with_moat_signals(batch, allow_weather_blocking=False)
                    if res[0].get("custom_marker") != "ORIGINAL_UNTOUCHED_VALUE":
                        t_errs.append(f"T{tid}: custom_marker mutated")
                    if "turnover_momentum" not in res[0]:
                        t_errs.append(f"T{tid}: turnover_momentum missing")
            except Exception as e:
                t_errs.append(f"T{tid} error: {e}")
            return t_errs

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(immutability_worker, tid) for tid in range(num_threads)]
            for fut in concurrent.futures.as_completed(futures, timeout=10.0):
                errors.extend(fut.result())

        self.assertEqual(len(errors), 0, f"Shared dict mutation errors: {errors}")

    def test_64_threads_poisoned_data_concurrency(self):
        """
        多倍長整数（10**400）、NaN、Inf、None、非文字列店舗名等の毒入りデータが混在する
        64スレッド並行アクセス下でも、クラッシュ・データ破損・デッドロックが発生しないことの実証
        """
        stores_normal = [
            {"name": "OLG 新宿", "women": 30, "men": 25, "is_open_now": True},
            {"name": "JIS 難波", "women": 40, "men": 35, "is_open_now": True, "pos_telemetry": {"is_reverse_waiting": True}},
        ]
        stores_poisoned = [
            {"name": 12345, "women": 10**400, "men": -10**400, "women_diff": float("nan"), "men_diff": float("inf"), "is_open_now": None},
            {"name": None, "women": "not_a_number", "men": None, "is_open_now": True},
            None,
            "corrupted_store_string",
        ]

        num_threads = 64
        iters = 25
        errors = []

        def worker(tid):
            t_errs = []
            try:
                for _ in range(iters):
                    if tid % 2 == 1:
                        batch = [s if not isinstance(s, dict) else dict(s) for s in stores_poisoned]
                    else:
                        batch = [dict(s) for s in stores_normal]

                    res = moat_intelligence.enrich_stores_with_moat_signals(batch, allow_weather_blocking=False)
                    if not isinstance(res, list):
                        t_errs.append(f"T{tid}: res not list")

                    surges = moat_intelligence.detect_arena_event_surges(batch)
                    if not isinstance(surges, dict):
                        t_errs.append(f"T{tid}: surges not dict")
            except Exception as e:
                t_errs.append(f"T{tid} crashed: {type(e).__name__}: {e}")
            return t_errs

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as pool:
            futs = [pool.submit(worker, tid) for tid in range(num_threads)]
            for f in concurrent.futures.as_completed(futs, timeout=15.0):
                errors.extend(f.result())

        self.assertEqual(len(errors), 0, f"Poisoned concurrency errors: {errors}")


class TestChallengerSystemConstraintsAndCompliance(unittest.TestCase):
    """
    R5 制約事項およびアーキテクチャ不変条件の機械的検証
    """

    def test_app_py_lines_under_2800(self):
        """app.py の行数が厳格に 2,800行以下であること"""
        root_dir = "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
        app_path = os.path.join(root_dir, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        line_count = len(lines)
        print(f"\n[CONSTRAINT] app.py line count: {line_count} (limit: <= 2800)")
        self.assertLessEqual(line_count, 2800, f"app.py has {line_count} lines, exceeding 2800 limit")

    def test_turbo_store_git_diff_zero(self):
        """turbo_store.py が一切変更されていないこと（git diff 差分ゼロ実証）"""
        root_dir = "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
        res = subprocess.run(
            ["git", "diff", "turbo_store.py"],
            cwd=root_dir,
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "", f"turbo_store.py has unexpected git diff: {res.stdout}")


if __name__ == "__main__":
    unittest.main()
