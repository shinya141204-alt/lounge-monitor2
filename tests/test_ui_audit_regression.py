# -*- coding: utf-8 -*-
"""
tests/test_ui_audit_regression.py - UI・コントラスト・キャッシュバスティング・ブランド表記 回帰テストスイート

独立監査の9項目に対する自動回帰テスト:
1. Cache-Control: immutable 対応のキャッシュバスティング同期 (calm.css?v=23, portal.css?v=10, app.js?v=20261101_3, affiliate_config.js?v=13, sw.js?v=74, aiseki-navi-v75)
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
        expected_calm = "/static/calm.css?v=33"
        expected_portal = "/static/portal.css?v=14"
        expected_app_js = "/static/app.js?v=20261105_2"
        expected_aff_js = "/static/affiliate_config.js?v=16"
        expected_style = "/static/style.css?v=20261030_6"
        expected_sw_url = "/sw.js?v=91"
        expected_cache_name = "aiseki-navi-v91"

        templates_dir = os.path.join(PROJECT_ROOT, "templates")
        calm_seen = 0
        portal_seen = 0
        app_js_seen = 0
        aff_js_seen = 0
        style_seen = 0
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

            for m in re.findall(r"/static/affiliate_config\.js(?:\?v=[^\"'\s>]+)?", txt):
                self.assertEqual(m, expected_aff_js, f"Outdated affiliate_config.js ref in {fname}: {m}")
                aff_js_seen += 1

            for m in re.findall(r"/static/style\.css(?:\?v=[^\"'\s>]+)?", txt):
                self.assertEqual(m, expected_style, f"Outdated style.css ref in {fname}: {m}")
                style_seen += 1

            for m in re.findall(r"/sw\.js(?:\?v=[^\"'\s>)]+)?", txt):
                self.assertEqual(m, expected_sw_url, f"Outdated sw.js ref in {fname}: {m}")
                sw_reg_seen += 1

        self.assertGreaterEqual(calm_seen, 29)
        self.assertGreaterEqual(portal_seen, 28)
        self.assertEqual(app_js_seen, 2)  # preload + defer script in index.html
        self.assertEqual(aff_js_seen, 1)  # defer script in index.html
        self.assertGreaterEqual(style_seen, 34)
        self.assertEqual(sw_reg_seen, 2)  # index.html + _nav_loader.html

        sw_path = os.path.join(PROJECT_ROOT, "static", "sw.js")
        with open(sw_path, "r", encoding="utf-8") as fp:
            sw_txt = fp.read()
        self.assertIn(f"CACHE_NAME = '{expected_cache_name}'", sw_txt)
        self.assertIn("aiseki-navi-v47", sw_txt)
        self.assertIn(f"'{expected_style}'", sw_txt)
        self.assertIn(f"'{expected_calm}'", sw_txt)
        self.assertIn(f"'{expected_portal}'", sw_txt)
        self.assertIn(f"'{expected_app_js}'", sw_txt)
        self.assertIn(f"'{expected_aff_js}'", sw_txt)

        for asset_url in (expected_calm, expected_portal, expected_app_js, expected_aff_js, expected_style):
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

        # PC vertical scroll guard: html selector in calm.css must NEVER declare overflow/overflow-x/overflow-y
        calm_no_comments = re.sub(r"/\*.*?\*/", "", calm_css, flags=re.S)
        for html_block in re.findall(r"(?<![a-zA-Z0-9_.-])html\s*\{([^}]*)\}", calm_no_comments):
            self.assertIsNone(
                re.search(r"\boverflow(?:-[xy])?\s*:", html_block),
                f"html {{ ... }} must not set overflow (breaks body viewport scroll propagation on PC): {html_block}",
            )

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
            "/guide/brand-waveform-asymmetry",
            "/guide/lounge-vs-screened-date",
            "/guide/dress-code-cleanliness",
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

        fake_weather_map = {
            "tokyo": {
                "city": "東京",
                "is_rainy": True,
                "precip_mm": 2.4,
                "precip_prob": 80,
                "temp_c": 16.5,
                "label": "本降り（2.4mm）",
            }
        }

        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None), \
             patch("local_db.get_cached_store_accuracy_map", return_value=fake_acc_map), \
             patch("moat_intelligence.get_city_weather_map", return_value=fake_weather_map), \
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

                    # Clean Minimal Neutral Dark (#111113), 1-tap Light toggle (#f6f6f7), Editorial Bento (#1 hero + #2+ airy hairline row), and PC Sticky Inspector (#pc-store-inspector)
                    cm_pc_initial = page.evaluate("""() => {
                        const insp = document.getElementById('pc-store-inspector');
                        const modal = document.getElementById('analysis-modal');
                        const cards = Array.from(document.querySelectorAll('#ranking-list .ranking-item[data-store]'));
                        const hero = cards[0];
                        const row2 = cards[1];
                        const heroCs = hero ? getComputedStyle(hero) : null;
                        const row2Cs = row2 ? getComputedStyle(row2) : null;
                        return {
                            bodyBg: getComputedStyle(document.body).backgroundColor,
                            inspDisplay: insp ? getComputedStyle(insp).display : 'none',
                            inspPosition: insp ? getComputedStyle(insp).position : '',
                            inspText: insp ? insp.innerText : '',
                            modalHidden: modal ? modal.classList.contains('hidden') : false,
                            heroHasCls: hero ? hero.classList.contains('bento-hero-card') : false,
                            heroRadius: heroCs ? heroCs.borderTopLeftRadius : '',
                            heroKicker: hero ? (hero.querySelector('.bento-kicker-label')?.innerText || '') : '',
                            row2HasCls: row2 ? row2.classList.contains('bento-hero-card') : true,
                            row2Bg: row2Cs ? row2Cs.backgroundColor : '',
                            row2BorderTop: row2Cs ? row2Cs.borderTopWidth : '',
                            row2BorderBottom: row2Cs ? row2Cs.borderBottomWidth : ''
                        };
                    }""")
                    self.assertEqual(cm_pc_initial["bodyBg"], "rgb(17, 17, 19)")
                    self.assertEqual(cm_pc_initial["inspDisplay"], "flex")
                    self.assertEqual(cm_pc_initial["inspPosition"], "sticky")
                    self.assertIn("オリエンタルラウンジ 新宿", cm_pc_initial["inspText"])
                    self.assertNotRegex(cm_pc_initial["inspText"], r"\bOLG\b|\bORIENTAL\b|\bYATAKOI\b")
                    self.assertTrue(cm_pc_initial["modalHidden"])
                    self.assertTrue(cm_pc_initial["heroHasCls"])
                    self.assertEqual(cm_pc_initial["heroRadius"], "24px")
                    self.assertIn("#1 PICK", cm_pc_initial["heroKicker"])
                    self.assertFalse(cm_pc_initial["row2HasCls"])
                    self.assertEqual(cm_pc_initial["row2Bg"], "rgba(0, 0, 0, 0)")
                    self.assertEqual(cm_pc_initial["row2BorderTop"], "0px")
                    self.assertEqual(cm_pc_initial["row2BorderBottom"], "1px")

                    # Clicking 2nd card on PC updates right inspector without opening #analysis-modal
                    page.click('#ranking-list .ranking-item[data-store="ag（アグ）渋谷"]')
                    page.wait_for_timeout(120)
                    cm_pc_switched = page.evaluate("""() => ({
                        inspTitle: document.querySelector('#pc-store-inspector .cmi-title')?.innerText || '',
                        inspText: document.getElementById('pc-store-inspector')?.innerText || '',
                        selectedStore: document.querySelector('#ranking-list .ranking-item.is-inspector-selected')?.getAttribute('data-store') || '',
                        modalHidden: document.getElementById('analysis-modal').classList.contains('hidden')
                    })""")
                    self.assertEqual(cm_pc_switched["selectedStore"], "ag（アグ）渋谷")
                    self.assertIn("ag", cm_pc_switched["inspTitle"])
                    self.assertIn("渋谷", cm_pc_switched["inspTitle"])
                    self.assertNotRegex(cm_pc_switched["inspText"], r"\bOLG\b|\bORIENTAL\b|\bYATAKOI\b")
                    self.assertTrue(cm_pc_switched["modalHidden"])

                    # Header 1-tap daylight toggle switches between Neutral Dark (#111113) and Clean Light (#f6f6f7)
                    page.click("#quick-daylight-btn")
                    page.wait_for_timeout(60)
                    self.assertEqual(page.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(246, 246, 247)")
                    page.click("#quick-daylight-btn")
                    page.wait_for_timeout(60)
                    self.assertEqual(page.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(17, 17, 19)")

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
                        page.evaluate("openModal('オリエンタルラウンジ 新宿')")
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

                    # 2b. PC wheel/trackpad vertical scroll & modal background scroll lock/restore regression check
                    for scroll_route in ("/", "/store/oriental-lounge-shinjuku", "/columns"):
                        page.goto(base + scroll_route, wait_until="domcontentloaded")
                        page.wait_for_timeout(120)
                        page.mouse.move(640, 450)
                        page.mouse.wheel(0, 500)
                        page.wait_for_timeout(120)
                        sc = page.evaluate("""() => ({
                            scrollY: window.scrollY,
                            maxScroll: document.documentElement.scrollHeight - window.innerHeight,
                            overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                            htmlOverflowX: getComputedStyle(document.documentElement).overflowX
                        })""")
                        self.assertEqual(sc["htmlOverflowX"], "visible", f"html overflow-x must stay visible on {scroll_route}")
                        self.assertLessEqual(sc["overflowX"], 0, f"Horizontal overflow on {scroll_route}: {sc}")
                        self.assertGreater(sc["maxScroll"], 80, f"Expected scrollable page on {scroll_route}: {sc}")
                        self.assertGreater(sc["scrollY"], 80, f"PC mouse wheel vertical scroll locked on {scroll_route}: {sc}")

                    # Verify openMenuModal() locks background wheel scroll and closeMenuModal() restores PC wheel scroll
                    page.goto(base + "/", wait_until="domcontentloaded")
                    page.wait_for_timeout(120)
                    page.evaluate("window.scrollTo(0, 0); openMenuModal();")
                    page.wait_for_timeout(100)
                    page.mouse.move(80, 80)
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(100)
                    self.assertEqual(page.evaluate("window.scrollY"), 0, "Background scrolled while #menu-modal was open")
                    page.evaluate("closeMenuModal()")
                    page.wait_for_timeout(100)
                    page.mouse.move(640, 450)
                    page.mouse.wheel(0, 500)
                    page.wait_for_timeout(120)
                    self.assertGreater(page.evaluate("window.scrollY"), 80, "PC mouse wheel scroll did not recover after closeMenuModal()")

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
                    self.assertEqual(mpage.evaluate("getComputedStyle(document.getElementById('pc-store-inspector')).display"), "none")

                    # Mobile card tap opens #analysis-modal as Clean Minimal bottom sheet (24px top radius)
                    mpage.click('#ranking-list .ranking-item[data-store="オリエンタルラウンジ 新宿"]')
                    mpage.wait_for_timeout(120)
                    sheet_state = mpage.evaluate("""() => {
                        const m = document.getElementById('analysis-modal');
                        const mc = m.querySelector('.modal-content');
                        return {
                            hidden: m.classList.contains('hidden'),
                            alignItems: getComputedStyle(m).alignItems,
                            topLeftRadius: getComputedStyle(mc).borderTopLeftRadius
                        };
                    }""")
                    self.assertFalse(sheet_state["hidden"])
                    self.assertEqual(sheet_state["alignItems"], "flex-end")
                    self.assertEqual(sheet_state["topLeftRadius"], "24px")
                    mpage.evaluate("closeModal()")

                    # 5. Functional UX on Mobile 390px: kana search, 1-tap clear, active filter summary, and 0-hit recovery
                    self.assertTrue(mpage.locator("#search-clear-btn").is_hidden())
                    self.assertTrue(mpage.locator("#active-filter-summary").is_hidden())

                    mpage.fill("#search-input", "しんじゅく")
                    mpage.wait_for_timeout(80)
                    self.assertFalse(mpage.locator("#search-clear-btn").is_hidden())
                    self.assertFalse(mpage.locator("#active-filter-summary").is_hidden())
                    summary_txt = mpage.locator("#active-filter-metrics").inner_text()
                    self.assertIn("店舗", summary_txt)
                    self.assertGreaterEqual(mpage.locator("#ranking-list .ranking-item").count(), 1)

                    mpage.click("#search-clear-btn")
                    mpage.wait_for_timeout(80)
                    self.assertEqual(mpage.input_value("#search-input"), "")
                    self.assertTrue(mpage.locator("#search-clear-btn").is_hidden())
                    self.assertTrue(mpage.locator("#active-filter-summary").is_hidden())

                    # Area filter -> summary strip -> 1-tap reset
                    mpage.click('.filter-btn[data-region="City:shinjuku"]')
                    mpage.wait_for_timeout(80)
                    self.assertFalse(mpage.locator("#active-filter-summary").is_hidden())
                    self.assertIn("新宿", mpage.locator("#active-filter-metrics").inner_text())
                    mpage.click(".afs-reset-btn")
                    mpage.wait_for_timeout(80)
                    self.assertTrue(mpage.locator("#active-filter-summary").is_hidden())

                    # 0-hit recovery button restores full store list
                    mpage.fill("#search-input", "存在しない店舗テスト999")
                    mpage.wait_for_timeout(80)
                    self.assertEqual(mpage.locator(".empty-filter-reset-btn").count(), 1)
                    mpage.click(".empty-filter-reset-btn")
                    mpage.wait_for_timeout(80)
                    self.assertEqual(mpage.input_value("#search-input"), "")
                    self.assertGreaterEqual(mpage.locator("#ranking-list .ranking-item").count(), 2)

                    # 6. Sitewide Option A (Apple Editorial & Weather Bento) verification on Mobile 390px & PC 1280px
                    for sub_route in (
                        "/concierge",
                        "/store/oriental-lounge-shinjuku",
                        "/columns",
                        "/for-women",
                        "/price-comparison",
                        "/guide/brand-waveform-asymmetry",
                        "/guide/lounge-vs-screened-date",
                        "/guide/dress-code-cleanliness",
                    ):
                        mpage.goto(base + sub_route, wait_until="domcontentloaded")
                        mpage.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                        mob_sub = mpage.evaluate("""() => {
                            const inactiveNav = document.querySelector('.portal-nav-item:not(.active):not(.portal-theme-toggle-btn)');
                            const hero = document.querySelector('.page-hero, .hero-box, .article-header');
                            const h2 = document.querySelector('.sec-title, .section-title, .section-card h2, h2.sec-h2, .article-body h2');
                            return {
                                overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                                navBg: inactiveNav ? getComputedStyle(inactiveNav).backgroundColor : '',
                                navBorderColor: inactiveNav ? getComputedStyle(inactiveNav).borderTopColor : '',
                                heroBg: hero ? getComputedStyle(hero).backgroundColor : null,
                                heroBorderTop: hero ? getComputedStyle(hero).borderTopWidth : null,
                                h2BorderLeft: h2 ? getComputedStyle(h2).borderLeftWidth : '0px'
                            };
                        }""")
                        self.assertLessEqual(mob_sub["overflowX"], 0, f"Mobile 390px horizontal overflow on {sub_route}: {mob_sub}")
                        self.assertEqual(mob_sub["navBg"], "rgba(0, 0, 0, 0)", f"Inactive nav must be borderless/transparent on {sub_route}")
                        self.assertEqual(mob_sub["navBorderColor"], "rgba(0, 0, 0, 0)", f"Inactive nav border must be transparent on {sub_route}")
                        if mob_sub["heroBg"] is not None:
                            self.assertEqual(mob_sub["heroBg"], "rgba(0, 0, 0, 0)", f"Open hero must have transparent bg on {sub_route}")
                            self.assertEqual(mob_sub["heroBorderTop"], "0px", f"Open hero must remove box border on {sub_route}")
                        self.assertEqual(mob_sub["h2BorderLeft"], "0px", f"h2 left vertical bar must be removed on {sub_route}")

                    ctx_mob.close()

                    ctx_apple_pc = browser.new_context(viewport={"width": 1280, "height": 900})
                    attach_router(ctx_apple_pc, self.client)
                    ap_page = ctx_apple_pc.new_page()
                    for sub_route, bento_sel in (
                        ("/concierge", ".verdict-card--primary"),
                        ("/store/oriental-lounge-shinjuku", ".live-hero-card"),
                        ("/columns", ".articles-grid > .article-card:first-child"),
                        ("/for-women", ".live-board"),
                        ("/price-comparison", ".apple-compare-col"),
                    ):
                        ap_page.goto(base + sub_route, wait_until="domcontentloaded")
                        ap_page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                        for is_light in (False, True):
                            ap_page.evaluate(f"document.body.classList.toggle('theme-light', {str(is_light).lower()})")
                            bento_state = ap_page.evaluate(f"""() => {{
                                const el = document.querySelector({json.dumps(bento_sel)});
                                const cs = el ? getComputedStyle(el) : null;
                                return {{
                                    exists: !!el,
                                    radius: cs ? cs.borderTopLeftRadius : '',
                                    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth
                                }};
                            }}""")
                            self.assertTrue(bento_state["exists"], f"Missing {bento_sel} on {sub_route}")
                            self.assertEqual(bento_state["radius"], "24px", f"Expected 24px Apple Bento radius for {bento_sel} on {sub_route} (light={is_light})")
                            self.assertLessEqual(bento_state["overflowX"], 0)
                    ctx_apple_pc.close()
                finally:
                    browser.close()

    # ------------------------------------------------------------------
    # 7. Anti-AI Design Guard: zero neon gradients & tabular-nums enforcement
    # ------------------------------------------------------------------
    def test_anti_ai_zero_neon_gradients_and_tabular_nums(self):
        """全公開テンプレートに旧ネオンピンク×紫グラデーションが残存せず、主要数値に tabular-nums が適用されていること"""
        neon_re = re.compile(r"linear-gradient\([^)]*(?:#ec4899|#8b5cf6|#3b82f6|#d946ef|#f43f5e)", re.I)
        templates_dir = os.path.join(PROJECT_ROOT, "templates")
        offenders = []
        for fname in sorted(os.listdir(templates_dir)):
            if not fname.endswith(".html") or fname.startswith("staff_"):
                continue
            with open(os.path.join(templates_dir, fname), "r", encoding="utf-8") as fp:
                txt = fp.read()
            hits = neon_re.findall(txt)
            if hits:
                offenders.append((fname, hits[:2]))
        self.assertEqual(offenders, [], f"Legacy neon gradients found in public templates: {offenders}")

        calm_path = os.path.join(PROJECT_ROOT, "static", "calm.css")
        with open(calm_path, "r", encoding="utf-8") as fp:
            calm_txt = fp.read()
        self.assertIn("font-variant-numeric: tabular-nums", calm_txt)
        self.assertIn(".search-clear-btn", calm_txt)
        self.assertIn(".active-filter-summary", calm_txt)
        self.assertIn(".empty-filter-reset-btn", calm_txt)

        nav_path = os.path.join(templates_dir, "_portal_nav.html")
        with open(nav_path, "r", encoding="utf-8") as fp:
            nav_txt = fp.read()
        for banned_emoji in ("🧭", "💄", "☀️", "🌙"):
            self.assertNotIn(banned_emoji, nav_txt)

    # ------------------------------------------------------------------
    # 9. Price slot pills (/ 10分 display), JIS charge accuracy & CSS subgrid row alignment
    # ------------------------------------------------------------------
    def test_price_slot_pills_and_subgrid_row_alignment(self):
        """全時間帯ピルで10分料金（定額店は定額プラン）が空欄にならず表示され、JIS他店への大宮U30誤表記がなく、並列カードの罫線が水平整列すること"""
        self.assertEqual(price_comparison.BRAND_PRICE_SPECS["JIS"]["charge_str"], "550円（テーブルチャージ）")
        self.assertEqual(price_comparison.STORE_PRICE_TABLE["JIS OMIYA"]["charge_str"], "550円（U30はチャージ0円）")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed in this Python environment")

        osaka_stores = [
            {"name": "OLG 難波", "source": "oriental", "women": 16, "men": 9, "total": 25, "ratio": 1.78, "timestamp": "2026-11-02 21:15:00"},
            {"name": "JIS NAMBA", "source": "jis", "women": 11, "men": 7, "total": 18, "ratio": 1.57, "timestamp": "2026-11-02 21:15:00"},
            {"name": "ASOBIBAR 心斎橋", "source": "asobibar", "women": 14, "men": 12, "total": 26, "ratio": 1.17, "timestamp": "2026-11-02 21:15:00"},
        ]
        with app_module.data_lock:
            app_module.latest_data["full_data"] = osaka_stores
            app_module.latest_data["top5"] = osaka_stores
            app_module.latest_data["last_updated"] = "2026-11-02 21:15:00"
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

        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    for vw, vh in ((1280, 900), (390, 844)):
                        ctx = browser.new_context(viewport={"width": vw, "height": vh}, service_workers="block")
                        attach_router(ctx, self.client)
                        page = ctx.new_page()

                        # A. /price-comparison?area=osaka-minami
                        page.goto(base + "/price-comparison?area=osaka-minami", wait_until="domcontentloaded")
                        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")

                        jis_text = page.locator('#cluster-osaka-minami .apple-compare-col[data-slug="jis-namba"]').inner_text()
                        self.assertIn("テーブルチャージ", jis_text)
                        self.assertNotIn("大宮U30", jis_text)

                        for slot_key in ("slot1820", "slot2022", "slot2224", "slot24Last", "auto"):
                            page.click(f'[data-slot-select="{slot_key}"]')
                            state = page.evaluate("""() => {
                                const cards = Array.from(document.querySelectorAll('#cluster-osaka-minami .apple-compare-col'));
                                const cardInfos = cards.map(c => {
                                    const rateEl = c.querySelector('[data-active-slot-rate]');
                                    const unitEl = c.querySelector('[data-slot-unit]');
                                    const labelRowEl = rateEl ? rateEl.closest('.apple-spec-row')?.querySelector('.apple-spec-label') : null;
                                    return {
                                        name: c.dataset.liveStore,
                                        isFlat: c.dataset.isFlat === '1',
                                        rate: rateEl ? (rateEl.textContent || '').trim() : '',
                                        unitHidden: unitEl ? unitEl.style.display === 'none' : true,
                                        label: labelRowEl ? (labelRowEl.textContent || '').trim() : ''
                                    };
                                });
                                const catCards = Array.from(document.querySelectorAll('#catalog-spec-grid .apple-compare-col')).slice(0, 8).map(c => ({
                                    isFlat: c.dataset.isFlat === '1',
                                    rate: (c.querySelector('[data-cat-slot-rate]')?.textContent || '').trim(),
                                    unitHidden: c.querySelector('[data-cat-slot-unit]')?.style.display === 'none'
                                }));
                                const customVals = Array.from(document.querySelectorAll('#apple-custom-compare-stage .apple-compare-col')).map(c => {
                                    const rows = c.querySelectorAll('.apple-spec-row .apple-spec-val-lg');
                                    return rows.length ? (rows[0].textContent || '').trim() : '';
                                });

                                // Check horizontal row alignment between side-by-side cards in #cluster-osaka-minami
                                const misalignments = [];
                                const byTop = [];
                                for (const col of cards) {
                                    const r = col.getBoundingClientRect();
                                    let grp = byTop.find(g => Math.abs(g.top - r.top) <= 6);
                                    if (!grp) { grp = {top: r.top, cols: []}; byTop.push(grp); }
                                    grp.cols.push(col);
                                }
                                for (const grp of byTop) {
                                    if (grp.cols.length < 2) continue;
                                    const rowLists = grp.cols.map(c => Array.from(c.querySelectorAll(':scope > .apple-spec-row')));
                                    const minLen = Math.min(...rowLists.map(l => l.length));
                                    for (let i = 0; i < minLen; i++) {
                                        const tops = rowLists.map(l => l[i].getBoundingClientRect().top);
                                        const diff = Math.max(...tops) - Math.min(...tops);
                                        if (diff > 2) misalignments.push({rowIdx: i, diff: +diff.toFixed(1)});
                                    }
                                }
                                return {cardInfos, catCards, customVals, misalignments};
                            }""")

                            self.assertGreaterEqual(len(state["cardInfos"]), 3)
                            for ci in state["cardInfos"]:
                                self.assertTrue(ci["rate"], f"Empty slot rate for {ci['name']} on {slot_key} ({vw}px)")
                                if ci["isFlat"]:
                                    self.assertTrue(ci["unitHidden"], f"Flat-rate store {ci['name']} should hide / 10分 on {slot_key}")
                                    self.assertIn("定額", ci["label"])
                                else:
                                    self.assertFalse(ci["unitHidden"], f"Metered store {ci['name']} must show / 10分 on {slot_key}")
                                    self.assertIn("相席10分料金", ci["label"])

                            for cc in state["catCards"]:
                                self.assertTrue(cc["rate"], f"Empty catalog slot rate on {slot_key}")
                                self.assertEqual(cc["unitHidden"], cc["isFlat"])

                            for cv in state["customVals"]:
                                self.assertTrue(cv, f"Empty custom compare rate on {slot_key}")

                            self.assertEqual(state["misalignments"], [], f"Subgrid row misalignment at {vw}px ({slot_key}): {state['misalignments']}")

                        # B. /store/oriental-lounge-namba nearby competitor slot pills
                        page.goto(base + "/store/oriental-lounge-namba", wait_until="domcontentloaded")
                        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
                        for slot_key in ("slot1820", "slot2022", "slot2224", "slot24Last", "auto"):
                            page.click(f'[data-nearby-slot-btn="{slot_key}"]')
                            nb_state = page.evaluate("""() => {
                                const cols = Array.from(document.querySelectorAll('[data-nearby-comp-row]'));
                                return cols.map(c => ({
                                    name: c.dataset.nearbyCompRow,
                                    isFlat: c.dataset.isFlat === '1',
                                    rate: (c.querySelector('[data-nearby-slot-rate]')?.textContent || '').trim(),
                                    unitHidden: c.querySelector('[data-nearby-slot-unit]')?.style.display === 'none'
                                }));
                            }""")
                            self.assertGreaterEqual(len(nb_state), 2)
                            for nb in nb_state:
                                self.assertTrue(nb["rate"], f"Empty nearby slot rate for {nb['name']} on {slot_key}")
                                self.assertEqual(nb["unitHidden"], nb["isFlat"])

                        ctx.close()
                finally:
                    browser.close()

    # ------------------------------------------------------------------
    # 10. Hero pill link (.columns-hero-pill-link) & zero unstyled/purple UA links
    # ------------------------------------------------------------------
    def test_columns_hero_pill_link_and_zero_unstyled_ua_links(self):
        """/columns の .columns-hero-pill-link（:visited 含む）および全公開テンプレートのリンククラスが未定義青・紫にならないこと"""
        static_dir = os.path.join(PROJECT_ROOT, "static")
        templates_dir = os.path.join(PROJECT_ROOT, "templates")

        with open(os.path.join(static_dir, "calm.css"), "r", encoding="utf-8") as fp:
            calm_css = fp.read()
        self.assertIn("body:not(.theme-light):not(.staff-mode) .columns-hero-pill-link,", calm_css)
        self.assertIn("body:not(.theme-light):not(.staff-mode) .columns-hero-pill-link:visited", calm_css)
        self.assertIn("body.theme-light:not(.staff-mode) .columns-hero-pill-link,", calm_css)
        self.assertIn("body.theme-light:not(.staff-mode) .columns-hero-pill-link:visited", calm_css)

        # Static audit: every static class-equipped <a> in public templates must have at least one defined CSS class
        css_chunks = []
        for fn in sorted(os.listdir(static_dir)):
            if fn.endswith(".css"):
                with open(os.path.join(static_dir, fn), "r", encoding="utf-8") as fp_css:
                    css_chunks.append(fp_css.read())
        all_css = "\n".join(css_chunks)
        undefined_anchor_classes = []
        for fn in sorted(os.listdir(templates_dir)):
            if not fn.endswith(".html") or fn.startswith("staff"):
                continue
            with open(os.path.join(templates_dir, fn), "r", encoding="utf-8") as fp_tpl:
                raw = fp_tpl.read()
            local_styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", raw, re.S))
            combined_css = all_css + "\n" + local_styles
            for m in re.finditer(r"<a\b([^>]*)>", raw, re.S):
                attrs = m.group(1)
                if "style=" in attrs and "color" in attrs:
                    continue
                cm = re.search(r"class=[\"\']([^\"\']+)[\"\']", attrs)
                if cm and "{{" not in cm.group(1) and "{%" not in cm.group(1):
                    classes = cm.group(1).split()
                    if classes and not any(("." + c) in combined_css for c in classes):
                        undefined_anchor_classes.append(f"{fn}: {classes}")
        self.assertEqual(undefined_anchor_classes, [], f"Undefined <a> classes found: {undefined_anchor_classes}")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed in this Python environment")

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

        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    for vw, vh in ((1280, 900), (390, 844)):
                        ctx = browser.new_context(viewport={"width": vw, "height": vh}, service_workers="block")
                        attach_router(ctx, self.client)
                        page = ctx.new_page()

                        # Visit /guide first so /guide is in visited history (:visited active)
                        page.goto(base + "/guide", wait_until="domcontentloaded")
                        page.goto(base + "/columns", wait_until="domcontentloaded")
                        page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")

                        for theme_mode, expected_rgb in (("dark", "rgb(244, 244, 245)"), ("light", "rgb(24, 24, 27)")):
                            if theme_mode == "light":
                                page.evaluate("document.body.classList.add('theme-light')")
                            else:
                                page.evaluate("document.body.classList.remove('theme-light')")

                            info = page.evaluate("""() => {
                                const el = document.querySelector('.columns-hero-pill-link');
                                if (!el) return null;
                                const cs = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                const badLinks = [];
                                for (const a of document.querySelectorAll('a[href]')) {
                                    const acs = getComputedStyle(a);
                                    if (acs.color === 'rgb(0, 0, 238)' || acs.color === 'rgb(85, 26, 139)') {
                                        badLinks.push({href: a.getAttribute('href'), cls: a.className, color: acs.color});
                                    }
                                }
                                return {
                                    color: cs.color,
                                    bg: cs.backgroundColor,
                                    textDecorationLine: cs.textDecorationLine,
                                    height: rect.height,
                                    rightOverflow: Math.max(0, Math.round(rect.right - window.innerWidth)),
                                    docOverflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
                                    badLinks
                                };
                            }""")
                            self.assertIsNotNone(info)
                            self.assertEqual(info["color"], expected_rgb, f"Unexpected color in {theme_mode} ({vw}px): {info}")
                            self.assertNotIn(info["bg"], ("rgba(0, 0, 0, 0)", "transparent"), f"Missing pill surface in {theme_mode}: {info}")
                            self.assertEqual(info["textDecorationLine"], "none")
                            self.assertGreaterEqual(info["height"], 38, f"Tap target too small at {vw}px: {info['height']}")
                            self.assertEqual(info["rightOverflow"], 0, f"Pill overflows viewport at {vw}px: {info}")
                            self.assertEqual(info["docOverflowX"], 0, f"Document horizontal overflow at {vw}px: {info}")
                            self.assertEqual(info["badLinks"], [], f"Unstyled UA blue/purple links on /columns: {info['badLinks']}")

                        ctx.close()
                finally:
                    browser.close()

    # ------------------------------------------------------------------
    # 13. Daily analytics persistence, contiguous calendar & proportional bar heights + Master UI
    # ------------------------------------------------------------------
    def test_analytics_web_flusher_contiguous_days_and_proportional_bars(self):
        """APP_ROLE=web 2段階フラッシュ・連続カレンダー日生成・棒グラフ実ピクセル比例高さ・マスター共通ナビを検証"""
        import datetime
        from urllib.parse import urlsplit
        import analytics
        import sheets_store
        import turbo_store

        analytics.reset_for_tests()
        try:
            # 1) Verify two-tier flush: force_sheets=False writes to Postgres immediately and defers Sheets until force_sheets=True
            pg_batches = []
            sheet_batches = []

            def fake_pg_batch(items):
                pg_batches.append(dict(items))
                return True

            def fake_sheet_append(title, header, rows):
                sheet_batches.append(list(rows))
                return True

            with patch.object(turbo_store, "is_postgres_active", return_value=True), \
                 patch.object(turbo_store, "analytics_add_batch", side_effect=fake_pg_batch), \
                 patch.object(sheets_store, "append_rows", side_effect=fake_sheet_append):
                analytics.track("page_view", "/")
                analytics.track("page_view", "/")
                flushed_pg = analytics.flush(force_sheets=False)
                self.assertEqual(flushed_pg, 1)
                self.assertEqual(len(pg_batches), 1)
                self.assertEqual(len(sheet_batches), 0, "force_sheets=False must not hit Google Sheets API")

                analytics.track("page_view", "/columns")
                app_module._flush_web_analytics_on_exit()
                self.assertEqual(len(pg_batches), 2)
                self.assertEqual(len(sheet_batches), 1, "Exit/10m flush must flush accumulated retry + new items to Sheets")
                total_sheet_count = sum(int(r[3]) for r in sheet_batches[0])
                self.assertEqual(total_sheet_count, 3)

            # 2) Verify contiguous 7-day calendar series & accurate proportional bar heights in real Chromium
            analytics.reset_for_tests()
            jst = datetime.timezone(datetime.timedelta(hours=9))
            today_dt = datetime.datetime.now(jst).date()
            d_6ago = (today_dt - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
            d_3ago = (today_dt - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
            d_today = today_dt.strftime("%Y-%m-%d")

            mock_rows = [
                [d_6ago, "page_view", "/", "100"],
                [d_3ago, "page_view", "/", "50"],
                [d_today, "page_view", "/", "25"],
                [d_today, "affiliate_click", "orbis @ /grooming", "2"],
            ]

            with patch.object(sheets_store, "read_rows", side_effect=lambda *a, **kw: mock_rows):
                s7 = analytics.summary(days=7, allow_blocking=True)
                self.assertEqual(len(s7["daily_series"]), 7, "7-day summary must contain all 7 contiguous calendar days")
                zero_days = [d for d in s7["daily_series"] if d["pv"] == 0]
                self.assertEqual(len(zero_days), 4, "Missing days must be filled with 0 PV entries")
                self.assertEqual(s7["revenue"]["pace_denom_days"], 7, "Monthly pace denominator must span full 7 days since earliest record")

                try:
                    from playwright.sync_api import sync_playwright
                except ImportError:
                    self.skipTest("playwright is not installed")

                base = "http://aiseki.local"

                def attach_master_router(context):
                    def handle_route(route):
                        req = route.request
                        u = urlsplit(req.url)
                        if u.netloc != "aiseki.local":
                            route.abort()
                            return
                        path_qs = u.path + (f"?{u.query}" if u.query else "")
                        with self.client.session_transaction() as sess:
                            sess["is_staff"] = True
                            sess["is_master"] = True
                        resp = self.client.open(path_qs, method=req.method, headers={"User-Agent": "Mozilla/5.0"})
                        headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                        route.fulfill(status=resp.status_code, headers=headers, body=resp.get_data())
                    context.route("**/*", handle_route)

                with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        try:
                            for vw, vh in ((1280, 900), (390, 844)):
                                ctx = browser.new_context(viewport={"width": vw, "height": vh}, service_workers="block")
                                attach_master_router(ctx)
                                page = ctx.new_page()
                                page.goto(base + "/staff/analytics?days=7", wait_until="domcontentloaded")
                                page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")

                                for theme_mode in ("dark", "light"):
                                    if theme_mode == "light":
                                        page.evaluate("document.body.classList.add('theme-light')")
                                    else:
                                        page.evaluate("document.body.classList.remove('theme-light')")

                                    chart_audit = page.evaluate("""() => {
                                        const dock = document.querySelector('.master-global-dock');
                                        const jumpbar = document.querySelector('.master-section-jumpbar');
                                        const cols = Array.from(document.querySelectorAll('.daily-chart-bars .day-col')).map(c => {
                                            const bar = c.querySelector('.day-col-bar');
                                            return {
                                                date: c.dataset.date,
                                                pv: Number(c.dataset.pv || 0),
                                                barHeight: bar ? bar.getBoundingClientRect().height : 0
                                            };
                                        });
                                        return {
                                            hasDock: !!dock,
                                            hasJumpbar: !!jumpbar,
                                            tabCount: document.querySelectorAll('.master-global-tabs .master-tab-link').length,
                                            docOverflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
                                            cols
                                        };
                                    }""")
                                    self.assertTrue(chart_audit["hasDock"])
                                    self.assertTrue(chart_audit["hasJumpbar"])
                                    self.assertEqual(chart_audit["tabCount"], 6)
                                    self.assertEqual(chart_audit["docOverflowX"], 0, f"Overflow on /staff/analytics at {vw}px {theme_mode}")
                                    self.assertEqual(len(chart_audit["cols"]), 7)

                                    by_pv = {c["pv"]: c["barHeight"] for c in chart_audit["cols"]}
                                    self.assertAlmostEqual(by_pv[100], 96.0, delta=1.5)
                                    self.assertAlmostEqual(by_pv[50], 48.0, delta=1.5)
                                    self.assertAlmostEqual(by_pv[25], 24.0, delta=1.5)
                                    self.assertAlmostEqual(by_pv[0], 2.0, delta=1.0)
                                    ratio_100_to_50 = by_pv[100] / max(1.0, by_pv[50])
                                    self.assertAlmostEqual(ratio_100_to_50, 2.0, delta=0.06)

                                # Also verify /staff master launcher layout & collapsible subpanels
                                page.goto(base + "/staff", wait_until="domcontentloaded")
                                staff_audit = page.evaluate("""() => {
                                    return {
                                        launcherCards: document.querySelectorAll('.master-launcher-card').length,
                                        docOverflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
                                    };
                                }""")
                                self.assertEqual(staff_audit["launcherCards"], 6)
                                self.assertEqual(staff_audit["docOverflowX"], 0, f"Overflow on /staff at {vw}px")
                                ctx.close()
                        finally:
                            browser.close()
        finally:
            analytics.reset_for_tests()

    def test_hybrid_odometer_and_oneshot_edge_motion_in_chromium(self):
        """人数増加時に変化した桁のみが縦ロールし（横ズレ0px）、左端が0.9秒だけ1回ソフト点灯して消灯すること"""
        from urllib.parse import urlsplit
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")

        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update({
                "last_updated": "2026-11-04 21:00:00",
                "is_seed": False,
                "full_data": [
                    {
                        "name": "渋谷本店",
                        "source": "ORIENTAL",
                        "region": "Kanto",
                        "women": 14,
                        "men": 10,
                        "total": 24,
                        "women_diff": 1,
                        "men_diff": 0,
                    },
                    {
                        "name": "ag渋谷",
                        "source": "AG",
                        "region": "Kanto",
                        "women": 9,
                        "men": 8,
                        "total": 17,
                        "women_diff": 0,
                        "men_diff": 0,
                    },
                ],
            })
        app_module.clear_fast_http_caches()

        base = "http://aiseki.local"

        def attach_public_router(context):
            def handle_route(route):
                req = route.request
                u = urlsplit(req.url)
                if u.netloc != "aiseki.local":
                    route.abort()
                    return
                path_qs = u.path + (f"?{u.query}" if u.query else "")
                resp = self.client.open(path_qs, method=req.method, headers={"User-Agent": "Mozilla/5.0"})
                headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                route.fulfill(status=resp.status_code, headers=headers, body=resp.get_data())
            context.route("**/*", handle_route)

        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    ctx = browser.new_context(
                        viewport={"width": 1280, "height": 900},
                        service_workers="block",
                        reduced_motion="no-preference",
                    )
                    attach_public_router(ctx)
                    page = ctx.new_page()
                    page.goto(base + "/", wait_until="domcontentloaded")
                    page.wait_for_selector("#ranking-list .ranking-item")

                    init_state = page.evaluate("""() => {
                        return {
                            odoCount: document.querySelectorAll('.odo-wrap').length,
                            deltaUpCount: document.querySelectorAll('.ranking-item.card-delta-up').length,
                            legacyPillCount: document.querySelectorAll('.live-delta-ticker').length
                        };
                    }""")
                    self.assertEqual(init_state["odoCount"], 0, "Initial load must not bounce numbers")
                    self.assertEqual(init_state["deltaUpCount"], 0)
                    self.assertEqual(init_state["legacyPillCount"], 0)

                    with app_module.data_lock:
                        app_module.latest_data.clear()
                        app_module.latest_data.update({
                            "last_updated": "2026-11-04 21:01:00",
                            "is_seed": False,
                            "full_data": [
                                {
                                    "name": "渋谷本店",
                                    "source": "ORIENTAL",
                                    "region": "Kanto",
                                    "women": 17,
                                    "men": 10,
                                    "total": 27,
                                    "women_diff": 3,
                                    "men_diff": 0,
                                },
                                {
                                    "name": "ag渋谷",
                                    "source": "AG",
                                    "region": "Kanto",
                                    "women": 12,
                                    "men": 8,
                                    "total": 20,
                                    "women_diff": 3,
                                    "men_diff": 0,
                                },
                            ],
                        })
                    app_module.clear_fast_http_caches()

                    live_snap = page.evaluate("""async () => {
                        const beforeTxt = document.querySelector('#ranking-list .ranking-item:not(.pr-card-item) .stat.ladies .count')?.textContent;
                        const beforeTs = window.lastData && window.lastData.timestamp;
                        const fetchOk = await fetchData();
                        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                        const storeCards = document.querySelectorAll('#ranking-list .ranking-item:not(.pr-card-item)');
                        const card1 = storeCards[0];
                        const countEl1 = card1.querySelector('.stat.ladies .count');
                        const cols1 = Array.from(countEl1.querySelectorAll('.odo-col'));
                        const card2 = storeCards[1];
                        const cols2 = Array.from(card2.querySelectorAll('.stat.ladies .count .odo-col'));
                        return {
                            beforeTxt,
                            beforeTs,
                            fetchOk,
                            afterTxt: countEl1.textContent,
                            afterTs: window.lastData && window.lastData.timestamp,
                            reduced: _prefersReducedMotion(),
                            hasDeltaUp1: card1.classList.contains('card-delta-up'),
                            hasDeltaUp2: card2.classList.contains('card-delta-up'),
                            legacyPillCount: document.querySelectorAll('.live-delta-ticker').length,
                            col0HasTrack: cols1[0] ? !!cols1[0].querySelector('.odo-track') : null,
                            col0Text: cols1[0] ? cols1[0].textContent : null,
                            col1HasUpTrack: cols1[1] ? !!cols1[1].querySelector('.odo-track.is-up') : null,
                            carryGhostTop: cols2[0] ? !!cols2[0].querySelector('.odo-digit.is-ghost') : null,
                            widthDuring: countEl1.getBoundingClientRect().width,
                            inspectorHasOdo: !!document.querySelector('#pc-store-inspector .cmi-count-val.is-w .odo-wrap')
                        };
                    }""")
                    self.assertTrue(live_snap["hasDeltaUp1"], msg=f"live_snap={live_snap}")
                    self.assertTrue(live_snap["hasDeltaUp2"])
                    self.assertEqual(live_snap["legacyPillCount"], 0)
                    self.assertFalse(live_snap["col0HasTrack"], "Unchanged tens digit '1' must remain completely static")
                    self.assertEqual(live_snap["col0Text"], "1")
                    self.assertTrue(live_snap["col1HasUpTrack"], "Changed ones digit '4 -> 7' must roll up")
                    self.assertTrue(live_snap["carryGhostTop"], "Carry '9 -> 12' must use invisible ghost slot for new tens digit")
                    self.assertTrue(live_snap["inspectorHasOdo"], "PC right inspector must synchronize odometer roll")

                    page.wait_for_timeout(1050)
                    settled = page.evaluate("""() => {
                        const card1 = document.querySelector('#ranking-list .ranking-item');
                        const countEl1 = card1.querySelector('.stat.ladies .count');
                        return {
                            odoCount: document.querySelectorAll('.odo-wrap').length,
                            hasDeltaUp1: card1.classList.contains('card-delta-up'),
                            finalText: countEl1.textContent,
                            widthAfter: countEl1.getBoundingClientRect().width
                        };
                    }""")
                    self.assertEqual(settled["odoCount"], 0, "Odometer DOM must cleanly revert to plain text after roll")
                    self.assertFalse(settled["hasDeltaUp1"], "Left-edge soft glow must extinguish after ~0.9s")
                    self.assertEqual(settled["finalText"], "17")
                    self.assertAlmostEqual(live_snap["widthDuring"], settled["widthAfter"], delta=0.8)
                    ctx.close()
                finally:
                    browser.close()

    def test_playwright_contextual_rank4_infeed_and_new_articles_contrast(self):
        """Playwright 実ブラウザで 4位直下の文脈連動インフィード導線（4パターン出し分け・スタッフ非表示・WCAG AA）と新キラー記事2本のコントラストを検証"""
        from urllib.parse import urlsplit
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed in this Python environment")

        five_stores = [
            {"name": "OLG 新宿", "source": "oriental", "region": "Kanto", "women": 32, "men": 14, "total": 46, "women_diff": 2, "men_diff": 0},
            {"name": "OLG 渋谷本店", "source": "oriental", "region": "Kanto", "women": 28, "men": 12, "total": 40, "women_diff": 1, "men_diff": 0},
            {"name": "JIS 梅田", "source": "jis", "region": "Kinki", "women": 26, "men": 11, "total": 37, "women_diff": 0, "men_diff": 1},
            {"name": "OLG 恵比寿", "source": "oriental", "region": "Kanto", "women": 22, "men": 10, "total": 32, "women_diff": 0, "men_diff": 0},
            {"name": "OLG ag渋谷", "source": "oriental", "region": "Kanto", "women": 18, "men": 9, "total": 27, "women_diff": 0, "men_diff": 0},
        ]
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update({
                "last_updated": "2026-11-04 20:45:00",
                "is_seed": False,
                "full_data": copy.deepcopy(five_stores),
            })
        app_module.clear_fast_http_caches()

        base = "http://aiseki.local"

        def attach_public_router(context):
            def handle_route(route):
                u = urlsplit(route.request.url)
                if u.netloc != "aiseki.local":
                    route.abort()
                    return
                path_qs = u.path + (f"?{u.query}" if u.query else "")
                resp = self.client.open(path_qs, method=route.request.method, headers={"User-Agent": "Mozilla/5.0"})
                headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                route.fulfill(status=resp.status_code, headers=headers, body=resp.get_data())
            context.route("**/*", handle_route)

        with patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
                    attach_public_router(ctx)
                    page = ctx.new_page()
                    page.goto(base + "/", wait_until="domcontentloaded")
                    page.wait_for_selector("#ranking-list .ranking-item")

                    slot_info = page.evaluate("""() => {
                        const children = Array.from(document.getElementById('ranking-list').children);
                        const prNodes = document.querySelectorAll('#ranking-list .context-lead-row.pr-card-item');
                        const slot = prNodes[0];
                        const mainLink = slot ? slot.querySelector('.pr-context-main') : null;
                        const subLink = slot ? slot.querySelector('.pr-editorial-sublink') : null;
                        const pill = slot ? slot.querySelector('.pr-disclosure-pill') : null;
                        return {
                            prCount: prNodes.length,
                            slotIndexInList: slot ? children.indexOf(slot) : -1,
                            hasRankingItemClass: slot ? slot.classList.contains('ranking-item') : true,
                            hasFakeBar: slot ? !!slot.querySelector('.pr-occupancy-bar') : true,
                            pillText: pill ? pill.textContent.trim() : '',
                            relAttr: mainLink ? (mainLink.getAttribute('rel') || '') : '',
                            subHref: subLink ? (subLink.getAttribute('href') || '') : '',
                        };
                    }""")
                    self.assertEqual(slot_info["prCount"], 1, "Exactly one contextual slot must appear")
                    self.assertEqual(slot_info["slotIndexInList"], 4, "Slot must appear immediately after 4th store (index 4)")
                    self.assertFalse(slot_info["hasRankingItemClass"], "Must not collide with .ranking-item store selectors")
                    self.assertFalse(slot_info["hasFakeBar"], "Must not render legacy fake gender ratio bar")
                    self.assertEqual(slot_info["pillText"], "PR")
                    self.assertIn("sponsored", slot_info["relAttr"])
                    self.assertTrue(slot_info["subHref"].startswith("/guide/"))

                    # Verify all 4 contextual modes via window.resolveInfeedContext + live DOM switching
                    modes = page.evaluate("""async () => {
                        const cfg = AFFILIATE_MASTER.infeedSlots[0];
                        const origTs = window.lastData && window.lastData.timestamp;
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 20:30:00';
                        const evening = window.resolveInfeedContext(cfg, [
                            { women: 30, men: 12 }, { women: 26, men: 10 }, { women: 24, men: 10 }, { women: 20, men: 9 }
                        ]);
                        const crowded = window.resolveInfeedContext(cfg, [
                            { women: 15, men: 28 }, { women: 14, men: 25 }, { women: 20, men: 12 }, { women: 18, men: 10 }
                        ]);
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 14:30:00';
                        const daytime = window.resolveInfeedContext(cfg, [
                            { women: 4, men: 2 }, { women: 3, men: 1 }, { women: 2, men: 1 }, { women: 0, men: 0 }
                        ]);
                        if (window.lastData && origTs) window.lastData.timestamp = origTs;
                        setAudienceMode('women');
                        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                        const womenDomMode = document.querySelector('#ranking-list .context-lead-row.pr-card-item')?.getAttribute('data-context-mode');
                        const womenDomHref = document.querySelector('#ranking-list .context-lead-row.pr-card-item')?.getAttribute('data-context-href');
                        setAudienceMode('men');
                        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                        return {
                            eveningMode: evening.mode,
                            eveningHref: evening.item.url,
                            crowdedMode: crowded.mode,
                            crowdedHref: crowded.item.url,
                            daytimeMode: daytime.mode,
                            daytimeHref: daytime.item.url,
                            womenDomMode,
                            womenDomHref,
                        };
                    }""")
                    self.assertEqual(modes["eveningMode"], "evening_1on1")
                    self.assertEqual(modes["eveningHref"], "/link/dating_bachelor")
                    self.assertEqual(modes["crowdedMode"], "crowded_wait")
                    self.assertEqual(modes["crowdedHref"], "/link/dating_instant")
                    self.assertEqual(modes["daytimeMode"], "daytime_prep")
                    self.assertEqual(modes["daytimeHref"], "/link/mens_hair_removal")
                    self.assertEqual(modes["womenDomMode"], "women_1on1")
                    self.assertEqual(modes["womenDomHref"], "/link/women_dating_bachelor")

                    # Contrast check on contextual card (dark & light) + staff-mode hide check
                    for is_light in (False, True):
                        page.evaluate(f"document.body.classList.toggle('theme-light', {str(is_light).lower()})")
                        colors = page.evaluate("""() => {
                            const slot = document.querySelector('#ranking-list .context-lead-row.pr-card-item');
                            const slotBg = getComputedStyle(slot).backgroundColor;
                            const cta = slot.querySelector('.pr-context-cta');
                            const ctaBg = getComputedStyle(cta).backgroundColor;
                            return {
                                slotBg,
                                titleFg: getComputedStyle(slot.querySelector('.pr-context-title')).color,
                                kickerFg: getComputedStyle(slot.querySelector('.pr-context-kicker')).color,
                                subFg: getComputedStyle(slot.querySelector('.pr-context-sub')).color,
                                subLinkFg: getComputedStyle(slot.querySelector('.pr-editorial-sublink')).color,
                                ctaBg,
                                ctaFg: getComputedStyle(cta).color,
                            };
                        }""")
                        bg_rgb = _parse_hex_or_rgba(colors["slotBg"])
                        for key in ("titleFg", "kickerFg", "subFg", "subLinkFg"):
                            cr = _contrast_ratio(_parse_hex_or_rgba(colors[key]), bg_rgb)
                            self.assertGreaterEqual(cr, 5.5, f"Contextual card {key} contrast {cr:.2f} < 5.5 (light={is_light})")
                        cta_cr = _contrast_ratio(_parse_hex_or_rgba(colors["ctaFg"]), _parse_hex_or_rgba(colors["ctaBg"]))
                        self.assertGreaterEqual(cta_cr, 5.5, f"Contextual CTA contrast {cta_cr:.2f} < 5.5 (light={is_light})")

                    staff_disp = page.evaluate("""() => {
                        document.body.classList.add('staff-mode');
                        const d = getComputedStyle(document.querySelector('#ranking-list .context-lead-row.pr-card-item')).display;
                        document.body.classList.remove('staff-mode');
                        return d;
                    }""")
                    self.assertEqual(staff_disp, "none", "Contextual promo row must hide in staff-mode")

                    # Verify 10th-rank hybrid second slot (rank10_context): SSR 2-slot placement on >=10 stores + all 6 time/audience modes
                    twelve_stores = five_stores + [
                        {"name": "OLG 町田", "source": "oriental", "region": "Kanto", "women": 17, "men": 8, "total": 25, "women_diff": 0, "men_diff": 0},
                        {"name": "OLG 横浜", "source": "oriental", "region": "Kanto", "women": 16, "men": 8, "total": 24, "women_diff": 0, "men_diff": 0},
                        {"name": "JIS 新宿", "source": "jis", "region": "Kanto", "women": 15, "men": 7, "total": 22, "women_diff": 0, "men_diff": 0},
                        {"name": "OLG 上野", "source": "oriental", "region": "Kanto", "women": 14, "men": 7, "total": 21, "women_diff": 0, "men_diff": 0},
                        {"name": "JIS 大宮", "source": "jis", "region": "Kanto", "women": 13, "men": 6, "total": 19, "women_diff": 0, "men_diff": 0},
                        {"name": "OLG 名古屋栄", "source": "oriental", "region": "Chubu", "women": 12, "men": 6, "total": 18, "women_diff": 0, "men_diff": 0},
                        {"name": "OLG 福岡", "source": "oriental", "region": "Kyushu", "women": 11, "men": 5, "total": 16, "women_diff": 0, "men_diff": 0},
                    ]
                    with app_module.data_lock:
                        app_module.latest_data.clear()
                        app_module.latest_data.update({
                            "last_updated": "2026-11-04 21:00:00",
                            "is_seed": False,
                            "full_data": copy.deepcopy(twelve_stores),
                        })
                    app_module.clear_fast_http_caches()

                    page.goto(base + "/", wait_until="domcontentloaded")
                    page.wait_for_selector('#ranking-list [data-context-slot="rank10_context"]')
                    second_slot_info = page.evaluate("""async () => {
                        const children = Array.from(document.getElementById('ranking-list').children);
                        const prNodes = Array.from(document.querySelectorAll('#ranking-list .context-lead-row.pr-card-item'));
                        const slot1 = prNodes[0];
                        const slot2 = prNodes[1];
                        const cfg2 = AFFILIATE_MASTER.infeedSlots[1];
                        const sample = [{ women: 20, men: 10 }, { women: 18, men: 9 }];
                        const origTs = window.lastData && window.lastData.timestamp;

                        if (window.lastData) window.lastData.timestamp = '2026-11-04 21:15:00';
                        const mNight = window.resolveInfeedContext(cfg2, sample);
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 19:20:00';
                        const mToast = window.resolveInfeedContext(cfg2, sample);
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 18:10:00';
                        const mSkin = window.resolveInfeedContext(cfg2, sample);
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 14:00:00';
                        const mDay = window.resolveInfeedContext(cfg2, sample);

                        setAudienceMode('women');
                        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 20:30:00';
                        const wVenue = window.resolveInfeedContext(cfg2, sample);
                        if (window.lastData) window.lastData.timestamp = '2026-11-04 23:30:00';
                        const wLate = window.resolveInfeedContext(cfg2, sample);

                        setAudienceMode('men');
                        if (window.lastData && origTs) window.lastData.timestamp = origTs;
                        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));

                        return {
                            prCount: prNodes.length,
                            slot1Key: slot1 ? slot1.getAttribute('data-context-slot') : '',
                            slot2Key: slot2 ? slot2.getAttribute('data-context-slot') : '',
                            slot1Idx: slot1 ? children.indexOf(slot1) : -1,
                            slot2Idx: slot2 ? children.indexOf(slot2) : -1,
                            slot2GridCol: slot2 ? getComputedStyle(slot2).gridColumn : '',
                            mNight: [mNight.mode, mNight.item.url],
                            mToast: [mToast.mode, mToast.item.url],
                            mSkin: [mSkin.mode, mSkin.item.url],
                            mDay: [mDay.mode, mDay.item.url],
                            wVenue: [wVenue.mode, wVenue.item.url],
                            wLate: [wLate.mode, wLate.item.url],
                        };
                    }""")
                    self.assertEqual(second_slot_info["prCount"], 2)
                    self.assertEqual(second_slot_info["slot1Key"], "rank4_context")
                    self.assertEqual(second_slot_info["slot2Key"], "rank10_context")
                    self.assertEqual(second_slot_info["slot1Idx"], 4)
                    self.assertEqual(second_slot_info["slot2Idx"], 11)
                    self.assertEqual(second_slot_info["mNight"], ["night_second_venue", "/link/second_venue_reservation"])
                    self.assertEqual(second_slot_info["mToast"], ["evening_toast_care", "/link/night_care_supplement"])
                    self.assertEqual(second_slot_info["mSkin"], ["evening_skincare", "/link/mens_skincare_clinic"])
                    self.assertEqual(second_slot_info["mDay"], ["daytime_styleup", "/link/spu_styleup"])
                    self.assertEqual(second_slot_info["wVenue"], ["women_venue_reserve", "/link/women_second_venue"])
                    self.assertEqual(second_slot_info["wLate"], ["women_late_care", "/link/women_cosme_salon"])

                    for is_light in (False, True):
                        page.evaluate(f"document.body.classList.toggle('theme-light', {str(is_light).lower()})")
                        colors2 = page.evaluate("""() => {
                            const slot = document.querySelector('#ranking-list [data-context-slot="rank10_context"]');
                            const slotBg = getComputedStyle(slot).backgroundColor;
                            const cta = slot.querySelector('.pr-context-cta');
                            return {
                                slotBg,
                                titleFg: getComputedStyle(slot.querySelector('.pr-context-title')).color,
                                kickerFg: getComputedStyle(slot.querySelector('.pr-context-kicker')).color,
                                subFg: getComputedStyle(slot.querySelector('.pr-context-sub')).color,
                                subLinkFg: getComputedStyle(slot.querySelector('.pr-editorial-sublink')).color,
                                ctaBg: getComputedStyle(cta).backgroundColor,
                                ctaFg: getComputedStyle(cta).color,
                            };
                        }""")
                        bg2 = _parse_hex_or_rgba(colors2["slotBg"])
                        for key in ("titleFg", "kickerFg", "subFg", "subLinkFg"):
                            cr2 = _contrast_ratio(_parse_hex_or_rgba(colors2[key]), bg2)
                            self.assertGreaterEqual(cr2, 5.5, f"rank10_context {key} contrast {cr2:.2f} < 5.5 (light={is_light})")
                        cta_cr2 = _contrast_ratio(_parse_hex_or_rgba(colors2["ctaFg"]), _parse_hex_or_rgba(colors2["ctaBg"]))
                        self.assertGreaterEqual(cta_cr2, 5.5, f"rank10_context CTA contrast {cta_cr2:.2f} < 5.5 (light={is_light})")

                    # Verify both new killer articles in dark & light mode
                    for art_route in ("/guide/lounge-vs-screened-date", "/guide/dress-code-cleanliness"):
                        page.goto(base + art_route, wait_until="domcontentloaded")
                        for is_light in (False, True):
                            page.evaluate(f"document.body.classList.toggle('theme-light', {str(is_light).lower()})")
                            art_colors = page.evaluate("""() => {
                                const bodyBg = getComputedStyle(document.body).backgroundColor;
                                const h1Fg = getComputedStyle(document.querySelector('.article-title')).color;
                                const pFg = getComputedStyle(document.querySelector('.article-body p')).color;
                                const faqCard = document.querySelector('.faq-card');
                                const faqBg = faqCard ? getComputedStyle(faqCard).backgroundColor : bodyBg;
                                const faqQFg = getComputedStyle(document.querySelector('.faq-q')).color;
                                const faqAFg = getComputedStyle(document.querySelector('.faq-a')).color;
                                return {
                                    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                                    bodyBg, h1Fg, pFg, faqBg, faqQFg, faqAFg
                                };
                            }""")
                            self.assertLessEqual(art_colors["overflowX"], 0, f"Overflow on {art_route}")
                            b_rgb = _parse_hex_or_rgba(art_colors["bodyBg"])
                            self.assertGreaterEqual(_contrast_ratio(_parse_hex_or_rgba(art_colors["h1Fg"]), b_rgb), 7.0)
                            self.assertGreaterEqual(_contrast_ratio(_parse_hex_or_rgba(art_colors["pFg"]), b_rgb), 5.5)
                            f_rgb = _parse_hex_or_rgba(art_colors["faqBg"])
                            if len(f_rgb) == 4 and f_rgb[3] < 0.99:
                                alpha = f_rgb[3]
                                f_rgb = tuple(int(round(f_rgb[i] * alpha + b_rgb[i] * (1 - alpha))) for i in range(3)) + (1.0,)
                            self.assertGreaterEqual(_contrast_ratio(_parse_hex_or_rgba(art_colors["faqQFg"]), f_rgb), 7.0)
                            self.assertGreaterEqual(_contrast_ratio(_parse_hex_or_rgba(art_colors["faqAFg"]), f_rgb), 5.5)

                    ctx.close()
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
