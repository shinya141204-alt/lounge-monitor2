"""曜日×時間帯別 実測ヒートマップ（embed_widgets / /embed/heatmap / /api/embed/heatmap.json / /stats#heatmap）回帰テスト。

- 一時 SQLite DB（tempfile.TemporaryDirectory）＋ `local_db.DB_PATH` パッチで本番・ローカル `lounge_data.db` を一切汚染しない。
- PostgreSQL は `turbo_store.is_postgres_active` を False 固定にして無効化し、非表示ブランド/店舗設定も空集合に固定する（決定論的）。
- 日付はすべて「今日（JST）から3日以上前」の相対日付で生成し、90日集計ウィンドウ内に必ず収まるようにする。
"""
import datetime
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import app as app_module  # noqa: E402
import embed_widgets  # noqa: E402
import local_db  # noqa: E402
import turbo_store  # noqa: E402

try:
    import ops_governance  # noqa: E402
except Exception:  # pragma: no cover
    ops_governance = None
try:
    import analytics  # noqa: E402
except Exception:  # pragma: no cover
    analytics = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JST = datetime.timezone(datetime.timedelta(hours=9))
STORE = "OLG 新宿"
STORE_SLUG = "oriental-lounge-shinjuku"
ATTRIBUTION = "出典: 相席ナビ編集部（https://aiseki-navi.com/stats） / CC BY 4.0"
NG_BRAND_PATTERNS = (
    re.compile(r"OLG "),
    re.compile(r"YATAKOI"),
    re.compile(r"オリラジ"),
    re.compile(r"オリエンタル(?!ラウンジ)"),
)


def _recent_weekday(weekday: int, weeks_back: int = 0) -> datetime.date:
    """今日（JST）から3日以上前で、指定曜日（月=0）に当たる直近のカレンダー日付（weeks_back 週さかのぼる）。"""
    d = datetime.datetime.now(_JST).date() - datetime.timedelta(days=3 + 7 * weeks_back)
    while d.weekday() != weekday:
        d -= datetime.timedelta(days=1)
    return d


def _cells_by_key(payload: dict) -> dict:
    return {(c["dow"], c["hour"]): c for c in payload["cells"]}


class _HeatmapDbTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "embed_heatmap_test.db")
        patches = [
            mock.patch.object(local_db, "DB_PATH", self.db_path),
            mock.patch.object(turbo_store, "is_postgres_active", return_value=False),
            mock.patch.object(embed_widgets, "_hidden_sets", return_value=(set(), set())),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._reset_all_caches()
        local_db.init_db()
        local_db._ensure_moat_rollup_schema()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self._reset_all_caches()
        if analytics is not None and hasattr(analytics, "reset_for_tests"):
            analytics.reset_for_tests()
        self._tmp.cleanup()

    def _reset_all_caches(self):
        embed_widgets.clear_heatmap_cache()
        if hasattr(local_db, "reset_caches_for_tests"):
            local_db.reset_caches_for_tests()
        app_module.clear_fast_http_caches()
        if ops_governance is not None and hasattr(ops_governance, "invalidate_governance_cache"):
            ops_governance.invalidate_governance_cache()

    # --- direct SQLite writers (temp DB only) ---------------------------------
    def _insert_logs(self, rows):
        """rows: iterable of (timestamp 'YYYY-MM-DD HH:MM:SS', store_name, women, men)."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO monitor_logs (timestamp, store_name, men, women, source) VALUES (?, ?, ?, ?, ?)",
                [(ts, st, int(m), int(w), "unittest") for ts, st, w, m in rows],
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_rollup(self, date_jst: str, hour: int, store_name: str, sample_count: int, avg_w: float, avg_m: float):
        total = avg_w + avg_m
        avg_wpct = (avg_w / total) * 100.0 if total > 0 else 0.0
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO store_hourly_rollups "
                "(date_jst, hour_jst, business_date, store_name, sample_count, avg_women, avg_men, avg_wpct) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    date_jst,
                    int(hour),
                    local_db._business_date_for_ymd_hour(date_jst, int(hour)),
                    store_name,
                    int(sample_count),
                    float(avg_w),
                    float(avg_m),
                    float(avg_wpct),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _fresh(self):
        """DB 書き込み後に集計キャッシュとスレッドローカル読取ハンドルをリセットする。"""
        embed_widgets.clear_heatmap_cache()
        if hasattr(local_db, "reset_caches_for_tests"):
            local_db.reset_caches_for_tests()
        app_module.clear_fast_http_caches()


class TestHeatmapAggregation(_HeatmapDbTestBase):
    def test_late_night_hours_attribute_to_previous_business_day(self):
        """カレンダー月曜01:00（日曜深夜25時）→ sun 行 hour=1、土曜00:30（金曜深夜24時半）→ fri 行 hour=0。"""
        rows = []
        for wb in (0, 1):
            mon = _recent_weekday(0, wb).isoformat()
            sat = _recent_weekday(5, wb).isoformat()
            rows.append((f"{mon} 01:00:00", STORE, 6, 4))
            rows.append((f"{sat} 00:30:00", STORE, 5, 5))
        self._insert_logs(rows)
        self._fresh()

        payload = embed_widgets.get_heatmap_payload(store=STORE_SLUG)
        self.assertEqual(len(payload["cells"]), 77)
        cells = _cells_by_key(payload)

        sun1 = cells[("sun", 1)]
        self.assertEqual(sun1["n_buckets"], 2)
        self.assertEqual(sun1["status"], "ok")
        self.assertEqual(sun1["avg_women"], 6.0)
        self.assertEqual(sun1["avg_men"], 4.0)
        self.assertEqual(sun1["avg_total"], 10.0)
        self.assertEqual(sun1["women_ratio_pct"], 60.0)
        self.assertTrue(sun1["women_majority"])

        fri0 = cells[("fri", 0)]
        self.assertEqual(fri0["n_buckets"], 2)
        self.assertEqual(fri0["women_ratio_pct"], 50.0)

        # カレンダー上の曜日（月曜・土曜）側には一切帰属しない
        self.assertEqual(cells[("mon", 1)]["n_buckets"], 0)
        self.assertEqual(cells[("sat", 0)]["n_buckets"], 0)
        self.assertEqual(payload["meta"]["total_buckets"], 4)

    def test_ratio_requires_four_guests_and_insufficient_cells_are_null(self):
        """(women+men)>=4 未満は来店人数にのみ算入。MIN_CELL_BUCKETS 未満のセルは status=insufficient かつ数値すべて None。"""
        rows = []
        for wb in (0, 1):
            tue = _recent_weekday(1, wb).isoformat()
            rows.append((f"{tue} 19:00:00", STORE, 2, 0))  # 計2名 < 4名
        wed = _recent_weekday(2, 0).isoformat()
        rows.append((f"{wed} 21:00:00", STORE, 10, 10))  # 1時間枠のみ（店舗スコープ最低2枠に未達）
        self._insert_logs(rows)
        self._fresh()

        payload = embed_widgets.get_heatmap_payload(store=STORE_SLUG)
        self.assertEqual(len(payload["cells"]), 77)
        cells = _cells_by_key(payload)

        tue19 = cells[("tue", 19)]
        self.assertEqual(tue19["n_buckets"], 2)
        self.assertEqual(tue19["status"], "ok")
        self.assertEqual(tue19["avg_total"], 2.0)
        self.assertEqual(tue19["avg_women"], 2.0)
        self.assertEqual(tue19["avg_men"], 0.0)
        self.assertEqual(tue19["n_ratio"], 0)
        self.assertIsNone(tue19["women_ratio_pct"])
        self.assertEqual(tue19["ratio_band"], "na")
        self.assertFalse(tue19["women_majority"])

        min_store = embed_widgets.MIN_CELL_BUCKETS["store"]
        wed21 = cells[("wed", 21)]
        self.assertLess(wed21["n_buckets"], min_store)
        self.assertEqual(wed21["status"], "insufficient")
        for k in ("women_ratio_pct", "avg_women", "avg_men", "avg_total"):
            self.assertIsNone(wed21[k], f"{k} must be None for insufficient cell (no dummy values)")
        self.assertEqual(wed21["ratio_band"], "na")
        self.assertEqual(wed21["headcount_band"], "na")

        for c in payload["cells"]:
            if c["status"] != "ok":
                for k in ("women_ratio_pct", "avg_women", "avg_men", "avg_total"):
                    self.assertIsNone(c[k], f"dummy value leaked in insufficient cell {c['dow']}/{c['hour']}: {k}={c[k]}")
        self.assertEqual(payload["peaks"]["ratio_top"], [])

        # 全国スコープ（最低5枠）では同じデータでも全セルがサンプル不足になり、推測値は出ない
        nat = embed_widgets.get_heatmap_payload(scope="all")
        self.assertEqual(len(nat["cells"]), 77)
        self.assertEqual(nat["meta"]["data_status"], "insufficient")
        self.assertTrue(all(c["status"] == "insufficient" for c in nat["cells"]))
        self.assertTrue(all(c["women_ratio_pct"] is None for c in nat["cells"]))
        self.assertIn("検証ログ蓄積中", nat["embed"]["markdown"])

    def test_ratio_peaks_prefer_active_hours_and_fall_back_when_absent(self):
        """ratio_top は1店舗平均10名以上の実戦稼働枠を優先し、該当枠が無いときのみ全比率セルへフォールバックする。"""
        rows = []
        for wb in (0, 1):
            thu = _recent_weekday(3, wb).isoformat()
            rows.append((f"{thu} 18:00:00", STORE, 3, 1))  # 計4名・女性75%（極小人数枠）
        self._insert_logs(rows)
        self._fresh()
        only_small = embed_widgets.get_heatmap_payload(store=STORE_SLUG)
        self.assertEqual(len(only_small["peaks"]["ratio_top"]), 1)
        self.assertEqual(only_small["peaks"]["ratio_top"][0]["dow"], "thu")
        self.assertEqual(only_small["peaks"]["ratio_top"][0]["hour"], 18)

        rows = []
        for wb in (0, 1):
            fri = _recent_weekday(4, wb).isoformat()
            rows.append((f"{fri} 22:00:00", STORE, 12, 10))  # 計22名・女性54.5%
        self._insert_logs(rows)
        self._fresh()
        payload = embed_widgets.get_heatmap_payload(store=STORE_SLUG, metric="ratio")
        top = payload["peaks"]["ratio_top"]
        self.assertEqual(len(top), 1)
        self.assertEqual((top[0]["dow"], top[0]["hour"]), ("fri", 22))
        self.assertEqual(top[0]["women_ratio_pct"], 54.5)
        self.assertGreaterEqual(top[0]["avg_total"], 10.0)
        # 極小人数枠のセル自体は推測なしの実測値として表に残る
        self.assertEqual(_cells_by_key(payload)[("thu", 18)]["women_ratio_pct"], 75.0)
        self.assertIn("金曜 22時", payload["embed"]["markdown"])

    def test_rollup_and_raw_logs_are_deduplicated_per_bucket(self):
        """同一 (date_jst, hour_jst, store) はロールアップのみ1回計上。ロールアップに無い時間枠は monitor_logs から補完。"""
        d_roll = _recent_weekday(3, 0).isoformat()
        d_raw = _recent_weekday(3, 1).isoformat()
        self._insert_rollup(d_roll, 21, STORE, sample_count=60, avg_w=12.0, avg_m=8.0)
        # 同一バケットの生ログ（二重計上されてはならない・値も大きく異なる）
        self._insert_logs([
            (f"{d_roll} 21:05:00", STORE, 2, 18),
            (f"{d_roll} 21:30:00", STORE, 2, 18),
            (f"{d_raw} 21:10:00", STORE, 10, 10),  # ロールアップ未登録の時間枠 → 補完
        ])
        self._fresh()

        agg = embed_widgets._get_aggregates(90)
        self.assertEqual(agg["total_buckets"], 2)
        self.assertEqual(agg["rollup_buckets"], 1)
        self.assertEqual(agg["raw_buckets"], 1)

        payload = embed_widgets.get_heatmap_payload(store=STORE_SLUG)
        thu21 = _cells_by_key(payload)[("thu", 21)]
        self.assertEqual(thu21["n_buckets"], 2)
        self.assertEqual(thu21["n_snapshots"], 61)
        self.assertEqual(thu21["avg_women"], 11.0)
        self.assertEqual(thu21["avg_men"], 9.0)
        self.assertEqual(thu21["women_ratio_pct"], 55.0)
        self.assertEqual(payload["meta"]["total_buckets"], 2)

    def test_empty_result_is_never_cached_and_data_hits_cache_afterwards(self):
        """0件時はキャッシュに残らず、直後の挿入が即反映される。実データがある場合は15分キャッシュにヒットする。"""
        empty = embed_widgets.get_heatmap_payload(scope="all")
        self.assertEqual(empty["meta"]["total_buckets"], 0)
        self.assertEqual(empty["meta"]["data_status"], "insufficient")
        self.assertEqual(len(empty["cells"]), 77)
        with embed_widgets._CACHE_LOCK:
            self.assertEqual(len(embed_widgets._CACHE), 0)
        self.assertIsNone(embed_widgets.peek_cached_total_buckets(90))

        recent_d = (datetime.datetime.now(_JST).date() - datetime.timedelta(days=2)).isoformat()
        self._insert_logs([(f"{recent_d} 22:00:00", STORE, 12, 10)])
        if hasattr(local_db, "reset_caches_for_tests"):
            local_db.reset_caches_for_tests()  # 読取ハンドルのみリセット（集計キャッシュはあえて触らない）
        filled = embed_widgets.get_heatmap_payload(scope="all")
        self.assertEqual(filled["meta"]["total_buckets"], 1)
        self.assertEqual(embed_widgets.peek_cached_total_buckets(90), 1)

        with mock.patch.object(
            embed_widgets, "_load_store_slot_aggregates", wraps=embed_widgets._load_store_slot_aggregates
        ) as spy:
            again = embed_widgets.get_heatmap_payload(scope="all")
            embed_widgets.get_heatmap_payload(scope="shinjuku")
            embed_widgets.get_heatmap_payload(store=STORE_SLUG, metric="headcount")
        self.assertEqual(spy.call_count, 0, "populated aggregates must be served from the 15-minute cache")
        self.assertEqual(again["meta"]["total_buckets"], 1)
        self.assertEqual(embed_widgets.CACHE_TTL_SEC, 900.0)


class TestHeatmapScopes(_HeatmapDbTestBase):
    def test_scope_resolution_for_national_area_and_store(self):
        for key in ("all", "shinjuku", "shibuya", "ebisu", "umeda"):
            sc = embed_widgets.resolve_heatmap_scope(scope=key)
            self.assertTrue(sc["raw_stores"], f"scope {key} resolved to zero stores")
            self.assertEqual(sc["type"], "all" if key == "all" else "area")
            for raw in sc["raw_stores"]:
                self.assertFalse(local_db._is_korea_store_name(raw), f"Korean store leaked into {key}: {raw}")

        self.assertIn(STORE, embed_widgets.resolve_heatmap_scope(scope="shinjuku")["raw_stores"])
        shibuya = embed_widgets.resolve_heatmap_scope(scope="shibuya")["raw_stores"]
        ebisu = embed_widgets.resolve_heatmap_scope(scope="ebisu")["raw_stores"]
        self.assertIn("OLG 渋谷本店", shibuya)
        self.assertNotIn("OLG 恵比寿", shibuya)
        self.assertIn("OLG 恵比寿", ebisu)
        self.assertNotIn("OLG 渋谷本店", ebisu)
        self.assertIn("OLG 大阪駅前", embed_widgets.resolve_heatmap_scope(scope="umeda")["raw_stores"])

        st = embed_widgets.resolve_heatmap_scope(store=STORE_SLUG)
        self.assertEqual(st["type"], "store")
        self.assertEqual(st["raw_stores"], [STORE])
        self.assertEqual(st["label"], "オリエンタルラウンジ 新宿")

        scopes = embed_widgets.list_heatmap_scopes()
        self.assertEqual(scopes[0]["key"], "all")
        self.assertIn("shinjuku", [s["key"] for s in scopes])

        for bad_store in ("oriental-lounge-seoul-gangnam", "no-such-store", "../etc/passwd", "OLG 新宿"):
            with self.assertRaises(LookupError):
                embed_widgets.resolve_heatmap_scope(store=bad_store)
        for bad_scope in ("atlantis", "../../etc", "<script>"):
            with self.assertRaises(LookupError):
                embed_widgets.resolve_heatmap_scope(scope=bad_scope)

    def test_unknown_or_korean_targets_return_404_on_both_endpoints(self):
        for path in ("/embed/heatmap", "/api/embed/heatmap.json"):
            for q in ("store=oriental-lounge-seoul-gangnam", "store=no-such-store", "scope=atlantis"):
                resp = self.client.get(f"{path}?{q}&staff_test=1")
                try:
                    self.assertEqual(resp.status_code, 404, f"{path}?{q} returned {resp.status_code}")
                finally:
                    resp.close()

        resp = self.client.get(f"/api/embed/heatmap.json?store={STORE_SLUG}&staff_test=1")
        try:
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["scope"]["type"], "store")
            self.assertEqual(data["scope"]["store_slug"], STORE_SLUG)
            self.assertNotIn("raw_stores", data["scope"])
        finally:
            resp.close()


class TestHeatmapHttpAndSnippets(_HeatmapDbTestBase):
    def _assert_no_ng_brand(self, text: str, where: str):
        for pat in NG_BRAND_PATTERNS:
            self.assertIsNone(pat.search(text), f"NG brand notation {pat.pattern!r} found in {where}")

    def test_embed_page_headers_attribution_and_no_affiliate_links(self):
        resp = self.client.get("/embed/heatmap?scope=all&theme=light&metric=ratio&staff_test=1")
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("frame-ancestors *", resp.headers.get("Content-Security-Policy", ""))
            html = resp.get_data(as_text=True)
        finally:
            resp.close()
        self.assertNotIn("/link/", html)
        self.assertIn(ATTRIBUTION, html)
        self.assertIn('data-theme="light"', html)
        self._assert_no_ng_brand(html, "/embed/heatmap")

    def test_json_api_cors_and_public_payload_hygiene(self):
        resp = self.client.get("/api/embed/heatmap.json?scope=all&staff_test=1")
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            data = resp.get_json()
            body = resp.get_data(as_text=True)
        finally:
            resp.close()
        self.assertEqual(len(data["cells"]), 77)
        self.assertEqual(data["meta"]["attribution"], ATTRIBUTION)
        self.assertNotIn("/link/", body)
        self._assert_no_ng_brand(json.dumps(data, ensure_ascii=False), "/api/embed/heatmap.json")

    def test_build_embed_snippets_seo_contract(self):
        payload = embed_widgets.get_heatmap_payload(scope="all")
        for theme in ("light", "dark"):
            sn = embed_widgets.build_embed_snippets(payload, theme=theme, metric="ratio")
            html = sn["html"]
            self.assertIn("<iframe", html)
            self.assertIn('title="', html)
            self.assertIn("?scope=all", html)
            self.assertIn(f"theme={theme}", html)
            self.assertIn('<a href="https://aiseki-navi.com/stats">', html)
            self.assertNotIn('scrolling="no"', html)
            self.assertNotIn('rel="nofollow"', html)
            self.assertNotIn("/link/", html)
            self.assertIn("https://aiseki-navi.com/stats", sn["markdown"])
            self.assertIn("CC BY 4.0", sn["markdown"])
            self._assert_no_ng_brand(html + sn["markdown"], f"snippets[{theme}]")

        store_payload = embed_widgets.get_heatmap_payload(store=STORE_SLUG)
        st_sn = embed_widgets.build_embed_snippets(store_payload, theme="light", metric="headcount")
        self.assertIn(f"?store={STORE_SLUG}", st_sn["html"])
        self.assertIn("metric=headcount", st_sn["html"])
        self.assertIn(f'href="https://aiseki-navi.com/store/{STORE_SLUG}"', st_sn["html"])
        self.assertNotIn("/link/", st_sn["html"])
        self._assert_no_ng_brand(st_sn["html"] + st_sn["markdown"], "store snippets")

    def test_stats_page_renders_heatmap_section_and_embed_builder(self):
        resp = self.client.get("/stats?staff_test=1")
        try:
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
        finally:
            resp.close()
        for needle in (
            'id="heatmap"',
            'href="#heatmap"',
            "曜日×時間帯別 実測男女比インタラクティブ・ヒートマップ",
            'id="hm-scope-select"',
            'id="hm-store-select"',
            "指定なし（エリア全体を集計）",
            'role="tablist"',
            'data-metric="ratio"',
            'data-metric="headcount"',
            'aria-label="曜日×時間帯別 実測ヒートマップ表"',
            'role="tooltip"',
            "埋め込みウィジェット ＆ 出典コード発行ビルダー",
            'id="hmx-code-html"',
            'id="hmx-code-md"',
            "HTMLコードをコピー",
            "Markdownをコピー",
            'value="light"',
            'value="dark"',
            "https://aiseki-navi.com/api/embed/heatmap.json?scope=all",
            "参考波形モデル ｜ 18:00〜翌05:00傾向",
        ):
            self.assertIn(needle, html, f"/stats missing {needle!r}")
        self.assertNotIn("/link/", html)
        self.assertLess(html.index('id="heatmap"'), html.index('id="stats-simulator"'))
        # 店舗セレクタは正式SEO名称のみ（初期リスト＝全国スコープの国内店舗）
        self.assertIn(f'<option value="{STORE_SLUG}">オリエンタルラウンジ 新宿</option>', html)
        self.assertNotIn("oriental-lounge-seoul-gangnam", html)
        section = html[html.index('id="heatmap"'):html.index('id="stats-simulator"')]
        self._assert_no_ng_brand(section, "/stats#heatmap section")
        # 空データ時も「検証ログ蓄積中」で安全に描画される（推測値なし）
        self.assertIn("検証ログ蓄積中", section)

    def test_stats_page_survives_payload_failure(self):
        with mock.patch.object(embed_widgets, "get_heatmap_payload", side_effect=RuntimeError("db down")):
            app_module.clear_fast_http_caches()
            resp = self.client.get("/stats?staff_test=1")
            try:
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
            finally:
                resp.close()
        self.assertIn('id="heatmap"', html)
        self.assertIn("検証ログ蓄積中", html)
        self.assertEqual(html.count('<td data-i="'), 77)

    def test_llms_facts_keeps_schema_and_exposes_heatmap_urls(self):
        resp = self.client.get("/api/llms-facts.json?staff_test=1")
        try:
            self.assertEqual(resp.status_code, 200)
            facts = json.loads(resp.get_data(as_text=True))
            body = resp.get_data(as_text=True)
        finally:
            resp.close()
        self.assertEqual(facts["schema_version"], "2.1")
        for key in ("site_name", "canonical_url", "license", "open_datasets", "dataset_summary", "brands", "areas", "faqs", "stores"):
            self.assertIn(key, facts)
        od = facts["open_datasets"]
        for key in ("stats_whitepaper_url", "monthly_csv_url", "year_end_2026_whitepaper_url", "license"):
            self.assertIn(key, od)
        self.assertEqual(od["embed_heatmap_widget_url"], "https://aiseki-navi.com/embed/heatmap")
        self.assertEqual(od["embed_heatmap_json_api_url"], "https://aiseki-navi.com/api/embed/heatmap.json?scope=all")
        self.assertNotIn("/link/", body)

    def test_days_snapping_prevents_cache_churn_and_propagates_non_default_days(self):
        """任意の days 入力は {7, 30, 90} に丸められ、連番 days によるキャッシュ破棄が起きない。"""
        for raw_in, expected in ((1, 7), (7, 7), (14, 7), (25, 30), (30, 30), (50, 30), (65, 90), (999, 90), ("bad", 90)):
            self.assertEqual(embed_widgets.clamp_days(raw_in), expected)

        recent_d = (datetime.datetime.now(_JST).date() - datetime.timedelta(days=2)).isoformat()
        self._insert_logs([(f"{recent_d} 22:00:00", STORE, 12, 10)])
        self._fresh()

        with mock.patch.object(
            embed_widgets, "_load_store_slot_aggregates", wraps=embed_widgets._load_store_slot_aggregates
        ) as spy:
            for d in range(7, 18):
                embed_widgets.get_heatmap_payload(scope="all", days=d)
        self.assertEqual(spy.call_count, 1, "arbitrary days=7..17 must collapse onto the single 7-day cache entry")

        p30 = embed_widgets.get_heatmap_payload(scope="all", days=30)
        self.assertIn("days=30", p30["urls"]["embed"])
        self.assertIn("days=30", p30["urls"]["json"])
        self.assertIn("days=30", p30["embed"]["iframe_src"])

        p90 = embed_widgets.get_heatmap_payload(scope="all", days=90)
        self.assertNotIn("days=", p90["urls"]["embed"])
        self.assertNotIn("days=", p90["urls"]["json"])

    def test_stale_while_revalidate_serves_stale_when_compute_lock_is_held(self):
        """TTL 失効直後に別スレッドが再計算ロック保持中でも、後続スレッドはブロックせず既存実データを返す。"""
        recent_d = (datetime.datetime.now(_JST).date() - datetime.timedelta(days=2)).isoformat()
        self._insert_logs([(f"{recent_d} 22:00:00", STORE, 12, 10)])
        self._fresh()
        first = embed_widgets.get_heatmap_payload(scope="all")
        self.assertEqual(first["meta"]["total_buckets"], 1)

        with embed_widgets._CACHE_LOCK:
            for k, (ts, data) in list(embed_widgets._CACHE.items()):
                embed_widgets._CACHE[k] = (ts - embed_widgets.CACHE_TTL_SEC - 5.0, data)

        acquired = embed_widgets._COMPUTE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with mock.patch.object(embed_widgets, "_load_store_slot_aggregates") as spy:
                stale_resp = embed_widgets.get_heatmap_payload(scope="all")
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(stale_resp["meta"]["total_buckets"], 1)
        finally:
            embed_widgets._COMPUTE_LOCK.release()

    def test_embed_html_preserves_public_cache_control_and_never_pollutes_human_pv(self):
        """/embed/heatmap はデータ存在時に public キャッシュを維持し、スタッフ時は private 降格、人間PVも汚染しない。"""
        recent_d = (datetime.datetime.now(_JST).date() - datetime.timedelta(days=2)).isoformat()
        self._insert_logs([(f"{recent_d} 22:00:00", STORE, 12, 10)])
        self._fresh()

        ua_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0"}
        with mock.patch.object(analytics, "track") as track_spy:
            resp = self.client.get("/embed/heatmap?scope=all", headers=ua_headers)
            try:
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers.get("Cache-Control"), "public, max-age=900")
                self.assertIsNone(resp.headers.get("X-Frame-Options"))
                self.assertIn("frame-ancestors *", resp.headers.get("Content-Security-Policy", ""))
            finally:
                resp.close()
        track_spy.assert_not_called()

        non_embed = self.client.get("/about?staff_test=1")
        try:
            self.assertEqual(non_embed.headers.get("X-Frame-Options"), "SAMEORIGIN")
            self.assertIn("frame-ancestors 'self'", non_embed.headers.get("Content-Security-Policy", ""))
        finally:
            non_embed.close()

        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
        staff_resp = self.client.get("/embed/heatmap?scope=all&staff_test=1")
        try:
            self.assertNotIn("public", staff_resp.headers.get("Cache-Control", ""))
        finally:
            staff_resp.close()

    def test_embed_endpoints_enforce_ip_rate_limit(self):
        """非 staff_test リクエストで IP スライディングレート制限超過時は 429 + Retry-After を返す。"""
        with mock.patch.object(turbo_store, "check_sliding_rate_limit", return_value=False):
            r_html = self.client.get("/embed/heatmap?scope=all")
            try:
                self.assertEqual(r_html.status_code, 429)
                self.assertEqual(r_html.headers.get("Retry-After"), "60")
                self.assertEqual(r_html.headers.get("Cache-Control"), "no-store")
            finally:
                r_html.close()

            r_json = self.client.get("/api/embed/heatmap.json?scope=all")
            try:
                self.assertEqual(r_json.status_code, 429)
                self.assertEqual(r_json.headers.get("Retry-After"), "60")
                self.assertEqual(r_json.headers.get("Access-Control-Allow-Origin"), "*")
            finally:
                r_json.close()


class TestStatsTemplateStaticAudit(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, "templates", "stats_report.html"), "r", encoding="utf-8") as fp:
            self.src = fp.read()

    def test_no_affiliate_links_emoji_or_neon_gradients(self):
        self.assertNotIn("/link/", self.src)
        emoji_re = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
        for tag in ("h1", "h2", "h3", "button", "th"):
            for inner in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", self.src, flags=re.S):
                self.assertIsNone(emoji_re.search(inner), f"emoji in <{tag}>: {inner.strip()[:60]}")
        self.assertIsNone(re.search(r"linear-gradient\([^)]*(?:#ec4899|#8b5cf6|#f43f5e)", self.src, flags=re.I))

    def test_heatmap_css_variables_defined_for_dark_and_light(self):
        used = set(re.findall(r"var\(\s*(--hmx-[a-zA-Z0-9_-]+)", self.src))
        self.assertTrue(used)
        dark_block = next(b for b in re.findall(r":root\s*\{([^}]*)\}", self.src) if "--hmx-surface" in b)
        light_block = next(b for b in re.findall(r"body\.theme-light\s*\{([^}]*)\}", self.src) if "--hmx-surface" in b)
        dark_defs = set(re.findall(r"(--hmx-[a-zA-Z0-9_-]+)\s*:", dark_block))
        light_defs = set(re.findall(r"(--hmx-[a-zA-Z0-9_-]+)\s*:", light_block))
        self.assertEqual(sorted(used - dark_defs), [])
        self.assertEqual(sorted(dark_defs - light_defs), [])
        self.assertIn("html.theme-light,", self.src)
        self.assertIn("position: sticky; inset-inline-start: 0;", self.src)
        self.assertIn('"https://aiseki-navi.com/api/embed/heatmap.json?scope=all"', self.src)


if __name__ == "__main__":
    unittest.main()
