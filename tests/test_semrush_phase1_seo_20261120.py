"""Regression tests for 2026-11-20 Semrush Phase 1 SEO implementation:
- Track 1: www.aiseki-navi.com -> https://aiseki-navi.com permanent redirect (301 GET/HEAD, 308 POST, open-redirect & percent-encoding safe, trailing-dot safe)
- Track 2: Ultra-low-KD existing page refreshes (/guide/solo-vs-pair, /asobibar, /jis, /guide/dress-code-cleanliness, /guide, /guide/lounge-vs-aisekiya-bar)
- Track 3: New column /guide/oriental-lounge-vip-app full registration (HTML, JSON-LD, sitemap, llms.txt, /columns, analytics, column_editorial, X/Tips weights, pricing math reconciliation)
"""

import json
import os
import re
import unittest
from unittest.mock import patch

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")

import app as app_module
import analytics
import column_editorial
import price_comparison
import store_master
import x_poster
import tips_poster


class SemrushPhase1SeoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()

    # ------------------------------------------------------------------
    # Track 1: www.aiseki-navi.com -> apex canonical permanent redirect
    # ------------------------------------------------------------------
    def test_www_get_redirects_301_preserving_path_and_query(self):
        resp = self.client.get(
            "/store/asobibar-chayamachi?s=x22_search&tab=stats",
            headers={"Host": "www.aiseki-navi.com"},
        )
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(
            resp.headers.get("Location"),
            "https://aiseki-navi.com/store/asobibar-chayamachi?s=x22_search&tab=stats",
        )

    def test_www_trailing_dot_host_also_redirects_301(self):
        resp = self.client.get(
            "/asobibar",
            headers={"Host": "www.aiseki-navi.com."},
        )
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp.headers.get("Location"), "https://aiseki-navi.com/asobibar")

    def test_www_head_redirects_301(self):
        resp = self.client.head("/", headers={"Host": "www.aiseki-navi.com"})
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp.headers.get("Location"), "https://aiseki-navi.com/")

    def test_www_post_redirects_308(self):
        resp = self.client.post(
            "/api/track",
            json={"event": "noop"},
            headers={"Host": "www.aiseki-navi.com"},
        )
        self.assertEqual(resp.status_code, 308)
        self.assertEqual(resp.headers.get("Location"), "https://aiseki-navi.com/api/track")

    def test_www_open_redirect_double_slash_via_raw_path_info_is_sanitized(self):
        resp = self.client.get(
            "/",
            headers={"Host": "www.aiseki-navi.com"},
            environ_overrides={"PATH_INFO": "//evil.example/phish"},
        )
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(
            resp.headers.get("Location"),
            "https://aiseki-navi.com/evil.example/phish",
        )

    def test_www_percent_encoded_special_chars_in_path_stay_encoded(self):
        resp = self.client.get(
            "/",
            headers={"Host": "www.aiseki-navi.com"},
            environ_overrides={"PATH_INFO": "/foo?bar#baz"},
        )
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(
            resp.headers.get("Location"),
            "https://aiseki-navi.com/foo%3Fbar%23baz",
        )

    def test_apex_host_does_not_redirect_loop(self):
        resp = self.client.get(
            "/guide/oriental-lounge-vip-app",
            headers={"Host": "aiseki-navi.com"},
        )
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # Track 2: Ultra-low-KD existing pages
    # ------------------------------------------------------------------
    def test_solo_vs_pair_refreshed_for_solo_keywords_and_faq(self):
        resp = self.client.get("/guide/solo-vs-pair")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for token in (
            "オリエンタルラウンジ",
            "JIS（ジス）",
            "ひとり",
            "1人",
            "相席屋",
            "ASOBIBAR（アソビバー）",
            '"@type": "FAQPage"',
            "よくある質問",
            "最新規定は公式要確認",
        ):
            self.assertIn(token, html)
        self.assertNotIn('href="/link/', html)
        self.assertNotIn("href='/link/", html)

    def test_asobibar_jis_olg_brand_pages_have_price_column_and_no_none(self):
        for path, must_tokens in (
            ("/asobibar", ("アソビバー", "料金", "カラーバンド", "男性60分目安", "国内10店舗", "/guide/lounge-vs-aisekiya-bar")),
            ("/jis", ("JIS（ジス）", "料金", "男性60分目安", "国内10店舗")),
            ("/oriental-lounge", ("オリエンタルラウンジ", "男性60分目安", "国内29店舗", "/guide/oriental-lounge-vip-app")),
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, msg=f"Failed {path}")
            html = resp.get_data(as_text=True)
            for tok in must_tokens:
                self.assertIn(tok, html, msg=f"{tok!r} missing from {path}")
            self.assertNotIn("女None", html, msg=f"Raw None leaked in {path}")
            self.assertNotIn("男None", html, msg=f"Raw None leaked in {path}")
            self.assertNotIn("/guide/aiseki-lounge-vs-bar-izakaya", html)

    def test_brand_page_handles_missing_wpct_and_low_sample_waveform_safely(self):
        # Simulate waveform dict missing 'wpct' key and thin sample count (<5)
        fake_wf = {
            "ASOBIBAR 新宿": {
                "h22": {"w": 10.25, "m": 12.75, "total": 23.0, "n": 10},  # intentionally no 'wpct'
                "h00": {"w": 0.0, "m": 0.0, "total": 0.0, "n": 1},       # thin & zero -> hidden
            }
        }
        with patch.object(app_module.local_db, "get_all_stores_hourly_waveforms", return_value=fake_wf):
            with app_module._page_html_cache_lock:
                app_module._page_html_cache.clear()
            resp = self.client.get("/asobibar")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("女10.2 : 男12.8", html)
        self.assertIn("計測蓄積中", html)

    def test_dress_code_and_guide_decannibalization(self):
        r_dress = self.client.get("/guide/dress-code-cleanliness")
        self.assertEqual(r_dress.status_code, 200)
        h_dress = r_dress.get_data(as_text=True)
        self.assertIn("オリエンタルラウンジ", h_dress)
        self.assertIn("ドレスコード", h_dress)
        self.assertIn("服装", h_dress)
        self.assertNotIn('href="/link/', h_dress)

        r_guide = self.client.get("/guide")
        self.assertEqual(r_guide.status_code, 200)
        h_guide = r_guide.get_data(as_text=True)
        self.assertIn('href="/guide/dress-code-cleanliness"', h_guide)
        self.assertNotIn("相席ラウンジ とは", h_guide)

    def test_lounge_vs_aisekiya_bar_captures_definition_queries_cleanly(self):
        resp = self.client.get("/guide/lounge-vs-aisekiya-bar")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for tok in ("相席ラウンジとは", "相席屋", "相席バー", "/guide/oriental-lounge-vip-app"):
            self.assertIn(tok, html)
        self.assertNotIn("`store_hourly_rollups`", html)
        self.assertNotIn('href="/link/', html)

    # ------------------------------------------------------------------
    # Track 3: New column /guide/oriental-lounge-vip-app full wiring
    # ------------------------------------------------------------------
    def test_new_vip_app_column_html_jsonld_and_compliance(self):
        resp = self.client.get("/guide/oriental-lounge-vip-app")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for tok in (
            "オリエンタルラウンジVIP席の料金は公式非公開",
            "SOLOTTE!",
            "550円",
            "2,640〜7,200円",
            "9,000円",
            "oriental-lounge.com",
            "くわもん（相席ナビ編集長）",
            "相席ナビ編集部",
            "eyecatch-oriental-lounge-vip-app.jpg?v=3",
            "XIX（ジックス）",
            "ALFA（アルファ）",
            "CLOVERS（クローバーズ）",
            "よくある質問",
        ):
            self.assertIn(tok, html)
        self.assertNotIn('href="/link/', html)
        self.assertNotIn("href='/link/", html)
        # Reconcile OLG domestic min/max 10-min rates with price_comparison master
        stores = store_master.load()
        olg_wk_min, olg_we_max = 99999, 0
        for sname, meta in stores.items():
            if meta.get("brand") == "OLG" and meta.get("region") != "Korea":
                prof = price_comparison.get_store_price_profile(sname)
                nums_wk = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+(?=円)", prof.get("weekday_10m_str") or "")]
                nums_we = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+(?=円)", prof.get("weekend_10m_str") or "")]
                if nums_wk:
                    olg_wk_min = min(olg_wk_min, min(nums_wk))
                if nums_we:
                    olg_we_max = max(olg_we_max, max(nums_we))
        self.assertEqual(olg_wk_min, 440)
        self.assertEqual(olg_we_max, 1500)

        # Zero banned bare brand abbreviations or AI hype words in article content/title
        main_match = re.search(r"<main>(.*?)</main>", html, re.S)
        self.assertIsNotNone(main_match)
        title_match = re.search(r"<title>(.*?)</title>", html, re.S)
        checked_text = (title_match.group(1) if title_match else "") + "\n" + main_match.group(1)
        for banned in ("オリラジ", "徹底解説", "完全攻略", "完全ガイド", "間違いなし", "必見", "黄金帯", "突撃"):
            self.assertNotIn(banned, checked_text, msg=f"Banned token {banned!r} found")
        self.assertIsNone(re.search(r"オリエンタル(?!ラウンジ)", checked_text))
        self.assertNotIn("/guide/aiseki-lounge-vs-bar-izakaya", html)

        # Validate JSON-LD parses cleanly with Article + BreadcrumbList + FAQPage
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        ld = json.loads(m.group(1))
        types = {node.get("@type") for node in ld}
        self.assertEqual(types, {"Article", "BreadcrumbList", "FAQPage"})

    def test_new_vip_app_column_registered_everywhere(self):
        r_smap = self.client.get("/sitemap.xml")
        self.assertEqual(r_smap.status_code, 200)
        self.assertIn("https://aiseki-navi.com/guide/oriental-lounge-vip-app", r_smap.get_data(as_text=True))

        r_llms = self.client.get("/llms.txt")
        self.assertEqual(r_llms.status_code, 200)
        self.assertIn("/guide/oriental-lounge-vip-app", r_llms.get_data(as_text=True))

        r_cols = self.client.get("/columns")
        self.assertEqual(r_cols.status_code, 200)
        self.assertIn('href="/guide/oriental-lounge-vip-app"', r_cols.get_data(as_text=True))

        self.assertIn("/guide/oriental-lounge-vip-app", analytics._PAGE_TITLES)
        self.assertIn("oriental-lounge-vip-app", column_editorial.RESERVED_STATIC_SLUGS)
        self.assertTrue(
            any(c.get("slug") == "oriental-lounge-vip-app" for c in column_editorial.EXISTING_STATIC_COLUMNS)
        )

        promo = next((p for p in x_poster.COLUMN_PROMO_LIST if p.get("slug") == "oriental-lounge-vip-app"), None)
        self.assertIsNotNone(promo)
        w_promo = x_poster.compute_twitter_weight(promo["main_text"])
        self.assertLessEqual(w_promo, 272, msg=f"X promo weight too high: {w_promo}")

        tip_text = tips_poster.TIPS_LIST[76]
        self.assertIn("SOLOTTE!", tip_text)
        w_tip = x_poster.compute_twitter_weight(tip_text)
        self.assertLessEqual(w_tip, 276, msg=f"Tip #76 weight too high: {w_tip}")
        _, mapped_path = tips_poster._column_for_tip_index(76)
        self.assertEqual(mapped_path, "/guide/oriental-lounge-vip-app")


if __name__ == "__main__":
    unittest.main()
