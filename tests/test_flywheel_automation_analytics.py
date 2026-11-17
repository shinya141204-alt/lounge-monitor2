import datetime
import io
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import analytics
import app as app_module
import column_editorial
import job_state
import pdca_engine
import scheduled_articles
import sns_insights_loop


JST = datetime.timezone(datetime.timedelta(hours=9))


class FlywheelAutomationAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_job_dir = os.environ.get("JOB_STATE_DIR")
        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        with job_state._lock:
            job_state._keys = None
            job_state._kv = None
            job_state._kv_ts = None
            job_state._local_loaded = False
            job_state._sheets_loaded = True
            job_state._turbo_loaded = False
            job_state._neg_cache.clear()
        sns_insights_loop.reset_insights_loop_cache_for_tests()

        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        if self._prev_job_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = self._prev_job_dir
        self._tmpdir.cleanup()

    def test_external_referer_classification_and_full_funnel_cookie_attribution(self):
        self.assertEqual(
            analytics.classify_external_referrer_inflow("https://www.google.com/search?q=相席ラウンジ", "aiseki-navi.com"),
            "ref_google",
        )
        self.assertEqual(
            analytics.classify_external_referrer_inflow("https://m.youtube.com/", "aiseki-navi.com"),
            "ref_youtube",
        )
        self.assertEqual(
            analytics.classify_external_referrer_inflow("https://chatgpt.com/", "aiseki-navi.com"),
            "ref_ai_search",
        )
        # Same-site referer must never overwrite or create an external inflow tag
        self.assertEqual(
            analytics.classify_external_referrer_inflow("https://aiseki-navi.com/columns", "aiseki-navi.com"),
            "",
        )
        self.assertIn("Google", analytics.describe_inflow_tag("ref_google"))
        self.assertIn("YouTube", analytics.describe_inflow_tag("short_bio"))

        recorded_events = []

        def _capture_track(event, label="", _now_jst=None):
            recorded_events.append((event, label))

        with patch.object(analytics, "track", side_effect=_capture_track):
            # Step 1: Organic visitor lands from Google Search onto /columns
            r1 = self.client.get(
                "/columns",
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
                    "Referer": "https://www.google.co.jp/",
                },
            )
            self.assertEqual(r1.status_code, 200)
            self.assertTrue(
                any(ev[0] == "sns_inflow" and ev[1].startswith("ref_google") for ev in recorded_events),
                f"Expected ref_google in {recorded_events}",
            )

            # Step 2: Same visitor taps a 2-step cushion link (/grooming) firing cushion_click beacon + page load
            r_beacon = self.client.post(
                "/api/track",
                json={"event": "cushion_click", "label": "/grooming @ /columns"},
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
                    "Referer": "http://localhost/columns",
                },
            )
            self.assertEqual(r_beacon.status_code, 204)
            r2 = self.client.get(
                "/grooming",
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
                    "Referer": "http://localhost/columns",
                },
            )
            self.assertEqual(r2.status_code, 200)
            self.assertTrue(
                any(ev[0] == "sns_inflow" and ev[1].startswith("cush:ref_google") for ev in recorded_events),
                f"Expected cush:ref_google in {recorded_events}",
            )

            # Step 3: Visitor clicks affiliate redirect /link/mens_skincare_clinic
            r3 = self.client.get(
                "/link/mens_skincare_clinic?from=/grooming",
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
                    "Referer": "http://localhost/grooming",
                },
            )
            self.assertIn(r3.status_code, (301, 302))
            self.assertTrue(
                any(ev[0] == "sns_inflow" and ev[1].startswith("cv:ref_google") for ev in recorded_events),
                f"Expected cv:ref_google in {recorded_events}",
            )
            self.assertTrue(
                any(ev[0] == "sns_inflow" and ev[1].startswith("cvslot:ref_google @") for ev in recorded_events),
                f"Expected cvslot:ref_google in {recorded_events}",
            )

    def test_csv_ingestion_for_gsc_and_youtube_plus_moshimo_3d_preservation(self):
        # 1) Google Search Console CSV (Japanese headers + opportunity query detection)
        gsc_csv = (
            "上位のクエリ,クリック数,表示回数,CTR,掲載順位\n"
            "オリエンタルラウンジ 男女比,12,180,6.67%,3.4\n"
            "相席ラウンジ ドーム ライブ帰り,1,85,1.18%,6.2\n"
            "マッチングアプリ 疲れた 相席,0,44,0%,8.1\n"
        )
        res_gsc = sns_insights_loop.ingest_csv_insights(gsc_csv, platform_hint="auto", filename="クエリ.csv")
        self.assertTrue(res_gsc["ok"])
        self.assertEqual(res_gsc["detected_platform"], "gsc")
        self.assertEqual(res_gsc["queries_ingested"], 3)
        gsc_diag = res_gsc["feedback"]["gsc_diagnostics"]
        self.assertEqual(gsc_diag["data_status"], "ok")
        self.assertEqual(gsc_diag["total_clicks"], 13)
        self.assertEqual(gsc_diag["total_impressions"], 309)
        opp_qs = [q["query"] for q in gsc_diag["opportunity_queries"]]
        self.assertIn("相席ラウンジ ドーム ライブ帰り", opp_qs)
        self.assertIn("マッチングアプリ 疲れた 相席", opp_qs)
        self.assertIn("未回収お宝クエリ", gsc_diag["seo_prompt_directive"])

        # 2) YouTube Studio CSV (Shorts retention metrics)
        yt_csv = (
            "コンテンツ,動画のタイトル,視聴回数,インプレッション数,視聴を選択 (%),平均再生率 (%),平均視聴時間,高評価数,チャンネル登録者\n"
            "abc123xyz,もし相席ラウンジがこの世から消えたら？ #もしもシリーズ #雑学,4820,6900,71.4%,94.2%,0:00:29,142,18\n"
            "def456uvw,もし全員の頭の上に年収が出たら？ #もしもシリーズ #雑学,2310,3800,64.8%,86.5%,0:00:26,67,7\n"
        )
        res_yt = sns_insights_loop.ingest_csv_insights(yt_csv, platform_hint="auto", filename="表データ.csv")
        self.assertTrue(res_yt["ok"])
        self.assertEqual(res_yt["detected_platform"], "youtube_shorts")
        self.assertEqual(res_yt["ingested_posts"], 2)
        yt_diag = res_yt["feedback"]["youtube_shorts_diagnostics"]
        self.assertEqual(yt_diag["data_status"], "ok")
        self.assertEqual(yt_diag["total_views"], 7130)
        self.assertAlmostEqual(yt_diag["avg_stayed_to_watch_pct"], 68.1, places=1)
        self.assertTrue(yt_diag["retention_strong"])

        # 3) Moshimo 3D post registered first, then synced by IG Graph API -> preserves moshimo_ep1_vanished & moshimo_3d
        store = sns_insights_loop._load_store(allow_blocking=True)
        sns_insights_loop._upsert_post_record(
            store,
            channel="reels",
            post_id="1800999888777",
            text="もし相席ラウンジがこの世から消えたら？ 相席ナビ実測データ",
            style="moshimo_ep1_vanished",
            reel_type="moshimo_3d",
            metrics={},
            source="moshimo_queue_auto",
        )
        updated = sns_insights_loop._upsert_post_record(
            store,
            channel="reels",
            post_id="1800999888777",
            text="もし相席ラウンジがこの世から消えたら？ 相席ナビ実測データ",
            style="reel",
            reel_type="live_top5",
            metrics={"views": 3200, "avg_watch_sec": 19.5, "likes": 85, "saves": 24},
            source="ig_graph_api",
        )
        self.assertEqual(updated["style"], "moshimo_ep1_vanished")
        self.assertEqual(updated["reel_type"], "moshimo_3d")

        # Duration-aware score: 19.5s on a 22s target scores high on moshimo_3d
        q_score = sns_insights_loop._compute_reel_quality_score(
            {"views": 3200, "avg_watch_sec": 19.5, "saves": 24, "shares": 10},
            median_views=1500.0,
            reel_type="moshimo_3d",
        )
        self.assertGreater(q_score, 70.0)

    def test_scheduled_seo_columns_gating_and_100pt_desk_audit(self):
        audit = column_editorial.audit_existing_columns(force=True)
        self.assertEqual(audit["total_bare_oriental"], 0)
        by_slug = {r["slug"]: r for r in audit["columns"]}
        self.assertIn("dome-live-after-surge", by_slug)
        self.assertIn("app-fatigue-vs-lounge", by_slug)
        self.assertEqual(by_slug["dome-live-after-surge"]["desk_score"], 100)
        self.assertEqual(by_slug["app-fatigue-vs-lounge"]["desk_score"], 100)

        before_dt = datetime.datetime(2026, 11, 15, 12, 0, 0)
        after_dt = datetime.datetime(2026, 11, 22, 12, 0, 0)

        with patch.object(scheduled_articles, "_coerce_naive_jst", return_value=before_dt):
            app_module.clear_fast_http_caches()
            resp_pre = self.client.get("/guide/dome-live-after-surge")
            self.assertEqual(resp_pre.status_code, 404)
            with self.client.session_transaction() as sess:
                sess["is_staff"] = True
            resp_staff = self.client.get("/guide/dome-live-after-surge")
            self.assertEqual(resp_staff.status_code, 200)

        with self.client.session_transaction() as sess:
            sess.clear()

        with patch.object(scheduled_articles, "_coerce_naive_jst", return_value=after_dt):
            app_module.clear_fast_http_caches()
            for path in ("/guide/dome-live-after-surge", "/guide/app-fatigue-vs-lounge"):
                resp_pub = self.client.get(path)
                self.assertEqual(resp_pub.status_code, 200)
                body = resp_pub.get_data(as_text=True)
                self.assertNotIn("/link/", body)
                self.assertIn("オリエンタルラウンジ", body)

    def test_master_ui_and_csv_api_endpoints(self):
        # Unauthorized check
        unauth = self.client.post("/api/pdca/sns-insights/ingest-csv", json={"csv_text": "a,b\n1,2"})
        self.assertEqual(unauth.status_code, 401)

        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True

        # 1) /staff/sns-editorial?channel=instagram renders #moshimo-queue-section with all 10 episodes
        r_sns = self.client.get("/staff/sns-editorial?channel=instagram")
        self.assertEqual(r_sns.status_code, 200)
        html_sns = r_sns.get_data(as_text=True)
        self.assertIn("moshimo-queue-section", html_sns)
        self.assertIn("ep10_solo_night", html_sns)

        # 2) Toggle evergreen_recycle via /api/moshimo-reel-queue
        r_tog = self.client.post(
            "/api/moshimo-reel-queue",
            json={"action": "toggle", "evergreen_recycle": True},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_tog.status_code, 200)
        self.assertTrue(r_tog.get_json()["evergreen_recycle"])

        # 3) Multipart CSV upload to /api/pdca/sns-insights/ingest-csv
        csv_bytes = (
            "上位のクエリ,クリック数,表示回数,CTR,掲載順位\n"
            "相席ナビ 梅田,9,120,7.5%,2.1\n"
            "オリエンタルラウンジ 料金 女性,1,65,1.54%,5.4\n"
        ).encode("utf-8-sig")
        r_csv = self.client.post(
            "/api/pdca/sns-insights/ingest-csv",
            data={"platform": "auto", "csv_file": (io.BytesIO(csv_bytes), "クエリ.csv")},
            content_type="multipart/form-data",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_csv.status_code, 200)
        j_csv = r_csv.get_json()
        self.assertTrue(j_csv["ok"])
        self.assertEqual(j_csv["ingest"]["detected_platform"], "gsc")
        self.assertIn("未回収お宝クエリ", j_csv["active_directives"].get("gsc_seo_directive", ""))

        # 4) /staff/pdca & /staff/analytics render 4-Gear Flywheel & Referer Cohort UI
        r_pdca = self.client.get("/staff/pdca")
        self.assertEqual(r_pdca.status_code, 200)
        html_pdca = r_pdca.get_data(as_text=True)
        self.assertIn("pdca-flywheel", html_pdca)
        self.assertIn("insights-csv-form", html_pdca)
        self.assertIn("お宝枠", html_pdca)

        r_ana = self.client.get("/staff/analytics")
        self.assertEqual(r_ana.status_code, 200)
        self.assertIn("SNS・ショート動画・自然検索", r_ana.get_data(as_text=True))

        # 5) Multipart Google Search Console ZIP upload to /api/pdca/sns-insights/ingest-csv
        import zipfile
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "クエリ.csv",
                "上位のクエリ,クリック数,表示回数,CTR,掲載順位\n"
                "アソビバー 人数,2,4,50%,11\n"
                "ag umeda レビュー,0,2,0%,10.5\n"
                "オリエンタル ラウンジ 長崎 レビュー,0,2,0%,12\n",
            )
            zf.writestr(
                "ページ.csv",
                "上位のページ,クリック数,表示回数,CTR,掲載順位\n"
                "https://aiseki-navi.com/store/asobibar-shibuya,2,5,40%,20.8\n"
                "https://aiseki-navi.com/guide/dress-code-cleanliness,1,13,7.69%,5.92\n",
            )
            zf.writestr(
                "デバイス.csv",
                "デバイス,クリック数,表示回数,CTR,掲載順位\n"
                "モバイル,5,100,5%,8.36\n"
                "PC,1,41,2.44%,39.66\n",
            )
        r_zip = self.client.post(
            "/api/pdca/sns-insights/ingest-csv",
            data={"platform": "auto", "csv_file": (io.BytesIO(zbuf.getvalue()), "Performance-on-Search.zip")},
            content_type="multipart/form-data",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_zip.status_code, 200)
        j_zip = r_zip.get_json()
        self.assertTrue(j_zip["ok"])
        self.assertEqual(j_zip["ingest"]["source"], "gsc_zip")
        gsc_d = j_zip["closed_loop_feedback"]["gsc_diagnostics"]
        self.assertEqual(gsc_d["total_clicks"], 6)
        self.assertEqual(gsc_d["total_impressions"], 141)
        self.assertEqual(gsc_d["opportunity_queries"][0]["query"], "ag umeda レビュー")
        self.assertNotIn("ラウンジ ラウンジ", gsc_d["seo_prompt_directive"])


if __name__ == "__main__":
    unittest.main()
