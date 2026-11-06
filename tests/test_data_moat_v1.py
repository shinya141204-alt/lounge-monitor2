import csv
import datetime
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import app as app_module
import arena_calendar
import local_db
import moat_intelligence
import price_comparison


FORBIDDEN_STANDALONE_BRAND_RE = re.compile(
    r"オリエンタル(?!ラウンジ)|(?<![A-Za-z0-9_-])OLG(?![A-Za-z0-9_-])|(?<![A-Za-z0-9_-])YATAKOI(?![A-Za-z0-9_-])|オリラジ"
)


class DataMoatV1TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db_path = os.path.join(self.tmp_dir.name, "test_moat_v1.db")
        self._orig_db_path = local_db.DB_PATH
        self._orig_pg_patch = mock.patch.object(local_db, "_is_pg", return_value=False)
        self._orig_pg_patch.start()
        local_db.DB_PATH = self.tmp_db_path
        local_db.reset_caches_for_tests()
        local_db.init_db()
        arena_calendar.reset_for_tests()
        app_module.app.config["TESTING"] = True
        app_module.clear_fast_http_caches()
        self.client = app_module.app.test_client()

        # Ensure known stores exist in latest_data for vote/watch validation
        with app_module.data_lock:
            self._saved_latest = json.loads(json.dumps(app_module.latest_data, default=str))
            app_module.latest_data["full_data"] = [
                {
                    "name": "OLG 新宿",
                    "women": 28,
                    "men": 16,
                    "total": 44,
                    "area": "東京・関東",
                    "region": "Kanto",
                },
                {
                    "name": "OLG 渋谷本店",
                    "women": 22,
                    "men": 18,
                    "total": 40,
                    "area": "東京・関東",
                    "region": "Kanto",
                },
                {
                    "name": "JIS NAMBA",
                    "women": 20,
                    "men": 14,
                    "total": 34,
                    "area": "大阪・関西",
                    "region": "Kansai",
                },
            ]
            app_module.latest_data["last_updated"] = "21:15"

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        arena_calendar.reset_for_tests()
        local_db.DB_PATH = self._orig_db_path
        local_db.reset_caches_for_tests()
        self._orig_pg_patch.stop()
        app_module.clear_fast_http_caches()
        self.tmp_dir.cleanup()

    # ------------------------------------------------------------------
    # Pillar 1: Time-compounding rollups, 90-day raw retention & anonymization
    # ------------------------------------------------------------------
    def test_pillar1_rollup_before_prune_and_vote_anonymization(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime.now(jst).replace(tzinfo=None)
        old_95d = (now - datetime.timedelta(days=95)).replace(hour=21, minute=10, second=0)
        mid_70d = (now - datetime.timedelta(days=70)).replace(hour=21, minute=10, second=0)
        old_vote_16d = (now - datetime.timedelta(days=16)).strftime("%Y-%m-%d %H:%M:%S")
        fresh_vote_1h = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                [
                    (old_95d.strftime("%Y-%m-%d %H:%M:%S"), "OLG 新宿", 32, 18),
                    ((old_95d + datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"), "OLG 新宿", 36, 20),
                    (mid_70d.strftime("%Y-%m-%d %H:%M:%S"), "OLG 新宿", 24, 16),
                ],
            )
            cur.executemany(
                "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes) VALUES (?, ?, ?, ?, ?)",
                [
                    (old_vote_16d, "OLG 新宿", "waiting", "secret_hash_old", 30),
                    (fresh_vote_1h, "OLG 新宿", "smooth", "secret_hash_new", 0),
                ],
            )
            cur.execute(
                "INSERT INTO store_watch_intents (timestamp, store_name, target_cond, ip_hash) VALUES (?, ?, ?, ?)",
                (old_vote_16d, "OLG 新宿", "female_55", "watch_hash_old"),
            )
            conn.commit()
        finally:
            conn.close()

        # 1) Safety guard: if rollup raises, prune_old_logs must abort without deleting raw monitor_logs
        with mock.patch.object(local_db, "rollup_store_hourly_stats", side_effect=RuntimeError("simulated failure")):
            local_db.prune_old_logs(monitor_days=90, vote_days=14)
        cnt_after_abort = local_db._fetch_all_rows("SELECT COUNT(*) FROM monitor_logs")[0][0]
        self.assertEqual(cnt_after_abort, 3)

        # 2) Normal prune_old_logs(monitor_days=90): rolls up 95d data first, keeps 70d raw data, anonymizes 16d votes
        local_db.prune_old_logs(monitor_days=90, vote_days=14)

        remaining_raw = local_db._fetch_all_rows("SELECT timestamp, women FROM monitor_logs")
        self.assertEqual(len(remaining_raw), 1)
        self.assertEqual(remaining_raw[0][1], 24)  # 70-day-old row preserved under 90-day policy

        rollups = local_db._fetch_all_rows(
            "SELECT date_jst, hour_jst, store_name, sample_count, max_women, avg_women FROM store_hourly_rollups WHERE date_jst = ?",
            (old_95d.strftime("%Y-%m-%d"),),
        )
        self.assertEqual(len(rollups), 1)
        self.assertEqual(rollups[0][1], 21)
        self.assertEqual(rollups[0][2], "OLG 新宿")
        self.assertEqual(rollups[0][3], 2)
        self.assertEqual(rollups[0][4], 36)
        self.assertAlmostEqual(float(rollups[0][5]), 34.0, places=1)

        day_ctx = local_db._fetch_all_rows(
            "SELECT date_jst, season_tag FROM day_context_logs WHERE date_jst = ?",
            (old_95d.strftime("%Y-%m-%d"),),
        )
        self.assertEqual(len(day_ctx), 1)

        # Votes and watch intents are NEVER deleted, only ip_hash anonymized after 14 days
        votes_all = local_db._fetch_all_rows(
            "SELECT vote_type, ip_hash, wait_minutes FROM crowd_votes ORDER BY timestamp ASC"
        )
        self.assertEqual(len(votes_all), 2)
        self.assertEqual(votes_all[0][0], "waiting")
        self.assertEqual(votes_all[0][1], "")  # Anonymized!
        self.assertEqual(votes_all[0][2], 30)  # First-party wait_minutes preserved forever!
        self.assertEqual(votes_all[1][1], "secret_hash_new")  # Recent vote retains anti-dup hash

        watches_all = local_db._fetch_all_rows("SELECT target_cond, ip_hash FROM store_watch_intents")
        self.assertEqual(len(watches_all), 1)
        self.assertEqual(watches_all[0][1], "")  # Anonymized!

    def test_pillar1_store_event_history_diff_archive(self):
        evs_day1 = [
            {"brand": "OLG", "store_name": "OLG 新宿", "title": "早割男性半額ナイト", "gender": "men", "url": "https://oriental-lounge.com/"},
        ]
        n1 = local_db.upsert_store_event_history(evs_day1, date_jst="2026-11-05")
        self.assertEqual(n1, 1)
        # Re-crawling on the same day must NOT increment days_seen
        local_db.upsert_store_event_history(evs_day1, date_jst="2026-11-05")
        row_d1 = local_db._fetch_all_rows("SELECT first_seen_date, last_seen_date, days_seen FROM store_event_history")[0]
        self.assertEqual(tuple(row_d1), ("2026-11-05", "2026-11-05", 1))

        # Next day crawl increments days_seen, and adding a new event does not erase the previous one
        local_db.upsert_store_event_history(evs_day1, date_jst="2026-11-06")
        evs_day3 = [
            {"brand": "JIS", "store_name": "JIS NAMBA", "title": "冬スイーツビュッフェ女性プレゼント", "gender": "women", "url": "https://jis.bar/"},
        ]
        local_db.upsert_store_event_history(evs_day3, date_jst="2026-11-07")

        summary = local_db.get_event_history_summary(days=3650)
        self.assertEqual(summary["total_archived"], 2)
        self.assertGreaterEqual(summary["by_category"]["mens_discount"], 1)
        self.assertGreaterEqual(summary["by_category"]["ladies_perk"], 1)

    # ------------------------------------------------------------------
    # Pillar 2: Official Arena/Transit Context & Adjacent Format Reference
    # ------------------------------------------------------------------
    def test_pillar2_day_context_and_arena_transit_parsers(self):
        ctx_xmas = arena_calendar.classify_day_context("2026-12-25")
        self.assertEqual(ctx_xmas["season_tag"], "christmas")
        self.assertTrue(ctx_xmas["is_payday_window"])
        self.assertTrue(ctx_xmas["is_pre_holiday"])  # Friday night

        ctx_bonus = arena_calendar.classify_day_context("2026-12-11")
        self.assertTrue(ctx_bonus["is_bonus_window"])
        self.assertEqual(ctx_bonus["season_tag"], "bounenkai_peak")

        # Vantelin Dome multi-year table disambiguation via weekday matching:
        # 2026-11-14 is Saturday (土), whereas 2025-11-14 was Friday (金)
        vantelin_html = """
        <table>
          <tr><th>日程</th><th>イベント</th></tr>
          <tr><td>11/14 (金)</td><td>16:00 18:00 旧年度2025コンサート</td></tr>
          <tr><td>11/14 (土)</td><td>16:00 18:00 ドームツアー2026名古屋公演</td></tr>
        </table>
        """.encode("utf-8")
        parsed_v = arena_calendar.parse_vantelin_dome_html(vantelin_html, ref_year=2026)
        self.assertEqual(len(parsed_v), 1)
        self.assertEqual(parsed_v[0]["event_date"], "2026-11-14")
        self.assertIn("2026名古屋公演", parsed_v[0]["event_title"])

        # JR East official transit HTML parser
        jr_normal_html = (
            '<html><body><ul><li class="traininfo-routes__table__item">'
            '<span>山手線</span><span>平常運転</span>'
            '</li></ul></body></html>'
        ).encode("utf-8")
        tr_ok = arena_calendar.parse_jreast_status_html(jr_normal_html)
        self.assertFalse(tr_ok["tokyo"]["disrupted"])
        self.assertEqual(tr_ok["tokyo"]["delayed_lines"], [])

        jr_delay_html = (
            '<html><body><ul><li class="traininfo-routes__table__item">'
            '<span>山手線</span><span>運転見合わせ（人身事故の影響）</span>'
            '</li></ul></body></html>'
        ).encode("utf-8")
        tr_ng = arena_calendar.parse_jreast_status_html(jr_delay_html)
        self.assertTrue(tr_ng["tokyo"]["disrupted"])
        self.assertTrue(any("山手線" in s for s in tr_ng["tokyo"]["delayed_lines"]))

    def test_pillar2_arena_two_stage_surge_and_logger_compat(self):
        fake_event_map = {
            "tokyo_dome_budokan": [{
                "event_date": "2026-11-07",
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "city_key": "tokyo",
                "event_title": "WINTER DOME LIVE 2026",
                "open_time": "16:00",
                "start_time": "18:00",
                "est_end_hour": 21,
            }]
        }
        fake_weather = {
            "tokyo": {
                "city_label": "東京",
                "temp_c": 14.5,
                "precip_mm": 0.0,
                "weather_code": 1,
                "wind_kmh": 8.0,
                "condition_ja": "晴れ",
                "is_rainy": False,
            }
        }
        stores = [
            {"name": "OLG 新宿", "women": 24, "men": 18, "women_diff": 6},
            {"name": "OLG 上野", "women": 20, "men": 16, "women_diff": 6},
        ]

        with mock.patch.object(arena_calendar, "get_arena_events_for_date", return_value=fake_event_map), \
             mock.patch.object(arena_calendar, "get_city_transit_status", return_value={"tokyo": {"disrupted": True, "delayed_lines": ["山手線（遅れ）"]}}), \
             mock.patch.object(moat_intelligence, "get_city_weather_map", return_value=fake_weather):
            # Stage 1: Afternoon 16:00 -> pre_announce only, NOT surging yet
            dt_afternoon = datetime.datetime(2026, 11, 7, 16, 0)
            sig_pre = moat_intelligence.detect_arena_event_surges(stores, now_jst=dt_afternoon)
            self.assertIn("OLG 新宿", sig_pre)
            self.assertEqual(sig_pre["OLG 新宿"]["phase"], "pre_announce")
            self.assertFalse(sig_pre["OLG 新宿"]["is_surge_now"])

            # Logger compatibility: pre_announce must keep active_arenas_json == '[]'
            snap_pre = moat_intelligence.capture_external_context_snapshot(stores, allow_blocking=False, now_jst=dt_afternoon)
            tokyo_row_pre = next(r for r in snap_pre if r["city_key"] == "tokyo")
            self.assertEqual(tokyo_row_pre["active_arenas_json"], "[]")
            self.assertEqual(tokyo_row_pre["transit_disrupted"], 1)
            self.assertIn("山手線", tokyo_row_pre["transit_lines_json"])

            # Stage 2: Post-show 20:30 with multi-store female jump -> verified_surge
            dt_post = datetime.datetime(2026, 11, 7, 20, 30)
            sig_post = moat_intelligence.detect_arena_event_surges(stores, now_jst=dt_post)
            self.assertEqual(sig_post["OLG 新宿"]["phase"], "verified_surge")
            self.assertTrue(sig_post["OLG 新宿"]["is_surge_now"])

            snap_post = moat_intelligence.capture_external_context_snapshot(stores, allow_blocking=False, now_jst=dt_post)
            tokyo_row_post = next(r for r in snap_post if r["city_key"] == "tokyo")
            self.assertIn("tokyo_dome_budokan", tokyo_row_post["active_arenas_json"])

            fc = moat_intelligence.predict_store_60m_dynamics(
                "OLG 新宿", 24, 18, women_diff=6, men_diff=1,
                arena_signal=sig_post["OLG 新宿"], now_jst=dt_post
            )
            self.assertEqual(fc["arena_phase"], "verified_surge")
            self.assertEqual(fc["arena_event_title"], "WINTER DOME LIVE 2026")

    def test_pillar2_adjacent_format_reference_on_price_comparison(self):
        self.assertGreaterEqual(len(price_comparison.ADJACENT_FORMAT_SPECS), 4)
        resp = self.client.get("/price-comparison")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn('id="adjacent-format-comparison"', html)
        self.assertIn("相席屋", html)
        self.assertIn("THE SINGLE", html)
        self.assertIn("パブリックスタンド", html)
        visible = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", html, flags=re.S)
        self.assertIsNone(FORBIDDEN_STANDALONE_BRAND_RE.search(visible))

    # ------------------------------------------------------------------
    # Pillar 3: First-Party Wait Minutes & Latent Entry Watch Intents
    # ------------------------------------------------------------------
    def test_pillar3_vote_wait_minutes_and_watch_intent_api(self):
        # 1-step vote with wait_minutes=30
        r1 = self.client.post(
            "/api/vote",
            json={"store_name": "オリエンタルラウンジ 新宿", "vote_type": "waiting", "voter_id": "voter_alpha", "wait_minutes": 30},
        )
        self.assertEqual(r1.status_code, 200)
        d1 = r1.get_json()
        self.assertEqual(d1["status"], "success")
        self.assertEqual(d1["votes"]["waiting"], 1)
        self.assertEqual(d1["votes"]["reported_wait_samples"], 1)
        self.assertAlmostEqual(float(d1["votes"]["reported_wait_avg"]), 30.0, places=1)

        # 2-step vote: voter_beta first taps 'waiting', then taps '約15分' -> count stays 2, wait_minutes updated!
        r2a = self.client.post(
            "/api/vote",
            json={"store_name": "オリエンタルラウンジ 新宿", "vote_type": "waiting", "voter_id": "voter_beta"},
        )
        self.assertEqual(r2a.get_json()["votes"]["waiting"], 2)
        self.assertEqual(r2a.get_json()["votes"]["reported_wait_samples"], 1)

        r2b = self.client.post(
            "/api/vote",
            json={"store_name": "オリエンタルラウンジ 新宿", "vote_type": "waiting", "voter_id": "voter_beta", "wait_minutes": 15},
        )
        d2b = r2b.get_json()
        self.assertEqual(d2b["status"], "success")
        self.assertEqual(d2b["reason"], "updated_wait")
        self.assertEqual(d2b["votes"]["waiting"], 2)  # Not double-counted!
        self.assertEqual(d2b["votes"]["reported_wait_samples"], 2)
        self.assertAlmostEqual(float(d2b["votes"]["reported_wait_avg"]), 22.5, places=1)

        # Watch intent API
        rw_ok = self.client.post(
            "/api/watch-intent",
            json={"store_name": "オリエンタルラウンジ 新宿", "target_cond": "female_55", "voter_id": "watcher_1"},
        )
        self.assertEqual(rw_ok.status_code, 200)
        dw = rw_ok.get_json()
        self.assertEqual(dw["status"], "success")
        self.assertEqual(dw["watching_count"], 1)

        rw_bad = self.client.post(
            "/api/watch-intent",
            json={"store_name": "存在しない店舗XYZ", "target_cond": "female_55"},
        )
        self.assertEqual(rw_bad.status_code, 400)

        # Store detail page renders #store-first-party-signals
        app_module.clear_fast_http_caches()
        sd_resp = self.client.get("/store/oriental-lounge-shinjuku")
        self.assertEqual(sd_resp.status_code, 200)
        self.assertIn('id="store-first-party-signals"', sd_resp.data.decode("utf-8"))

    # ------------------------------------------------------------------
    # Pillar 4: Open Primary Stats CSV, Year-End 2026 Whitepaper & AI Feeds
    # ------------------------------------------------------------------
    def test_pillar4_stats_90d_monthly_csv_year_end_whitepaper_and_llms_feeds(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime.now(jst).replace(tzinfo=None)
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for i in range(5):
                ts = (now - datetime.timedelta(days=i + 1)).replace(hour=21, minute=15, second=0).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                    (ts, "OLG 新宿", 30 + i, 18),
                )
                cur.execute(
                    "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                    (ts, "JIS NAMBA", 22 + i, 14),
                )
            conn.commit()
        finally:
            conn.close()

        app_module.clear_fast_http_caches()

        # 1) /stats renders 90-day track record & CSV DataDownload
        r_stats = self.client.get("/stats")
        self.assertEqual(r_stats.status_code, 200)
        html_stats = r_stats.data.decode("utf-8")
        self.assertIn("過去90日間", html_stats)
        self.assertIn("/stats/monthly.csv", html_stats)
        self.assertIn("/stats/year-end-2026", html_stats)

        # 2) /stats/monthly.csv returns clean CC BY 4.0 CSV with zero forbidden brand abbreviations
        r_csv = self.client.get("/stats/monthly.csv")
        self.assertEqual(r_csv.status_code, 200)
        self.assertIn("text/csv", r_csv.headers.get("Content-Type", ""))
        csv_text = r_csv.data.decode("utf-8")
        self.assertIsNone(FORBIDDEN_STANDALONE_BRAND_RE.search(csv_text))
        parsed_rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertGreaterEqual(len(parsed_rows), 2)
        names = {r["store_name"] for r in parsed_rows}
        self.assertIn("オリエンタルラウンジ 新宿", names)
        self.assertIn("JIS（ジス）難波", names)

        # 3) /stats/year-end-2026 renders with zero raw /link/ URLs and full brand compliance
        r_ye = self.client.get("/stats/year-end-2026")
        self.assertEqual(r_ye.status_code, 200)
        html_ye = r_ye.data.decode("utf-8")
        self.assertNotIn('href="/link/', html_ye)
        self.assertIn('data-cushion="1"', html_ye)
        self.assertIn("くわもん（相席ナビ編集長）", html_ye)
        visible_ye = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", html_ye, flags=re.S)
        self.assertIsNone(FORBIDDEN_STANDALONE_BRAND_RE.search(visible_ye))

        # 4) /sitemap.xml and /api/llms-facts.json include the new statistical assets
        r_smap = self.client.get("/sitemap.xml")
        self.assertEqual(r_smap.status_code, 200)
        self.assertIn("https://aiseki-navi.com/stats/year-end-2026", r_smap.data.decode("utf-8"))

        r_facts = self.client.get("/api/llms-facts.json")
        self.assertEqual(r_facts.status_code, 200)
        facts = r_facts.get_json()
        self.assertEqual(facts["open_datasets"]["monthly_csv_url"], "https://aiseki-navi.com/stats/monthly.csv")
        self.assertEqual(facts["open_datasets"]["year_end_2026_whitepaper_url"], "https://aiseki-navi.com/stats/year-end-2026")
        self.assertEqual(facts["forecast_accuracy_90d"]["window_days"], 90)
        facts_raw = json.dumps(facts, ensure_ascii=False)
        self.assertIsNone(FORBIDDEN_STANDALONE_BRAND_RE.search(facts_raw))

    # ------------------------------------------------------------------
    # Claude Fable 5.1 Security & Memory Audit Regression Guards (C-1, H-1, H-2, H-3)
    # ------------------------------------------------------------------
    def test_audit_c1_contiguous_date_range_split_prevents_wide_scan(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime.now(jst).replace(tzinfo=None)
        old_95d = (now - datetime.timedelta(days=95)).replace(hour=21, minute=10, second=0)

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                (old_95d.strftime("%Y-%m-%d %H:%M:%S"), "OLG 新宿", 28, 18),
            )
            conn.commit()
        finally:
            conn.close()

        observed_spans_days = []
        orig_fetch = local_db._fetch_all_rows

        def _spy_fetch(sql_qmark, params=()):
            if "FROM monitor_logs" in sql_qmark and len(params) == 2 and len(str(params[0])) >= 10 and len(str(params[1])) >= 10:
                d0 = datetime.date.fromisoformat(str(params[0])[:10])
                d1 = datetime.date.fromisoformat(str(params[1])[:10])
                observed_spans_days.append((d1 - d0).days)
            return orig_fetch(sql_qmark, params)

        with mock.patch.object(local_db, "_fetch_all_rows", side_effect=_spy_fetch):
            local_db.prune_old_logs(monitor_days=90, vote_days=14)

        self.assertGreaterEqual(len(observed_spans_days), 2)
        for span in observed_spans_days:
            self.assertLessEqual(span, 7, f"Wide full-table range scan detected: {span} days")

    def test_audit_h1_watch_intent_rate_limit_cannot_be_bypassed_by_rotating_voter_id(self):
        statuses = []
        for idx in range(25):
            r = self.client.post(
                "/api/watch-intent",
                json={
                    "store_name": "オリエンタルラウンジ 新宿",
                    "target_cond": "female_55",
                    "voter_id": f"rotated_voter_{idx}",
                },
            )
            statuses.append(r.status_code)
        self.assertEqual(statuses[:20], [200] * 20)
        self.assertEqual(statuses[20:], [429] * 5)
        cnt_info = local_db.get_watch_intent_counts(store_name="OLG 新宿", hours=6)
        self.assertEqual(cnt_info["watching_count"], 20)

    def test_audit_h2_h3_zero_dummy_fallback_and_monthly_csv_server_cache(self):
        # H-2: When resolved_count == 0, /stats/year-end-2026 must NOT show hardcoded '5.8' or '84.0'
        app_module.clear_fast_http_caches()
        r_ye = self.client.get("/stats/year-end-2026")
        self.assertEqual(r_ye.status_code, 200)
        html_ye = r_ye.data.decode("utf-8")
        self.assertIn("検証ログ蓄積中", html_ye)
        self.assertNotIn("±5.8pt", html_ye)
        self.assertNotIn("84.0%", html_ye)

        # H-3: /stats/monthly.csv normalizes arbitrary ?days=45 -> 30 and caches in memory
        with mock.patch.object(local_db, "get_monthly_store_stats_rows", wraps=local_db.get_monthly_store_stats_rows) as spy_csv:
            r1 = self.client.get("/stats/monthly.csv?days=45")
            r2 = self.client.get("/stats/monthly.csv?days=42")
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(spy_csv.call_count, 1)
            self.assertEqual(spy_csv.call_args.kwargs.get("days"), 30)


if __name__ == "__main__":
    unittest.main()
