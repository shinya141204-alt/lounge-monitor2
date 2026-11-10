# -*- coding: utf-8 -*-
"""
tests/test_seo_llmo_design_goal_audit.py - デザイン・SEO・LLMO 全面レビュー恒久回帰テストスイート

検証範囲:
1. 全公開HTMLルート (126ページ) の HTTP 200・lang="ja"・title/description 一意性＆適正長・canonical・OGP・twitter:card
2. 全公開ページの /link/ アンカー rel="nofollow sponsored noopener" 統一 ＆ 記事/統計/LLMOへの直 /link/ 混入ゼロ
3. 全公開ページの Schema.org JSON-LD 構文・韓国2店舗 addressCountry="KR"・主要固定ページの構造化データ完備
4. 全公開ページの単一 <h1> ＆ 見出し階層スキップゼロ (H1->H3, H2->H4 等ゼロ) ＆ LCP 画像属性
5. 全公開ページの <meta name="color-scheme" content="dark light"> ＆ LLMO <link rel="alternate"> 3本完備
6. /robots.txt の全クローラー内部パス Disallow 完全対称性 ＆ /llms.txt・/llms-full.txt・/api/llms-facts.json 網羅性
7. デザインCSSルール (color-scheme, text-wrap: balance 非グローバル, calm.css html overflow 禁止, 旧51店舗表記ゼロ)
"""

import json
import os
import re
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from html.parser import HTMLParser

import app as app_module
import portal_guides
import scheduled_articles
import store_master

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class _RouteHTMLInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.html_lang = None
        self.in_title = False
        self.title_parts = []
        self.metas = []
        self.links = []
        self.anchors = []
        self._cur_a = None
        self._cur_a_text = []
        self.headings = []
        self._cur_h = None
        self._cur_h_text = []
        self.images = []
        self.json_ld_blocks = []
        self._in_json_ld = False
        self._cur_json_ld = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        t = tag.lower()
        if t == "html":
            self.html_lang = ad.get("lang")
        elif t == "title":
            self.in_title = True
            self.title_parts = []
        elif t == "meta":
            self.metas.append(ad)
        elif t == "link":
            self.links.append(ad)
        elif t == "a":
            self._cur_a = ad
            self._cur_a_text = []
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._cur_h = t
            self._cur_h_text = []
        elif t == "img":
            self.images.append(ad)
            if self._cur_a is not None and ad.get("alt"):
                self._cur_a_text.append(ad["alt"])
        elif t == "svg":
            if self._cur_a is not None and ad.get("aria-label"):
                self._cur_a_text.append(ad["aria-label"])
        elif t == "script":
            if (ad.get("type") or "").strip().lower() == "application/ld+json":
                self._in_json_ld = True
                self._cur_json_ld = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self.in_title = False
        elif t == "a" and self._cur_a is not None:
            txt = re.sub(r"\s+", " ", "".join(self._cur_a_text)).strip()
            aria = (self._cur_a.get("aria-label") or "").strip()
            self.anchors.append(
                {
                    "href": self._cur_a.get("href") or "",
                    "rel": self._cur_a.get("rel") or "",
                    "text": txt or aria,
                }
            )
            self._cur_a = None
            self._cur_a_text = []
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6") and self._cur_h == t:
            txt = re.sub(r"\s+", " ", "".join(self._cur_h_text)).strip()
            self.headings.append((int(t[1]), txt))
            self._cur_h = None
            self._cur_h_text = []
        elif t == "script" and self._in_json_ld:
            self.json_ld_blocks.append("".join(self._cur_json_ld).strip())
            self._in_json_ld = False
            self._cur_json_ld = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self._cur_a is not None:
            self._cur_a_text.append(data)
        if self._cur_h is not None:
            self._cur_h_text.append(data)
        if self._in_json_ld:
            self._cur_json_ld.append(data)


def _discover_public_routes(client):
    smap_resp = client.get("/sitemap.xml")
    assert smap_resp.status_code == 200
    root = ET.fromstring(smap_resp.data.decode("utf-8"))
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_paths = [
        urllib.parse.urlparse(u.findtext("sm:loc", default="", namespaces=ns).strip()).path or "/"
        for u in root.findall("sm:url", ns)
    ]

    upcoming_paths = {it["path"] for it in scheduled_articles.get_upcoming_articles() if it.get("path")}
    expected_public = set(sitemap_paths) | {"/grooming"}
    for rule in app_module.app.url_map.iter_rules():
        r = rule.rule
        if "GET" not in rule.methods or "<" in r:
            continue
        if any(r.startswith(p) for p in ("/api/", "/staff", "/static", "/ogp/", "/link/")):
            continue
        if r in (
            "/robots.txt",
            "/sitemap.xml",
            "/llms.txt",
            "/llms-full.txt",
            "/ads.txt",
            "/sw.js",
            "/stats/monthly.csv",
            "/ladies",
            "/guide/compare",
            "/guide/strategy",
        ) or r in upcoming_paths:
            continue
        expected_public.add(r)

    for slug in portal_guides.AREA_GUIDES.keys():
        expected_public.add(f"/area/{slug}")
    for sname in store_master.load().keys():
        sl = store_master.slug_of(sname)
        if sl:
            expected_public.add(f"/store/{sl}")
    for it in scheduled_articles.get_published_articles():
        if it.get("path"):
            expected_public.add(it["path"])

    return sitemap_paths, sorted(expected_public)


class SeoLlmoDesignGoalAuditTests(unittest.TestCase):
    """デザイン・SEO・LLMO 全面改修の恒久品質ゲート"""

    @classmethod
    def setUpClass(cls):
        app_module.app.config["TESTING"] = True
        app_module.clear_fast_http_caches()
        cls.client = app_module.app.test_client()
        cls.sitemap_paths, cls.public_paths = _discover_public_routes(cls.client)
        cls.parsed_pages = {}
        cls.raw_html_map = {}
        for path in cls.public_paths:
            resp = cls.client.get(path)
            assert resp.status_code == 200, f"{path} returned HTTP {resp.status_code}"
            html = resp.data.decode("utf-8")
            ins = _RouteHTMLInspector()
            ins.feed(html)
            cls.parsed_pages[path] = ins
            cls.raw_html_map[path] = html

    def test_all_public_routes_metadata_ogp_canonical_and_sitemap_parity(self):
        """全公開ルートの title/description/canonical/OGP/twitter:card と sitemap.xml 整合性を検証"""
        self.assertGreaterEqual(len(self.public_paths), 126)
        self.assertNotIn("/grooming", self.sitemap_paths)
        self.assertEqual(
            sorted(set(self.public_paths) - {"/grooming"}),
            sorted(self.sitemap_paths),
            "sitemap.xml と公開ルート集合 (/grooming 除外) が完全一致すること",
        )
        self.assertEqual(len(self.sitemap_paths), len(set(self.sitemap_paths)), "sitemap.xml に重複URLがないこと")

        titles = defaultdict(list)
        descs = defaultdict(list)
        for path, ins in self.parsed_pages.items():
            self.assertEqual(ins.html_lang, "ja", f"{path}: html lang must be 'ja'")
            title = re.sub(r"\s+", " ", "".join(ins.title_parts)).strip()
            self.assertTrue(18 <= len(title) <= 72, f"{path}: title length {len(title)} out of [18,72]: {title}")
            titles[title].append(path)

            meta_name = {(m.get("name") or "").lower(): m.get("content") or "" for m in ins.metas if m.get("name")}
            meta_prop = {(m.get("property") or "").lower(): m.get("content") or "" for m in ins.metas if m.get("property")}

            desc = meta_name.get("description", "").strip()
            self.assertTrue(50 <= len(desc) <= 175, f"{path}: description length {len(desc)} out of [50,175]")
            descs[desc].append(path)

            self.assertNotIn("noindex", meta_name.get("robots", "").lower(), f"{path}: unexpected noindex")

            canons = [lk.get("href") for lk in ins.links if "canonical" in (lk.get("rel") or "").lower().split()]
            expected_url = f"https://aiseki-navi.com{path}"
            self.assertEqual(canons, [expected_url], f"{path}: canonical mismatch")

            for og_key in ("og:title", "og:description", "og:url", "og:image", "og:type", "og:site_name"):
                self.assertTrue(meta_prop.get(og_key), f"{path}: missing {og_key}")
            self.assertEqual(meta_prop.get("og:url"), expected_url, f"{path}: og:url mismatch")
            self.assertTrue(meta_name.get("twitter:card"), f"{path}: missing twitter:card")

        dup_titles = {k: v for k, v in titles.items() if len(v) > 1}
        dup_descs = {k: v for k, v in descs.items() if len(v) > 1}
        self.assertEqual(dup_titles, {}, f"重複 <title> が存在しないこと: {dup_titles}")
        self.assertEqual(dup_descs, {}, f"重複 meta description が存在しないこと: {dup_descs}")

    def test_affiliate_rel_attributes_and_two_step_cushion_purity(self):
        """全公開ページの /link/ アンカーが rel='nofollow sponsored noopener' を持ち、コラム/統計に直 /link/ がないこと"""
        for path, ins in self.parsed_pages.items():
            is_pure_editorial = path == "/guide" or path.startswith("/guide/") or path.startswith("/stats")
            for a in ins.anchors:
                href = a["href"]
                if href.startswith("/link/") or "aiseki-navi.com/link/" in href:
                    self.assertFalse(is_pure_editorial, f"{path}: 記事/統計ページに直 /link/ が混入: {href}")
                    rel_tokens = set(a["rel"].lower().split())
                    self.assertTrue(
                        {"nofollow", "sponsored", "noopener"}.issubset(rel_tokens),
                        f"{path}: /link/ anchor {href} missing nofollow/sponsored/noopener: rel={a['rel']!r}",
                    )

    def test_heading_hierarchy_single_h1_and_lcp_image_attributes(self):
        """全公開ページで <h1> が1個かつ見出しレベルのスキップがなく、主要LCP画像属性が適切であること"""
        for path, ins in self.parsed_pages.items():
            h1s = [h for h in ins.headings if h[0] == 1]
            self.assertEqual(len(h1s), 1, f"{path}: expected exactly 1 <h1>, got {len(h1s)}")
            prev_lvl = 1
            for lvl, txt in ins.headings[1:]:
                self.assertLessEqual(
                    lvl,
                    prev_lvl + 1,
                    f"{path}: skipped heading level H{prev_lvl} -> H{lvl} ({txt[:40]})",
                )
                prev_lvl = lvl

        # /columns first card image must be high priority without lazy loading
        col_imgs = [im for im in self.parsed_pages["/columns"].images if (im.get("src") or "").startswith("/static/")]
        self.assertTrue(col_imgs)
        self.assertEqual(col_imgs[0].get("fetchpriority"), "high")
        self.assertNotEqual(col_imgs[0].get("loading"), "lazy")

    def test_schema_json_ld_validity_and_seoul_country_accuracy(self):
        """全公開ページの JSON-LD が構文有効で、韓国2店舗が addressCountry='KR' になっていること"""
        required_types_by_path = {
            "/about": {"AboutPage", "BreadcrumbList"},
            "/contact": {"ContactPage", "BreadcrumbList"},
            "/privacy": {"WebPage", "BreadcrumbList"},
            "/columns": {"CollectionPage", "BreadcrumbList"},
            "/grooming": {"Article", "BreadcrumbList"},
            "/for-women": {"BreadcrumbList"},
            "/stats": {"Dataset", "BreadcrumbList"},
        }

        for path, ins in self.parsed_pages.items():
            self.assertTrue(ins.json_ld_blocks, f"{path}: missing JSON-LD")
            found_types = set()
            parsed_items = []
            for raw in ins.json_ld_blocks:
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        parsed_items.append(item)
                        t = item.get("@type")
                        if isinstance(t, list):
                            found_types.update(t)
                        elif isinstance(t, str):
                            found_types.add(t)

            if path in required_types_by_path:
                self.assertTrue(
                    required_types_by_path[path].issubset(found_types),
                    f"{path}: missing required schema types {required_types_by_path[path] - found_types}",
                )
            if path == "/guide" or path.startswith("/guide/"):
                self.assertIn("BreadcrumbList", found_types, f"{path}: guide missing BreadcrumbList")
                self.assertTrue(
                    bool({"Article", "TechArticle", "NewsArticle"} & found_types),
                    f"{path}: guide missing Article schema",
                )

            if path in ("/store/oriental-lounge-seoul-gangnam", "/store/oriental-lounge-seoul-hongdae"):
                local_biz = [it for it in parsed_items if "LocalBusiness" in (it.get("@type") or []) or it.get("@type") == "LocalBusiness"]
                self.assertTrue(local_biz)
                self.assertEqual(local_biz[0]["address"]["addressCountry"], "KR")
            elif path.startswith("/store/"):
                local_biz = [it for it in parsed_items if "LocalBusiness" in (it.get("@type") or []) or it.get("@type") == "LocalBusiness"]
                self.assertTrue(local_biz)
                self.assertEqual(local_biz[0]["address"]["addressCountry"], "JP")

    def test_llmo_head_links_robots_symmetry_and_facts_completeness(self):
        """全公開HTMLの LLMO alternate/color-scheme、robots.txt 対称性、llms.txt / llms-facts.json 完全性を検証"""
        expected_alternates = {"/llms.txt", "/llms-full.txt", "/api/llms-facts.json"}
        for path, ins in self.parsed_pages.items():
            meta_name = {(m.get("name") or "").lower(): m.get("content") or "" for m in ins.metas if m.get("name")}
            self.assertEqual(
                meta_name.get("color-scheme"),
                "dark light",
                f"{path}: missing <meta name='color-scheme' content='dark light'>",
            )
            alts = {
                urllib.parse.urlparse(lk.get("href") or "").path
                for lk in ins.links
                if "alternate" in (lk.get("rel") or "").lower().split()
            }
            self.assertTrue(
                expected_alternates.issubset(alts),
                f"{path}: missing LLMO alternate links {expected_alternates - alts}",
            )
            # Stale store count check
            html = self.raw_html_map[path]
            for stale in ("全国51店舗", "全51店舗", "国内51店舗"):
                self.assertNotIn(stale, html, f"{path}: stale store count phrase '{stale}' found")

        # robots.txt symmetry check
        robots_txt = self.client.get("/robots.txt").data.decode("utf-8")
        self.assertIn("Sitemap: https://aiseki-navi.com/sitemap.xml", robots_txt)
        blocks = [b.strip() for b in robots_txt.split("\n\n") if "User-agent:" in b]
        self.assertGreaterEqual(len(blocks), 10)
        for blk in blocks:
            for req_dis in ("Disallow: /staff", "Disallow: /link/", "Disallow: /api/vote", "Disallow: /api/watch"):
                self.assertIn(req_dis, blk, f"robots.txt block missing {req_dis}:\n{blk}")

        # LLMO endpoints completeness
        llms_txt = self.client.get("/llms.txt").data.decode("utf-8")
        llms_full = self.client.get("/llms-full.txt").data.decode("utf-8")
        facts_raw = self.client.get("/api/llms-facts.json").data.decode("utf-8")
        for doc_name, body in (("llms.txt", llms_txt), ("llms-full.txt", llms_full), ("llms-facts.json", facts_raw)):
            self.assertNotIn("/link/", body, f"{doc_name} must not contain affiliate /link/ URLs")

        facts = json.loads(facts_raw)
        for req_key in (
            "license",
            "stable_30d_summary",
            "second_venue_guide_summary",
            "forecast_accuracy_90d",
            "columns",
            "stores",
        ):
            self.assertIn(req_key, facts)

        guide_paths = [p for p in self.public_paths if p == "/guide" or p.startswith("/guide/")]
        fact_col_paths = {urllib.parse.urlparse(c["url"]).path for c in facts["columns"]}
        for gp in guide_paths:
            self.assertIn(gp, fact_col_paths, f"{gp} missing from llms-facts.json columns")
            self.assertIn(f"https://aiseki-navi.com{gp}", llms_txt, f"{gp} missing from /llms.txt")

        self.assertEqual(len(facts["stores"]), 61)
        sample_store = facts["stores"][0]
        for sk in ("dossier_catch", "mens_tactic", "womens_tactic", "second_venue_spots"):
            self.assertIn(sk, sample_store)

    def test_modern_web_css_invariants(self):
        """calm.css / portal.css の color-scheme・text-wrap: balance 非グローバル・html overflow 非指定を検証"""
        with open(os.path.join(PROJECT_ROOT, "static", "calm.css"), encoding="utf-8") as f:
            calm_css = f.read()
        with open(os.path.join(PROJECT_ROOT, "static", "portal.css"), encoding="utf-8") as f:
            portal_css = f.read()

        self.assertIn("color-scheme: dark;", calm_css)
        self.assertIn("color-scheme: light;", calm_css)
        self.assertIn("text-wrap: balance;", calm_css)
        self.assertIn("color-scheme: dark;", portal_css)
        self.assertIn("color-scheme: light;", portal_css)

        # Global * { text-wrap: balance } is prohibited per modern-web-guidance
        for css_name, css_body in (("calm.css", calm_css), ("portal.css", portal_css)):
            star_blocks = re.findall(r"(?:^|\n)\*\s*\{([^}]*)\}", css_body)
            for sb in star_blocks:
                self.assertNotIn("text-wrap: balance", sb, f"{css_name}: global * text-wrap: balance is prohibited")

        # calm.css bare html selector must never set overflow (breaks sticky panels)
        html_blocks = re.findall(r"(?:^|\n)html\s*\{([^}]*)\}", calm_css)
        for hb in html_blocks:
            self.assertNotIn("overflow", hb, "calm.css bare html block must not set overflow")

        # .sr-only / .visually-hidden utility must exist so semantic screen-reader headings never leak visually
        self.assertIn(".sr-only:where(:not(:focus-within, :active))", calm_css)
        self.assertIn("clip-path: inset(50%) !important;", calm_css)

    def test_gsc_ctr_title_clarity_and_price_comparison_canonical_isolation(self):
        """GSC実測改善: 記事<title>先頭28文字の主題明示・店舗<title>の料金/客層網羅・/price-comparison canonical固定とキャッシュ非干渉を検証"""
        for path, ins in self.parsed_pages.items():
            title = re.sub(r"\s+", " ", "".join(ins.title_parts)).strip()
            if path == "/guide" or path.startswith("/guide/"):
                head28 = title[:28]
                self.assertTrue(
                    any(kw in head28 for kw in ("相席", "オリエンタル", "JIS", "ag", "ASOBIBAR")),
                    f"{path}: mobile SERP first 28 chars lack lounge keyword: {title!r}",
                )
            elif path.startswith("/store/"):
                self.assertIn("現在人数", title, f"{path}: missing 現在人数 in title")
                self.assertIn("客層", title, f"{path}: missing 客層 in title")
                if "seoul" in path:
                    self.assertIn("韓国ソウル", title, f"{path}: missing 韓国ソウル in Seoul store title")
                else:
                    self.assertIn("料金", title, f"{path}: missing 料金 in domestic store title")

        # Warm ?area=shinjuku first, then verify /price-comparison root still serves national title & root canonical
        app_module.clear_fast_http_caches()
        r_shinjuku = self.client.get("/price-comparison?area=shinjuku").data.decode("utf-8")
        r_fukuoka = self.client.get("/price-comparison?area=fukuoka").data.decode("utf-8")
        r_root = self.client.get("/price-comparison").data.decode("utf-8")
        for variant_html in (r_shinjuku, r_fukuoka, r_root):
            self.assertIn('<link rel="canonical" href="https://aiseki-navi.com/price-comparison">', variant_html)
        self.assertIn("<title>新宿・西新宿（4店）の相席ラウンジ料金", r_shinjuku)
        self.assertIn("<title>相席ラウンジ料金・男性割引イベント比較｜", r_root)




if __name__ == "__main__":
    unittest.main()
