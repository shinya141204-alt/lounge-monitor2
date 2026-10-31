# -*- coding: utf-8 -*-
"""
tests/test_ui_audit_regression.py - UI・コントラスト・キャッシュバスティング・ブランド表記 回帰テストスイート

独立監査の9項目に対する自動回帰テスト:
1. Cache-Control: immutable 対応のキャッシュバスティング同期 (calm.css?v=21, portal.css?v=10, app.js?v=20261031_1, sw.js?v=71, aiseki-navi-v71)
2. ページバンドル単位でのCSSカスタムプロパティ未定義参照ゼロ（フォールバック付き var() 含む） & #comparison-modal 男女人数カラー変数
3. ライトモード .area-tag の枠線・背景なし端正タイポグラフィ統一
4. PC (860px / 1280px) .portal-global-nav の折り返し表示（右端文字切れ・テーマ切替ボタン干渉ゼロ）＆モバイル横スワイプ維持
5. スマホ (390px) .moat-pill の1〜2文字孤立改行防止 ＆ #header-x-btn .beacon-dot 視認性
6. 公開ページ (/stats, /price-comparison, #report-modal, #comparison-modal, #analysis-modal 等) における 'OLG' / 'ORIENTAL' / 単体 'オリエンタル' 露出ゼロ・ブランド名統合
7. 全 /staff/* 管理画面のダーク・ライト両モード WCAG コントラスト (>= 5.5:1 / >= 4.5:1、ステータスバッジ色含む) ＆ 非アクティブ .ai-btn 不透明化
8. #analysis-modal (openModal) の 60分後予測カード・天候チップ・答え合わせ実績・2軒目提案カード展開検証 (Playwright 実ブラウザ検証含む)
"""

import copy
import json
import os
import re
import unittest
from urllib.parse import urlparse
from unittest.mock import patch
from bs4 import BeautifulSoup

import app as app_module
import price_comparison
import store_master

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _parse_hex_or_rgba(val: str):
    """Parse #rgb, #rrggbb, rgb(...), or rgba(...) into (r, g, b, a) with 0..255 channels and 0..1 alpha."""
    s = (val or "").strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16), 1.0)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", s)
    if m:
        return (
            float(m.group(1)),
            float(m.group(2)),
            float(m.group(3)),
            float(m.group(4)) if m.group(4) is not None else 1.0,
        )
    raise ValueError(f"Unsupported color token: {val!r}")


def _blend_rgba(fg, bg):
    """Alpha-composite fg (r,g,b,a) over opaque bg (r,g,b,1)."""
    a = fg[3]
    return (
        fg[0] * a + bg[0] * (1.0 - a),
        fg[1] * a + bg[1] * (1.0 - a),
        fg[2] * a + bg[2] * (1.0 - a),
        1.0,
    )


def _rel_luminance(rgb):
    ch = []
    for c in rgb[:3]:
        s = c / 255.0
        ch.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _contrast_ratio(fg_rgb, bg_rgb):
    l1 = _rel_luminance(fg_rgb)
    l2 = _rel_luminance(bg_rgb)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _extract_css_vars_block(css_text: str, selector: str) -> dict:
    pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}", re.S)
    m = pattern.search(css_text)
    if not m:
        return {}
    out = {}
    for k, v in re.findall(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;]+);", m.group(1)):
        out[k.strip()] = v.strip()
    return out


class UiAuditRegressionTests(unittest.TestCase):
    """UI・コントラスト・キャッシュバスティング・ブランド表記の包括回帰テスト"""

    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        app_module.clear_fast_http_caches()

    # ------------------------------------------------------------------
    # 1. Cache-busting sync & immutable Cache-Control verification
    # ------------------------------------------------------------------
    def test_cache_busting_sync_across_templates_and_service_worker(self):
        """全テンプレートと static/sw.js のアセットバージョンが完全同期し、immutable ヘッダが正しく付与されること"""
        expected_calm = "/static/calm.css?v=21"
        expected_portal = "/static/portal.css?v=10"
        expected_app_js = "/static/app.js?v=20261031_1"
        expected_sw_url = "/sw.js?v=71"
        expected_cache_name = "aiseki-navi-v71"

        templates_dir = os.path.join(PROJECT_ROOT, "templates")
        calm_seen = 0
        portal_seen = 0
        app_js_seen = 0
        sw_reg_seen = 0

        for fname in sorted(os.listdir(templates_dir)):
            if not fname.endswith(".html"):
                continue
            fpath = os.path.join(templates_dir, fname)
            with open(fpath, "r", encoding="utf-8") as fp:
                raw_txt = fp.read()
            txt = re.sub(r"\{#.*?#\}|<!--.*?-->|/\*.*?\*/", "", raw_txt, flags=re.S)

            for m in re.findall(r"/static/calm\.css(?:\?v=[^\"'\s>]+)?", txt):
                self.assertEqual(m, expected_calm, f"Outdated calm.css ref in {fname}: {m}")
                calm_seen += 1

            for m in re.findall(r"/static/portal\.css(?:\?v=[^\"'\s>]+)?", txt):
                self.assertEqual(m, expected_portal, f"Outdated portal.css ref in {fname}: {m}")
                portal_seen += 1

            for m in re.findall(r"/static/app\.js(?:\?v=[^\"'\s>]+)?", txt):
                self.assertEqual(m, expected_app_js, f"Outdated app.js ref in {fname}: {m}")
                app_js_seen += 1

            for m in re.findall(r"/sw\.js(?:\?v=[^\"'\s>)]+)?", txt):
                self.assertEqual(m, expected_sw_url, f"Outdated sw.js ref in {fname}: {m}")
                sw_reg_seen += 1

        self.assertGreaterEqual(calm_seen, 29)
        self.assertGreaterEqual(portal_seen, 28)
        self.assertEqual(app_js_seen, 2)  # preload + defer script in index.html
        self.assertEqual(sw_reg_seen, 2)  # index.html + _nav_loader.html

        sw_path = os.path.join(PROJECT_ROOT, "static", "sw.js")
        with open(sw_path, "r", encoding="utf-8") as fp:
            sw_txt = fp.read()
        self.assertIn(f"CACHE_NAME = '{expected_cache_name}'", sw_txt)
        self.assertIn("aiseki-navi-v47", sw_txt)
        self.assertIn(f"'{expected_calm}'", sw_txt)
        self.assertIn(f"'{expected_portal}'", sw_txt)
        self.assertIn(f"'{expected_app_js}'", sw_txt)

        for asset_url in (expected_calm, expected_portal, expected_app_js):
            resp = self.client.get(asset_url)
            try:
                self.assertEqual(resp.status_code, 200, f"{asset_url} returned {resp.status_code}")
                cc = resp.headers.get("Cache-Control", "")
                self.assertIn("immutable", cc, f"{asset_url} missing immutable Cache-Control: {cc}")
                self.assertIn("max-age=31536000", cc)
            finally:
                resp.close()

        sw_resp = self.client.get(expected_sw_url)
        try:
            self.assertEqual(sw_resp.status_code, 200)
            self.assertIn("no-cache", sw_resp.headers.get("Cache-Control", ""))
        finally:
            sw_resp.close()

    # ------------------------------------------------------------------
    # 2. Zero undefined CSS custom properties per page bundle & #comparison-modal vars
    # ------------------------------------------------------------------
    def test_zero_undefined_css_variables_across_all_stylesheets_and_templates(self):
        """各ページテンプレートが実際に読み込むCSSバンドル単位で、フォールバック付き var() を含む全 var(--...) が定義済みであること"""
        re_comment = re.compile(r"/\*.*?\*/", re.S)
        re_def = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:")
        re_var_token = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)")
        re_js_set = re.compile(r"setProperty\(\s*['\"](--[a-zA-Z0-9_-]+)['\"]")

        static_dir = os.path.join(PROJECT_ROOT, "static")
        templates_dir = os.path.join(PROJECT_ROOT, "templates")

        css_files = {}
        for fname in sorted(os.listdir(static_dir)):
            if fname.endswith(".css"):
                with open(os.path.join(static_dir, fname), "r", encoding="utf-8") as fp:
                    raw = re_comment.sub("", fp.read())
                css_files[fname] = (set(re_def.findall(raw)), set(re_var_token.findall(raw)))

        js_defined = set()
        for fname in sorted(os.listdir(static_dir)):
            if fname.endswith(".js"):
                with open(os.path.join(static_dir, fname), "r", encoding="utf-8") as fp:
                    js_defined.update(re_js_set.findall(fp.read()))

        templates_raw = {}
        for fname in sorted(os.listdir(templates_dir)):
            if fname.endswith(".html"):
                with open(os.path.join(templates_dir, fname), "r", encoding="utf-8") as fp:
                    templates_raw[fname] = fp.read()

        def expand_includes(txt: str, depth: int = 0) -> str:
            if depth > 4:
                return txt
            def repl(m):
                inc = m.group(1)
                return expand_includes(templates_raw.get(inc, ""), depth + 1)
            return re.sub(r"\{%\s*include\s+['\"]([^'\"]+)['\"]\s*%\}", repl, txt)

        bundle_undefined = []
        checked_pages = 0
        for tname, raw_html in sorted(templates_raw.items()):
            if tname.startswith("_"):
                continue
            checked_pages += 1
            expanded = expand_includes(raw_html)
            linked_css = sorted(set(re.findall(r"/static/([a-zA-Z0-9_.-]+\.css)", expanded)))
            loads_app_js = "/static/app.js" in expanded

            clean_html = re_comment.sub("", expanded)
            defined = set(re_def.findall(clean_html))
            used_by_src = [(tname, v) for v in set(re_var_token.findall(clean_html))]

            if loads_app_js:
                defined.update(js_defined)
                with open(os.path.join(static_dir, "app.js"), "r", encoding="utf-8") as fp:
                    app_js_txt = fp.read()
                for v in set(re_var_token.findall(app_js_txt)):
                    used_by_src.append(("static/app.js", v))

            for cfile in linked_css:
                if cfile in css_files:
                    c_defs, c_uses = css_files[cfile]
                    defined.update(c_defs)
                    for v in c_uses:
                        used_by_src.append((f"static/{cfile}", v))

            for src_label, var_name in used_by_src:
                if var_name not in defined:
                    bundle_undefined.append(f"{tname} [{src_label}]: {var_name}")

        self.assertGreaterEqual(checked_pages, 35)
        self.assertEqual(
            sorted(set(bundle_undefined)),
            [],
            f"Undefined CSS custom properties detected in page bundles: {sorted(set(bundle_undefined))}",
        )

        with open(os.path.join(static_dir, "calm.css"), "r", encoding="utf-8") as fp:
            calm_css = fp.read()
        for forbidden_var in ("--calm-female", "--calm-male", "--calm-light-female", "--calm-light-male"):
            self.assertNotIn(forbidden_var, calm_css)
        for required_var in ("var(--calm-women)", "var(--calm-men)", "var(--calm-light-women)", "var(--calm-light-men)"):
            self.assertIn(required_var, calm_css)

    # ------------------------------------------------------------------
    # 3, 4, 5. Editorial UI CSS specs (.area-tag, PC nav wrap, .moat-pill, .beacon-dot)
    # ------------------------------------------------------------------
    def test_editorial_ui_css_rules_for_area_tag_pc_nav_moat_pill_and_beacon_dot(self):
        """ライトモード .area-tag 枠線除去、PCナビ折り返し、スマホ .moat-pill 1文字落ち防止、.beacon-dot 寸法が定義されていること"""
        with open(os.path.join(PROJECT_ROOT, "static", "calm.css"), "r", encoding="utf-8") as fp:
            calm_css = fp.read()
        with open(os.path.join(PROJECT_ROOT, "static", "portal.css"), "r", encoding="utf-8") as fp:
            portal_css = fp.read()

        # Item 3: body.theme-light .area-tag borderless typography
        m_area = re.search(r"body\.theme-light\s+\.area-tag\s*\{([^}]*)\}", calm_css, re.S)
        self.assertIsNotNone(m_area, "Missing body.theme-light .area-tag rule in calm.css")
        area_body = m_area.group(1)
        self.assertIn("background: transparent !important", area_body)
        self.assertIn("border: none !important", area_body)
        self.assertIn("padding: 0 !important", area_body)
        self.assertIn("color: var(--calm-light-sub) !important", area_body)

        # Item 4: Desktop @media (min-width: 860px) .portal-global-nav wrap in both calm.css and portal.css
        self.assertIn("@media (min-width: 860px)", calm_css)
        self.assertIn("flex-wrap: wrap !important", calm_css)
        self.assertIn("@media (min-width: 860px)", portal_css)
        self.assertIn("flex-wrap: wrap !important", portal_css)

        # Item 5a: Mobile @media (max-width: 600px) .moat-pill compact sizing
        self.assertIn("@media (max-width: 600px)", calm_css)
        self.assertIn("letter-spacing: -0.015em !important", calm_css)

        # Item 5b: .beacon-dot explicit non-zero dimensions + light mode accent color
        m_dot = re.search(r"\.beacon-dot\s*\{([^}]*)\}", calm_css, re.S)
        self.assertIsNotNone(m_dot, "Missing .beacon-dot dimensions rule in calm.css")
        self.assertIn("width: 7px", m_dot.group(1))
        self.assertIn("height: 7px", m_dot.group(1))
        self.assertIn("body.theme-light #header-x-btn .beacon-dot", calm_css)

        # Item 7 supplement: .momentum-legend-hint high-contrast override on / and /?staff=1
        self.assertIn("body:not(.theme-light) .momentum-legend-hint", calm_css)
        self.assertIn("body.theme-light .momentum-legend-hint", calm_css)

    # ------------------------------------------------------------------
    # 6. Public rendered HTML zero 'OLG' / 'ORIENTAL' / bare 'オリエンタル' exposure & brand alias merge
    # ------------------------------------------------------------------
    def test_rendered_public_pages_and_brand_ja_filter_have_zero_isolated_olg(self):
        """公開ページのレンダリング後HTML可視テキストに 'OLG'・'ORIENTAL'・'YATAKOI'・単体 'オリエンタル' が一切露出せず、重複ブランド集計も統合されること"""
        brand_ja = app_module.app.jinja_env.filters["brand_ja"]
        merge_brand_stats = app_module.app.jinja_env.filters["merge_brand_stats"]

        self.assertEqual(brand_ja("OLG"), "オリエンタルラウンジ")
        self.assertEqual(brand_ja("ORIENTAL"), "オリエンタルラウンジ")
        self.assertEqual(brand_ja("oriental"), "オリエンタルラウンジ")
        self.assertEqual(brand_ja("YATAKOI"), "ヤタコイ")
        self.assertEqual(brand_ja("yatakoi"), "ヤタコイ")
        self.assertEqual(brand_ja("AG"), "ag")
        self.assertEqual(brand_ja("ag"), "ag")
        self.assertEqual(brand_ja("JIS"), "JIS")
        self.assertEqual(brand_ja(""), "")
        self.assertEqual(brand_ja(None), "")

        # Verify merge_brand_stats merges OLG + ORIENTAL without mutating input dict
        raw_by_brand = {
            "ORIENTAL": {"store_count": 20, "total_avg_women": 420.4, "total_avg_men": 280.2},
            "OLG": {"store_count": 9, "total_avg_women": 199.6, "total_avg_men": 129.8},
            "YATAKOI": {"store_count": 2, "total_avg_women": 48.0, "total_avg_men": 42.0},
            "JIS": {"store_count": 10, "total_avg_women": 240.0, "total_avg_men": 180.0},
        }
        snapshot_before = copy.deepcopy(raw_by_brand)
        merged = merge_brand_stats(raw_by_brand)
        self.assertEqual(raw_by_brand, snapshot_before, "merge_brand_stats must not mutate input dict")
        self.assertEqual(list(merged.keys()), ["オリエンタルラウンジ", "ヤタコイ", "JIS"])
        self.assertEqual(merged["オリエンタルラウンジ"]["store_count"], 29)
        self.assertEqual(merged["オリエンタルラウンジ"]["total_avg_women"], 620.0)
        self.assertEqual(merged["オリエンタルラウンジ"]["total_avg_men"], 410.0)

        # Boundary tests for unregistered store names in store_master.seo_name
        self.assertEqual(store_master.seo_name("OLG"), "オリエンタルラウンジ")
        self.assertEqual(store_master.seo_name("ORIENTAL"), "オリエンタルラウンジ")
        self.assertEqual(store_master.seo_name("OLG　未登録新店"), "オリエンタルラウンジ 未登録新店")
        self.assertEqual(store_master.seo_name("olg 未登録新店"), "オリエンタルラウンジ 未登録新店")
        self.assertEqual(store_master.seo_name("ORIENTAL 未登録新店"), "オリエンタルラウンジ 未登録新店")
        self.assertEqual(store_master.seo_name("OLG ag未登録店"), "ag（アグ）未登録店")
        self.assertEqual(store_master.seo_name("YATAKOI 未登録店"), "ヤタコイ 未登録店")

        # Verify internal dict key in BRAND_PRICE_SPECS remains 'OLG'
        self.assertIn("OLG", price_comparison.BRAND_PRICE_SPECS)
        self.assertEqual(price_comparison.BRAND_PRICE_SPECS["OLG"]["brand_code"], "OLG")

        # Real weekly_report schema including production monitor.py 'ORIENTAL' & 'YATAKOI' + edge-case store names
        fake_report = {
            "period": "2026-10-24 ~ 2026-10-30",
            "generated_at": "2026-10-31 21:00:00",
            "total_records_analyzed": 265000,
            "weekly_sample_rows": 12000,
            "by_brand": {
                "ORIENTAL": {"store_count": 20, "total_avg_women": 420.4, "total_avg_men": 280.2},
                "OLG": {"store_count": 9, "total_avg_women": 199.6, "total_avg_men": 129.8},
                "ag": {"store_count": 8, "total_avg_women": 190.0, "total_avg_men": 140.0},
                "JIS": {"store_count": 10, "total_avg_women": 240.0, "total_avg_men": 180.0},
                "YATAKOI": {"store_count": 2, "total_avg_women": 48.0, "total_avg_men": 42.0},
            },
            "stores": [
                {"name": "OLG 新宿", "brand": "ORIENTAL", "avg_women": 52.4, "avg_men": 31.0, "max_women": 68, "avg_wpct": 62.8, "peak_hour": "22:00", "days_with_data": 7},
                {"name": "OLG ag渋谷", "brand": "ORIENTAL", "avg_women": 44.1, "avg_men": 28.5, "max_women": 59, "avg_wpct": 60.7, "peak_hour": "22:00", "days_with_data": 7},
                {"name": "OLG　難波新館", "brand": "OLG", "avg_women": 33.0, "avg_men": 22.0, "max_women": 45, "avg_wpct": 60.0, "peak_hour": "21:00", "days_with_data": 6},
                {"name": "OLG", "brand": "OLG", "avg_women": 25.0, "avg_men": 19.0, "max_women": 32, "avg_wpct": 56.8, "peak_hour": "21:00", "days_with_data": 5},
                {"name": "YATAKOI 梅田", "brand": "YATAKOI", "avg_women": 24.0, "avg_men": 21.0, "max_women": 34, "avg_wpct": 53.3, "peak_hour": "22:00", "days_with_data": 7},
            ],
            "anaba_stores": [
                {"name": "OLG 新宿", "brand": "ORIENTAL", "avg_women": 52.4, "avg_men": 31.0, "max_women": 68, "avg_wpct": 62.8, "peak_hour": "22:00", "days_with_data": 7},
                {"name": "OLG　難波新館", "brand": "OLG", "avg_women": 33.0, "avg_men": 22.0, "max_women": 45, "avg_wpct": 60.0, "peak_hour": "21:00", "days_with_data": 6},
            ],
            "weekday_summary": [
                {"weekday": "金曜日", "avg_women": 52.4, "avg_men": 49.5, "avg_wpct": 51.4, "note": "全国的に来店数が急増"}
            ],
            "timeslot_summary": [
                {"slot": "21:00〜23:00", "avg_women": 46.2, "avg_men": 41.0, "avg_wpct": 53.0, "advice": "最大ピーク帯"}
            ],
        }

        re_olg = re.compile(r"\bOLG\b", re.IGNORECASE)
        re_oriental_en = re.compile(r"\bORIENTAL\b(?!\s*LOUNGE)", re.IGNORECASE)
        re_yatakoi_en = re.compile(r"(?<!ヤタコイ（)\bYATAKOI\b", re.IGNORECASE)
        re_bare_oriental = re.compile(r"オリエンタル(?!ラウンジ)")
        re_oriradi = re.compile(r"オリラジ")

        ssr_edge_stores = [
            {"name": "OLG 新宿", "source": "oriental", "women": 32, "men": 18, "total": 50, "ratio": 1.78, "timestamp": "2026-10-31 21:00:00"},
            {"name": "OLG ag渋谷", "source": "oriental", "women": 24, "men": 16, "total": 40, "ratio": 1.50, "timestamp": "2026-10-31 21:00:00"},
            {"name": "OLG　難波新館", "source": "", "women": 18, "men": 12, "total": 30, "ratio": 1.50, "timestamp": "2026-10-31 21:00:00"},
            {"name": "OLG", "source": "", "women": 12, "men": 10, "total": 22, "ratio": 1.20, "timestamp": "2026-10-31 21:00:00"},
            {"name": "YATAKOI 梅田", "source": "yatakoi", "women": 15, "men": 14, "total": 29, "ratio": 1.07, "timestamp": "2026-10-31 21:00:00"},
        ]
        with app_module.data_lock:
            app_module.latest_data["full_data"] = ssr_edge_stores
            app_module.latest_data["top5"] = ssr_edge_stores[:3]
            app_module.latest_data["last_updated"] = "2026-10-31 21:00:00"
            app_module.latest_data["status"] = "ok"

        public_routes = [
            "/",
            "/stats",
            "/price-comparison",
            "/columns",
            "/areas",
            "/concierge",
            "/for-women",
            "/grooming",
            "/privacy",
            "/oriental-lounge",
            "/ag",
            "/jis",
            "/asobibar",
            "/store/oriental-lounge-shinjuku",
            "/guide/brand-comparison",
            "/guide/crowd-rush-paradox",
        ]

        app_module.clear_fast_http_caches()
        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None), \
             patch("weekly_report.generate_weekly_report", return_value=fake_report):
            app_module.clear_fast_http_caches()
            for route in public_routes:
                resp = self.client.get(route)
                try:
                    self.assertEqual(resp.status_code, 200, f"{route} returned {resp.status_code}")
                    html = resp.get_data(as_text=True)
                finally:
                    resp.close()

                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style"]):
                    tag.extract()
                visible_text = soup.get_text("\n")

                for pat, label in (
                    (re_olg, "Isolated OLG"),
                    (re_oriental_en, "English ORIENTAL"),
                    (re_yatakoi_en, "English YATAKOI"),
                    (re_bare_oriental, "Bare オリエンタル"),
                    (re_oriradi, "オリラジ"),
                ):
                    m = pat.search(visible_text)
                    self.assertIsNone(
                        m,
                        f"{label} leaked on {route}: {visible_text[max(0, m.start()-30):m.end()+30] if m else ''}",
                    )

                if route == "/stats":
                    self.assertIn("オリエンタルラウンジ", visible_text)
                    self.assertIn("ヤタコイ", visible_text)
                    card_headings = [h.get_text(strip=True) for h in soup.select(".brand-stat-card h3")]
                    olg_cards = [h for h in card_headings if h.startswith("オリエンタルラウンジ（")]
                    self.assertEqual(olg_cards, ["オリエンタルラウンジ（29店舗）"], f"Expected merged OLG+ORIENTAL card, got {card_headings}")
                elif route == "/price-comparison":
                    self.assertIn("オリエンタルラウンジ", visible_text)

    # ------------------------------------------------------------------
    # 7. Staff management screens dark & light mode WCAG contrast audit
    # ------------------------------------------------------------------
    def test_staff_screens_wcag_contrast_palettes_and_inactive_ai_buttons(self):
        """全 staff_*.html のダーク・ライト配色変数（ステータス色含む）と .desk-box / .ai-btn が WCAG AA (5.5:1) を満たすこと"""
        staff_files = [
            "staff_sns_editorial.html",
            "staff_column_editorial.html",
            "staff_analytics.html",
            "staff_pdca.html",
            "staff_sns_kit.html",
            "staff_login.html",
        ]
        semantic_fg_tokens = ("--gold", "--green", "--blue", "--rose", "--amber", "--red", "--err-ink", "--purple")

        for fname in staff_files:
            fpath = os.path.join(PROJECT_ROOT, "templates", fname)
            with open(fpath, "r", encoding="utf-8") as fp:
                html = fp.read()

            self.assertNotIn("opacity: 0.65", html, f"{fname} still contains low-contrast opacity: 0.65")
            self.assertNotIn("opacity:0.65", html, f"{fname} still contains low-contrast opacity:0.65")

            dark_vars = _extract_css_vars_block(html, ":root")
            light_vars = _extract_css_vars_block(html, "body.theme-light")
            self.assertTrue(dark_vars, f"Missing :root vars in {fname}")
            self.assertTrue(light_vars, f"Missing body.theme-light vars in {fname}")

            for mode_name, vmap in (("dark", dark_vars), ("light", light_vars)):
                bg = _parse_hex_or_rgba(vmap["--bg"])
                surface = _parse_hex_or_rgba(vmap["--surface"])
                surface2 = _parse_hex_or_rgba(vmap["--surface-2"])
                text_c = _parse_hex_or_rgba(vmap["--text"])
                sub_c = _parse_hex_or_rgba(vmap["--sub"])

                for bg_label, bg_rgba in (("--bg", bg), ("--surface", surface), ("--surface-2", surface2)):
                    cr_text = _contrast_ratio(text_c, bg_rgba)
                    cr_sub = _contrast_ratio(sub_c, bg_rgba)
                    self.assertGreaterEqual(cr_text, 10.0, f"{fname} [{mode_name}] --text on {bg_label} too low: {cr_text:.2f}")
                    self.assertGreaterEqual(cr_sub, 5.5, f"{fname} [{mode_name}] --sub on {bg_label} < 5.5:1: {cr_sub:.2f}")

                    for sem_tok in semantic_fg_tokens:
                        if sem_tok in vmap:
                            sem_rgba = _parse_hex_or_rgba(vmap[sem_tok])
                            cr_sem = _contrast_ratio(sem_rgba, bg_rgba)
                            self.assertGreaterEqual(
                                cr_sem,
                                5.5,
                                f"{fname} [{mode_name}] {sem_tok} ({vmap[sem_tok]}) on {bg_label} < 5.5:1: {cr_sem:.2f}",
                            )

                if "--green-soft" in vmap:
                    green_box_bg = _blend_rgba(_parse_hex_or_rgba(vmap["--green-soft"]), surface)
                    cr_sub_on_green = _contrast_ratio(sub_c, green_box_bg)
                    self.assertGreaterEqual(
                        cr_sub_on_green,
                        5.5,
                        f"{fname} [{mode_name}] --sub on --green-soft box < 5.5:1: {cr_sub_on_green:.2f}",
                    )

        # Specific checks for editorial screens (.desk-box & .ai-btn.is-inactive & .note-intent-btn)
        for ed_file in ("staff_sns_editorial.html", "staff_column_editorial.html"):
            with open(os.path.join(PROJECT_ROOT, "templates", ed_file), "r", encoding="utf-8") as fp:
                ed_html = fp.read()
            self.assertIn(".ai-btn.is-inactive", ed_html)
            self.assertIn("body.theme-light .desk-box", ed_html)
            self.assertGreaterEqual(_contrast_ratio(_parse_hex_or_rgba("#d6d2c9"), (39, 51, 49, 1.0)), 8.0)
            self.assertGreaterEqual(_contrast_ratio(_parse_hex_or_rgba("#3a3630"), _parse_hex_or_rgba("#faf9f6")), 9.0)

        with open(os.path.join(PROJECT_ROOT, "templates", "staff_sns_editorial.html"), "r", encoding="utf-8") as fp:
            sns_ed_html = fp.read()
        self.assertIn("#156b56", sns_ed_html)
        self.assertGreaterEqual(_contrast_ratio((255, 255, 255, 1.0), _parse_hex_or_rgba("#156b56")), 5.5)

        r_login = self.client.get("/staff")
        try:
            self.assertEqual(r_login.status_code, 200)
            self.assertIn("body.theme-light", r_login.get_data(as_text=True))
        finally:
            r_login.close()

        with patch("logger.get_client", return_value=None):
            with self.client.session_transaction() as sess:
                sess["is_staff"] = True
                sess["is_master"] = True
            for sr in ("/?staff=1", "/staff/analytics", "/staff/sns-editorial", "/staff/sns-editorial?channel=note", "/staff/column-editorial", "/staff/pdca", "/staff/sns-kit"):
                resp = self.client.get(sr)
                try:
                    self.assertEqual(resp.status_code, 200, f"Staff route {sr} returned {resp.status_code}")
                    body = resp.get_data(as_text=True)
                    self.assertNotIn("opacity:0.65", body)
                    self.assertNotIn("opacity: 0.65", body)
                finally:
                    resp.close()

    # ------------------------------------------------------------------
    # 8. Real-browser Playwright verification (#analysis-modal, #comparison-modal, #report-modal, PC 860/1280px, Mobile 390px, Staff DOM contrast)
    # ------------------------------------------------------------------
    def test_playwright_real_browser_modals_nav_mobile_and_contrast(self):
        """Playwright 実ブラウザで PC 860/1280px ナビ・スマホ 390px ピル・3種モーダル展開・管理画面DOMコントラストをダーク/ライト双方で検証"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed in this Python environment")

        sample_stores = [
            {
                "name": "OLG 新宿",
                "source": "oriental",
                "women": 28,
                "men": 18,
                "total": 46,
                "ratio": 1.56,
                "timestamp": "2026-10-31 21:30:00",
                "weather_brief": {"city": "新宿", "condition": "小雨", "temp_c": 16.5, "is_rainy": True},
                "forecast_60m": {
                    "pred_60m_women": 34,
                    "pred_60m_men": 20,
                    "pred_60m_total": 54,
                    "pred_60m_wpct": 63,
                    "pred_delta_pt": 2.4,
                    "flip_risk_pct": 14,
                    "badge_tone": "good",
                    "alert_code": "jackpot_hold",
                    "badge_men": "🔮60分後も好調維持(予測63%)",
                    "badge_women": "🔮60分後も華やか維持",
                    "reason_short": "金曜21時台の98万件遷移パターンでは女性流入が持続しやすい傾向です。",
                    "accuracy": {
                        "resolved_count": 12,
                        "mae_wpct": 5.4,
                        "within_10pt_pct": 83,
                        "latest": {"predicted_hm": "20:30", "pred_wpct": 60, "actual_wpct": 61, "error_pt": 1},
                    },
                },
                "wait_estimate": {
                    "is_measured": False,
                    "is_standing_free": False,
                    "est_wait_men_min": 0,
                    "wait_tone": "good",
                    "wait_badge_men": "🟢 推定:待ち約0分（即相席圏）",
                    "wait_badge_women": "🟢 推定:即案内",
                    "pos_summary_short": "現在女性が男性を上回っており、即相席圏内です。",
                },
            },
            {
                "name": "OLG ag渋谷",
                "source": "oriental",
                "women": 25,
                "men": 20,
                "total": 45,
                "ratio": 1.25,
                "timestamp": "2026-10-31 21:30:00",
                "wait_estimate": {
                    "is_measured": False,
                    "is_standing_free": False,
                    "est_wait_men_min": 0,
                    "wait_tone": "good",
                    "wait_badge_men": "🟢 推定:待ち約0分（即相席圏）",
                    "wait_badge_women": "🟢 推定:即案内",
                },
            },
            {
                "name": "OLG　梅田新館",
                "source": "",
                "women": 19,
                "men": 15,
                "total": 34,
                "ratio": 1.27,
                "timestamp": "2026-10-31 21:30:00",
            },
            {
                "name": "OLG",
                "source": "",
                "women": 14,
                "men": 11,
                "total": 25,
                "ratio": 1.27,
                "timestamp": "2026-10-31 21:30:00",
            },
        ]

        fake_weekly_report = {
            "period": "2026-10-24 ~ 2026-10-30",
            "generated_at": "2026-10-31 21:30:00",
            "total_records_analyzed": 265000,
            "weekly_sample_rows": 12000,
            "by_brand": {
                "ORIENTAL": {"store_count": 20, "total_avg_women": 420.4, "total_avg_men": 280.2},
                "OLG": {"store_count": 9, "total_avg_women": 199.6, "total_avg_men": 129.8},
                "YATAKOI": {"store_count": 2, "total_avg_women": 48.0, "total_avg_men": 42.0},
            },
            "stores": [
                {"name": "OLG 新宿", "brand": "ORIENTAL", "avg_women": 52.4, "avg_men": 31.0, "max_women": 68, "avg_wpct": 62.8, "peak_hour": "22:00", "days_with_data": 7},
                {"name": "OLG ag渋谷", "brand": "ORIENTAL", "avg_women": 44.1, "avg_men": 28.5, "max_women": 59, "avg_wpct": 60.7, "peak_hour": "22:00", "days_with_data": 7},
                {"name": "OLG　梅田新館", "brand": "OLG", "avg_women": 30.0, "avg_men": 21.0, "max_women": 40, "avg_wpct": 58.8, "peak_hour": "21:00", "days_with_data": 6},
                {"name": "OLG", "brand": "OLG", "avg_women": 22.0, "avg_men": 18.0, "max_women": 30, "avg_wpct": 55.0, "peak_hour": "21:00", "days_with_data": 5},
                {"name": "YATAKOI 梅田", "brand": "YATAKOI", "avg_women": 24.0, "avg_men": 21.0, "max_women": 34, "avg_wpct": 53.3, "peak_hour": "22:00", "days_with_data": 7},
            ],
            "anaba_stores": [],
            "weekday_summary": [],
            "timeslot_summary": [],
        }

        with app_module.data_lock:
            app_module.latest_data["full_data"] = sample_stores
            app_module.latest_data["top5"] = sample_stores[:3]
            app_module.latest_data["last_updated"] = "2026-10-31 21:30:00"
            app_module.latest_data["status"] = "ok"
        app_module.clear_fast_http_caches()

        base = "http://aiseki.local"

        def attach_router(ctx, client):
            def handle(route):
                req = route.request
                u = urlparse(req.url)
                if u.netloc != "aiseki.local":
                    route.abort()
                    return
                path_qs = u.path + (("?" + u.query) if u.query else "")
                headers = {k: v for k, v in req.headers.items() if k.lower() not in ("host", "content-length", "cookie")}
                resp = client.open(path_qs, method=req.method.upper(), headers=headers, data=req.post_data_buffer, follow_redirects=True)
                resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")}
                route.fulfill(status=resp.status_code, headers=resp_headers, body=resp.get_data())
                resp.close()
            ctx.route("**/*", handle)

        fake_acc_map = {
            "OLG 新宿": {
                "resolved_count": 12,
                "mae_wpct": 5.4,
                "within_10pt_pct": 83,
                "latest": {"predicted_hm": "20:30", "pred_wpct": 60, "actual_wpct": 61, "error_pt": 1},
            }
        }

        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None), \
             patch("local_db.get_cached_store_accuracy_map", return_value=fake_acc_map), \
             patch("weekly_report.generate_weekly_report", return_value=fake_weekly_report), \
             patch("logger.get_client", return_value=None):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    # 1. Desktop boundary 860px & standard 1280px nav checks
                    for vw in (860, 1280):
                        ctx_nav = browser.new_context(viewport={"width": vw, "height": 900})
                        attach_router(ctx_nav, self.client)
                        npage = ctx_nav.new_page()
                        for nav_route in ("/", "/columns"):
                            npage.goto(base + nav_route, wait_until="domcontentloaded")
                            npage.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                            nav_state = npage.evaluate("""() => {
                                const nav = document.querySelector('.portal-global-nav');
                                const nr = nav.getBoundingClientRect();
                                const clipped = Array.from(nav.querySelectorAll('a')).filter(a => a.getBoundingClientRect().right > Math.ceil(nr.right) + 2);
                                const btn = document.querySelector('#theme-toggle-btn, .portal-theme-btn');
                                let overlapsBtn = false;
                                if (btn) {
                                    const br = btn.getBoundingClientRect();
                                    overlapsBtn = Array.from(nav.querySelectorAll('a')).some(a => {
                                        const r = a.getBoundingClientRect();
                                        return !(r.right <= br.left + 1 || r.left >= br.right - 1 || r.bottom <= br.top + 1 || r.top >= br.bottom - 1);
                                    });
                                }
                                return {flexWrap: getComputedStyle(nav).flexWrap, clippedCount: clipped.length, overlapsBtn, scrollWidth: nav.scrollWidth, clientWidth: nav.clientWidth};
                            }""")
                            self.assertEqual(nav_state["flexWrap"], "wrap", f"Nav not wrapping at {vw}px on {nav_route}")
                            self.assertEqual(nav_state["clippedCount"], 0, f"Nav clipped at {vw}px on {nav_route}")
                            self.assertFalse(nav_state["overlapsBtn"], f"Nav overlaps theme button at {vw}px on {nav_route}")
                            self.assertLessEqual(nav_state["scrollWidth"], nav_state["clientWidth"] + 1)
                        ctx_nav.close()

                    # 2. PC 1280x900 modals & brand compliance checks in dark and light modes
                    ctx_pc = browser.new_context(viewport={"width": 1280, "height": 900})
                    attach_router(ctx_pc, self.client)
                    page = ctx_pc.new_page()
                    page.goto(base + "/", wait_until="domcontentloaded")
                    page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                    page.wait_for_timeout(150)

                    # Ranking cards must never expose isolated OLG or ORIENTAL
                    ranking_txt = page.evaluate("document.getElementById('ranking-list').innerText")
                    self.assertNotRegex(ranking_txt, r"\bOLG\b|\bORIENTAL\b(?!\s*LOUNGE)|\bYATAKOI\b")

                    for is_light in (False, True):
                        page.evaluate(f"document.body.classList.toggle('theme-light', {str(is_light).lower()})")

                        # .beacon-dot & .area-tag & .momentum-legend-hint
                        ui_state = page.evaluate("""() => {
                            const dot = document.querySelector('#header-x-btn .beacon-dot');
                            const dr = dot.getBoundingClientRect();
                            const area = document.querySelector('.area-tag');
                            const acs = getComputedStyle(area);
                            const hint = document.querySelector('.momentum-legend-hint');
                            const hcs = hint ? getComputedStyle(hint) : null;
                            return {
                                dotW: dr.width, dotH: dr.height, dotBg: getComputedStyle(dot).backgroundColor,
                                areaBg: acs.backgroundColor, areaBorder: acs.borderTopWidth, areaPad: acs.paddingLeft,
                                hintOpacity: hcs ? parseFloat(hcs.opacity) : 1
                            };
                        }""")
                        self.assertGreaterEqual(ui_state["dotW"], 6)
                        self.assertGreaterEqual(ui_state["dotH"], 6)
                        self.assertNotEqual(ui_state["dotBg"], "rgba(0, 0, 0, 0)")
                        self.assertEqual(ui_state["areaBg"], "rgba(0, 0, 0, 0)")
                        self.assertEqual(ui_state["areaBorder"], "0px")
                        self.assertEqual(ui_state["areaPad"], "0px")
                        self.assertEqual(ui_state["hintOpacity"], 1.0)

                        # #comparison-modal .w-count & .m-count colors + zero OLG/ORIENTAL
                        page.evaluate("openComparisonModal()")
                        comp_state = page.evaluate("""() => {
                            const cm = document.getElementById('comparison-modal');
                            const w = cm.querySelector('.brand-card-counts .w-count');
                            const m = cm.querySelector('.brand-card-counts .m-count');
                            return {w: getComputedStyle(w).color, m: getComputedStyle(m).color, text: cm.innerText};
                        }""")
                        self.assertNotIn(comp_state["w"], ("rgb(0, 0, 0)", "rgb(31, 30, 27)", "rgb(236, 234, 228)"))
                        self.assertNotIn(comp_state["m"], ("rgb(0, 0, 0)", "rgb(31, 30, 27)", "rgb(236, 234, 228)"))
                        self.assertNotEqual(comp_state["w"], comp_state["m"])
                        self.assertNotRegex(comp_state["text"], r"\bOLG\b|\bORIENTAL\b|\bYATAKOI\b")
                        page.evaluate("closeComparisonModal()")

                        # #report-modal merged brand rows + zero OLG/ORIENTAL/YATAKOI
                        page.evaluate("openReportModal()")
                        page.wait_for_function("document.querySelectorAll('#report-content table').length === 2")
                        rep_state = page.evaluate("""() => {
                            const rc = document.getElementById('report-content');
                            const brandRows = Array.from(rc.querySelectorAll('table')[0].querySelectorAll('tr')).slice(1).map(tr => tr.innerText.trim());
                            return {text: rc.innerText, brandRows};
                        }""")
                        self.assertNotRegex(rep_state["text"], r"\bOLG\b|\bORIENTAL\b|\bYATAKOI\b")
                        olg_brand_rows = [r for r in rep_state["brandRows"] if r.startswith("オリエンタルラウンジ")]
                        self.assertEqual(len(olg_brand_rows), 1, f"Expected single merged オリエンタルラウンジ row in #report-modal, got {rep_state['brandRows']}")
                        self.assertIn("29", olg_brand_rows[0])
                        self.assertIn("ヤタコイ", rep_state["text"])
                        page.evaluate("closeReportModal()")

                        # #analysis-modal live expansion (.moat-modal-card, .moat-weather-chip.is-rainy, .moat-acc-track, .second-venue-card)
                        page.evaluate("openModal('OLG 新宿')")
                        page.wait_for_timeout(180)
                        modal_state = page.evaluate("""() => {
                            const m = document.getElementById('analysis-modal');
                            const cards = Array.from(m.querySelectorAll('.moat-modal-card'));
                            const chip = m.querySelector('.moat-weather-chip.is-rainy');
                            const acc = m.querySelector('.moat-acc-track');
                            const sv = Array.from(m.querySelectorAll('.second-venue-card'));
                            return {
                                display: getComputedStyle(m).display,
                                cardsVisible: cards.length >= 2 && cards.every(c => c.getBoundingClientRect().height > 20),
                                weatherVisible: !!chip && chip.getBoundingClientRect().height > 12,
                                accVisible: !!acc && acc.getBoundingClientRect().height > 10,
                                secondVenueVisible: sv.length > 0 && sv[0].getBoundingClientRect().height > 20,
                                text: m.innerText
                            };
                        }""")
                        self.assertEqual(modal_state["display"], "flex")
                        self.assertTrue(modal_state["cardsVisible"])
                        self.assertTrue(modal_state["weatherVisible"])
                        self.assertTrue(modal_state["accVisible"])
                        self.assertTrue(modal_state["secondVenueVisible"])
                        self.assertNotRegex(modal_state["text"], r"\bOLG\b|\bORIENTAL\b")
                        page.evaluate("closeModal()")

                        # Boundary bare 'OLG' and full-width space 'OLG　梅田新館' in #analysis-modal
                        for edge_store in ("OLG", "OLG　梅田新館"):
                            page.evaluate(f"openModal({json.dumps(edge_store)})")
                            page.wait_for_timeout(100)
                            edge_txt = page.evaluate("document.getElementById('analysis-modal').innerText")
                            self.assertNotRegex(edge_txt, r"\bOLG\b|\bORIENTAL\b")
                            self.assertNotIn("オリエンタルラウンジ オリエンタルラウンジ", edge_txt)
                            page.evaluate("closeModal()")

                    # 3. Full-DOM Staff WCAG contrast verification across all staff routes (dark & light)
                    anon_client = app_module.app.test_client()
                    ctx_anon = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
                    attach_router(ctx_anon, anon_client)
                    apage = ctx_anon.new_page()
                    apage.goto(base + "/staff", wait_until="domcontentloaded")
                    apage.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                    for is_light in (False, True):
                        apage.evaluate(f"document.body.classList.toggle('theme-light', {str(is_light).lower()})")
                        login_scan = apage.evaluate("""() => {
                            function parseColor(s) {
                                const m = s && s.match(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/);
                                return m ? [+m[1], +m[2], +m[3], m[4] !== undefined ? +m[4] : 1] : [0, 0, 0, 0];
                            }
                            function blend(fg, bg) {
                                const a = fg[3] + bg[3] * (1 - fg[3]);
                                if (a === 0) return [0, 0, 0, 0];
                                return [(fg[0]*fg[3] + bg[0]*bg[3]*(1-fg[3]))/a, (fg[1]*fg[3] + bg[1]*bg[3]*(1-fg[3]))/a, (fg[2]*fg[3] + bg[2]*bg[3]*(1-fg[3]))/a, a];
                            }
                            function lum(c) {
                                const f = v => { const s = v/255; return s <= 0.03928 ? s/12.92 : Math.pow((s+0.055)/1.055, 2.4); };
                                return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]);
                            }
                            function cr(a, b) {
                                const l1 = lum(a), l2 = lum(b);
                                return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
                            }
                            const isL = document.body.classList.contains('theme-light');
                            const sub45 = [];
                            const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                            let n;
                            while ((n = w.nextNode())) {
                                const t = (n.nodeValue || '').trim();
                                if (!t) continue;
                                const el = n.parentElement;
                                const cs = getComputedStyle(el);
                                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                                const r = el.getBoundingClientRect();
                                if (!r.width || !r.height) continue;
                                const chain = [];
                                let cur = el, op = 1;
                                while (cur && cur.nodeType === 1) {
                                    chain.unshift(getComputedStyle(cur));
                                    op *= parseFloat(getComputedStyle(cur).opacity || '1');
                                    cur = cur.parentElement;
                                }
                                let bg = isL ? [246, 245, 241, 1] : [13, 15, 18, 1];
                                for (const lcs of chain) {
                                    const bc = parseColor(lcs.backgroundColor);
                                    if (bc[3] > 0) bg = blend(bc, bg);
                                }
                                const fgRaw = parseColor(cs.color);
                                const fg = blend([fgRaw[0], fgRaw[1], fgRaw[2], fgRaw[3] * op], bg);
                                const ratio = cr(fg, bg);
                                if (ratio < 4.5) sub45.push({txt: t, ratio: +ratio.toFixed(2)});
                            }
                            return sub45;
                        }""")
                        self.assertEqual(login_scan, [], f"/staff login contrast failure (light={is_light}): {login_scan}")
                    ctx_anon.close()

                    with self.client.session_transaction() as sess:
                        sess["is_staff"] = True
                        sess["is_master"] = True

                    staff_routes = [
                        "/?staff=1",
                        "/staff/sns-editorial",
                        "/staff/column-editorial",
                        "/staff/analytics",
                        "/staff/pdca",
                        "/staff/sns-kit",
                    ]
                    ctx_staff = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
                    attach_router(ctx_staff, self.client)
                    spage = ctx_staff.new_page()
                    for sroute in staff_routes:
                        spage.goto(base + sroute, wait_until="domcontentloaded")
                        spage.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                        spage.evaluate("""() => {
                            document.querySelectorAll('details, #platform-token-drawer, #gemini-key-drawer').forEach(el => {
                                if (el.tagName === 'DETAILS') el.open = true;
                                else el.style.display = 'block';
                            });
                        }""")
                        for is_light in (False, True):
                            spage.evaluate(f"""() => {{
                                document.body.classList.toggle('theme-light', {str(is_light).lower()});
                                document.documentElement.setAttribute('data-theme', {json.dumps('light' if is_light else 'dark')});
                            }}""")
                            staff_dom = spage.evaluate("""() => {
                                function parseColor(s) {
                                    const m = s && s.match(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/);
                                    return m ? [+m[1], +m[2], +m[3], m[4] !== undefined ? +m[4] : 1] : [0, 0, 0, 0];
                                }
                                function blend(fg, bg) {
                                    const a = fg[3] + bg[3] * (1 - fg[3]);
                                    if (a === 0) return [0, 0, 0, 0];
                                    return [
                                        (fg[0]*fg[3] + bg[0]*bg[3]*(1-fg[3]))/a,
                                        (fg[1]*fg[3] + bg[1]*bg[3]*(1-fg[3]))/a,
                                        (fg[2]*fg[3] + bg[2]*bg[3]*(1-fg[3]))/a,
                                        a
                                    ];
                                }
                                function applyLayer(cs, base) {
                                    const bgc = parseColor(cs.backgroundColor);
                                    let cur = bgc[3] > 0 ? blend(bgc, base) : base;
                                    const img = cs.backgroundImage || '';
                                    if (img.includes('gradient')) {
                                        const ms = [...img.matchAll(/rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)/g)];
                                        if (ms.length > 0) {
                                            let r=0, g=0, b=0;
                                            for (const m of ms) {
                                                const st = blend([+m[1], +m[2], +m[3], m[4] !== undefined ? +m[4] : 1], cur);
                                                r += st[0]; g += st[1]; b += st[2];
                                            }
                                            cur = [r/ms.length, g/ms.length, b/ms.length, 1];
                                        }
                                    }
                                    return cur;
                                }
                                function lum(c) {
                                    const f = v => { const s = v/255; return s <= 0.03928 ? s/12.92 : Math.pow((s+0.055)/1.055, 2.4); };
                                    return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]);
                                }
                                function cr(a, b) {
                                    const l1 = lum(a), l2 = lum(b);
                                    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
                                }
                                const isL = document.body.classList.contains('theme-light');
                                const baseInit = isL ? [246, 245, 241, 1] : [13, 15, 18, 1];
                                function checkEl(el) {
                                    const chain = [];
                                    let cur = el, op = 1;
                                    while (cur && cur.nodeType === 1) {
                                        const ccs = getComputedStyle(cur);
                                        chain.unshift(ccs);
                                        op *= parseFloat(ccs.opacity || '1');
                                        cur = cur.parentElement;
                                    }
                                    let bg = baseInit;
                                    for (const lcs of chain) bg = applyLayer(lcs, bg);
                                    const fgRaw = parseColor(getComputedStyle(el).color);
                                    const fg = blend([fgRaw[0], fgRaw[1], fgRaw[2], fgRaw[3] * op], bg);
                                    return cr(fg, bg);
                                }
                                const sub55 = [];
                                const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                                let n, checked = 0;
                                while ((n = w.nextNode())) {
                                    const t = (n.nodeValue || '').trim();
                                    if (!t) continue;
                                    const el = n.parentElement;
                                    const cs = getComputedStyle(el);
                                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                                    const r = el.getBoundingClientRect();
                                    if (!r.width || !r.height) continue;
                                    checked++;
                                    const ratio = checkEl(el);
                                    if (ratio < 5.5) {
                                        sub55.push({cls: String(el.className || '').slice(0, 50), txt: t.slice(0, 30), ratio: +ratio.toFixed(2)});
                                    }
                                }
                                const statusEl = document.getElementById('ai-status-pill');
                                const deskEl = document.querySelector('.desk-box');
                                const inactiveBtn = document.querySelector('.ai-btn.is-inactive');
                                return {
                                    title: document.title,
                                    checked,
                                    sub55,
                                    statusCr: statusEl ? checkEl(statusEl) : 99,
                                    deskCr: deskEl ? checkEl(deskEl) : 99,
                                    inactiveCr: inactiveBtn ? checkEl(inactiveBtn) : 99,
                                    inactiveOpacity: inactiveBtn ? parseFloat(getComputedStyle(inactiveBtn).opacity) : 1
                                };
                            }""")
                            self.assertNotIn("ログイン", staff_dom["title"], f"Expected authenticated staff page on {sroute}")
                            self.assertGreater(staff_dom["checked"], 20)
                            self.assertEqual(
                                staff_dom["sub55"],
                                [],
                                f"{sroute} [light={is_light}] text nodes below 5.5:1 contrast: {staff_dom['sub55']}",
                            )
                            self.assertGreaterEqual(staff_dom["statusCr"], 5.5)
                            self.assertGreaterEqual(staff_dom["deskCr"], 5.5)
                            self.assertGreaterEqual(staff_dom["inactiveCr"], 5.5)
                            self.assertEqual(staff_dom["inactiveOpacity"], 1.0)
                    ctx_staff.close()
                    ctx_pc.close()

                    # 4. Mobile 390x844 .moat-pill widow-wrap check & horizontal swipe nav check
                    ctx_mob = browser.new_context(viewport={"width": 390, "height": 844})
                    attach_router(ctx_mob, self.client)
                    mpage = ctx_mob.new_page()
                    mpage.goto(base + "/", wait_until="domcontentloaded")
                    mpage.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                    mpage.wait_for_timeout(120)
                    mob_state = mpage.evaluate("""() => {
                        const pills = Array.from(document.querySelectorAll('.moat-pill')).map(el => ({
                            text: el.innerText.trim(),
                            rects: el.getClientRects().length,
                            height: el.getBoundingClientRect().height
                        }));
                        const nav = document.querySelector('.portal-global-nav');
                        const ncs = getComputedStyle(nav);
                        return {pills, navWrap: ncs.flexWrap, navOverflowX: ncs.overflowX};
                    }""")
                    self.assertGreaterEqual(len(mob_state["pills"]), 2)
                    for pill in mob_state["pills"]:
                        self.assertEqual(pill["rects"], 1, f"Mobile 390px .moat-pill wrapped onto multiple lines: {pill}")
                        self.assertLessEqual(pill["height"], 24)
                    self.assertEqual(mob_state["navWrap"], "nowrap")
                    self.assertEqual(mob_state["navOverflowX"], "auto")
                    ctx_mob.close()
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
