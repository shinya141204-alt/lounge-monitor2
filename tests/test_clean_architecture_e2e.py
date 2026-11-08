# -*- coding: utf-8 -*-
"""
相席ナビ (AISEKI NAVI) クリーンアーキテクチャ E2E テストスイート (4階層構造)

本テストスイートは、要求仕様書 (ORIGINAL_REQUEST.md: R1〜R5) および
プロジェクト仕様書 (PROJECT.md: F1.1〜F4.3) に基づき、実装から独立した
不透明ボックス (Opaque-box) テストとして以下の4階層で網羅的に検証する：

- Tier 1: Feature Coverage (死にコード・画像削除、キャッシュバスティング同期、ブランド表記、GCPゼロ依存、CSS制約)
- Tier 2: Boundary & Corner Cases (SNS投稿・推論・ローカルDBにおけるNone/空文字/異常値/GViz例外防御)
- Tier 3: Cross-Feature Combinations (スクレイピング障害時のDBスナップショット補完による店舗非消失、SWオフライン整合性)
- Tier 4: Real-World Scenarios (本番相当の全公開エンドポイント疎通検証、ヘルスチェック、404ハンドラ契約)
"""

import os
import re
import sys
import glob
import json
import unittest
import datetime
from unittest.mock import patch, MagicMock

# プロジェクトルートのパス解決
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class Tier1FeatureCoverageTests(unittest.TestCase):
    """
    Tier 1 - Feature Coverage
    F1.1〜F1.4, F3.1, R3（運用制約・ブランド表記・GCP依存ゼロ・CSS制約）の検証
    """

    def test_01_legacy_scripts_completely_removed(self):
        """F1.1: scripts/legacy 配下の非推奨・未参照スクリプトが完全に削除されていることを検証"""
        legacy_dir = os.path.join(REPO_ROOT, "scripts", "legacy")
        if os.path.exists(legacy_dir):
            files = [f for f in os.listdir(legacy_dir) if not f.startswith(".")]
            self.assertEqual(
                len(files), 0,
                f"scripts/legacy/ must be empty or removed, but contains {len(files)} files: {files}"
            )

    def test_02_dead_images_completely_removed(self):
        """F1.3: static/ 配下の未参照デッド画像（thumb-note-eyecatch*.jpg/webp 4ファイル）が削除されていることを検証"""
        dead_images = [
            "thumb-note-eyecatch-weekday.jpg",
            "thumb-note-eyecatch-weekday.webp",
            "thumb-note-eyecatch.jpg",
            "thumb-note-eyecatch.webp",
        ]
        existing_dead_assets = []
        for img in dead_images:
            img_path = os.path.join(REPO_ROOT, "static", img)
            if os.path.exists(img_path):
                existing_dead_assets.append(img)
        self.assertEqual(
            existing_dead_assets, [],
            f"Dead assets still exist in static/: {existing_dead_assets}"
        )

    def test_03_benchmark_result_json_ignored(self):
        """F1.4: ベンチマーク実行動的JSONファイルがGit追跡対象から除外（.gitignore登録）されていることを検証"""
        gitignore_path = os.path.join(REPO_ROOT, ".gitignore")
        self.assertTrue(os.path.exists(gitignore_path), ".gitignore file must exist")
        with open(gitignore_path, "r", encoding="utf-8") as f:
            gitignore_content = f.read()

        has_benchmark_ignore = (
            "benchmark_*_result.json" in gitignore_content
            or "benchmark_*.json" in gitignore_content
            or "benchmark" in gitignore_content
        )
        self.assertTrue(
            has_benchmark_ignore,
            "Benchmark result JSON pattern must be defined in .gitignore"
        )

    def test_04_cache_busting_sync_across_templates_and_sw(self):
        """F3.1: style.css のキャッシュバスティングバージョン（?v=20261109_1）が全テンプレートおよび static/sw.js 間で同期されていることを検証"""
        expected_version = "20261109_1"
        tmpl_dir = os.path.join(REPO_ROOT, "templates")
        self.assertTrue(os.path.isdir(tmpl_dir), "templates directory must exist")

        mismatches = []
        for fname in sorted(os.listdir(tmpl_dir)):
            if fname.endswith(".html"):
                fpath = os.path.join(tmpl_dir, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                matches = re.findall(r"/static/style\.css(?:\?v=([^\s\"\'<>]+))?", content)
                for m in matches:
                    if m != expected_version:
                        mismatches.append(f"templates/{fname}: found v={m!r}, expected v={expected_version!r}")

        # sw.js の CORE_ASSETS
        sw_path = os.path.join(REPO_ROOT, "static", "sw.js")
        self.assertTrue(os.path.exists(sw_path), "static/sw.js must exist")
        with open(sw_path, "r", encoding="utf-8") as f:
            sw_text = f.read()
        sw_match = re.search(r"['\"]/static/style\.css(?:\?v=([^'\"\s]+))?['\"]", sw_text)
        if sw_match:
            sw_v = sw_match.group(1)
            if sw_v != expected_version:
                mismatches.append(f"static/sw.js: found v={sw_v!r}, expected v={expected_version!r}")
        else:
            mismatches.append("static/sw.js: /static/style.css not found in CORE_ASSETS")

        self.assertEqual(
            mismatches, [],
            f"style.css cache-busting version mismatch: {mismatches}"
        )

    def test_05_brand_compliance_public_surface(self):
        """R3: 公開UI全域（公開HTMLテンプレートの表示可視テキスト）において単体『OLG』『オリラジ』『オリエンタル』が露出していないことを検証"""
        tmpl_dir = os.path.join(REPO_ROOT, "templates")
        violations = []

        # 許可される正式名称: "オリエンタルラウンジ"
        # 違反対象: 単体 "OLG" (単語境界), "オリラジ", "オリエンタル(?!ラウンジ)"
        olg_pattern = re.compile(r"(?<![A-Za-z0-9_])OLG(?![A-Za-z0-9_])")
        oriental_pattern = re.compile(r"オリエンタル(?!ラウンジ)")
        oriraji_pattern = re.compile(r"オリラジ")

        for fname in sorted(os.listdir(tmpl_dir)):
            if not fname.endswith(".html"):
                continue
            # スタッフ管理画面（内部管理用ツール）は一般公開UIではないため除外
            if fname.startswith("staff_"):
                continue

            fpath = os.path.join(tmpl_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()

            # HTMLコメント、スクリプト、スタイルタグの除外
            clean_text = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
            clean_text = re.sub(r"<script.*?>.*?</script>", "", clean_text, flags=re.DOTALL)
            clean_text = re.sub(r"<style.*?>.*?</style>", "", clean_text, flags=re.DOTALL)
            # タグ自体（属性 data-filter="OLG" などを可視テキストから除外）
            clean_text = re.sub(r"<[^>]+>", " ", clean_text)

            if oriental_pattern.search(clean_text):
                violations.append(f"{fname}: detected isolated 'オリエンタル'")
            if oriraji_pattern.search(clean_text):
                violations.append(f"{fname}: detected 'オリラジ'")
            if olg_pattern.search(clean_text):
                violations.append(f"{fname}: detected isolated 'OLG'")

        self.assertEqual(
            violations, [],
            f"Brand compliance violations found in public templates: {violations}"
        )

    def test_06_no_paid_gcp_sdk_imports(self):
        """R3: 全Pythonコードおよび requirements.txt において Google Cloud 有料SDK依存がゼロであることを検証"""
        req_path = os.path.join(REPO_ROOT, "requirements.txt")
        self.assertTrue(os.path.exists(req_path), "requirements.txt must exist")
        with open(req_path, "r", encoding="utf-8") as f:
            req_content = f.read().lower()
        self.assertNotIn("google-cloud", req_content, "requirements.txt must not contain google-cloud packages")

        disallowed_imports = []
        for root, dirs, files in os.walk(REPO_ROOT):
            if any(ign in root for ign in [".git", ".agents", "venv", "__pycache__", "tests"]):
                continue
            for file in files:
                if file.endswith(".py"):
                    p = os.path.join(root, file)
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        code = f.read()
                    if "import google.cloud" in code or "from google.cloud" in code:
                        disallowed_imports.append(os.path.relpath(p, REPO_ROOT))

        self.assertEqual(
            disallowed_imports, [],
            f"Disallowed google.cloud imports found in: {disallowed_imports}"
        )

    def test_07_calm_css_pc_vertical_scroll_rule(self):
        """R3: static/calm.css の html セレクタに overflow / overflow-x / overflow-y が指定されていないことを検証（PC縦スクロール維持）"""
        css_path = os.path.join(REPO_ROOT, "static", "calm.css")
        self.assertTrue(os.path.exists(css_path), "static/calm.css must exist")
        with open(css_path, "r", encoding="utf-8") as f:
            css_text = f.read()

        html_block_match = re.search(r"html\s*\{([^}]+)\}", css_text)
        self.assertIsNotNone(html_block_match, "html selector block must exist in calm.css")
        html_body = html_block_match.group(1)
        self.assertNotIn("overflow", html_body, "html selector must not declare overflow property")


class Tier2BoundaryAndCornerCasesTests(unittest.TestCase):
    """
    Tier 2 - Boundary & Corner Cases
    F2.1〜F2.4（x_poster / sns_workflows / moat_intelligence / local_db）の堅牢性検証
    """

    def test_01_x_poster_none_and_empty_headcount_robustness(self):
        """F2.1: x_poster において店舗人数が None、未設定、空文字の場合でも TypeError/ValueError を防ぎ安全に投稿文面を生成できることを検証"""
        import x_poster

        corner_case_stores = [
            {"name": "OLG 渋谷本店", "women": None, "men": 5, "region": "Kanto"},
            {"name": "ag 札幌", "women": 10, "men": None, "region": "Hokkaido"},
            {"name": "JIS 新宿", "women": "", "men": "", "region": "Kanto"},
            {"name": "ASOBIBAR 心斎橋", "region": "Kinki"},
            {"name": "XIX 岡山", "women": 0, "men": 0, "region": "Chugoku"},
        ]

        test_slots = ["20:00", "22:00", "00:00", "02:00", "19:00"]
        for slot in test_slots:
            try:
                tweet_text = x_poster.build_scheduled_tweet_text(corner_case_stores, slot_label=slot)
                self.assertIsInstance(tweet_text, str)
                self.assertTrue(len(tweet_text) > 0)
                self.assertIn("女性", tweet_text)
            except (TypeError, ValueError) as err:
                self.fail(
                    f"x_poster.build_scheduled_tweet_text raised {type(err).__name__} for slot {slot}: {err}"
                )

    def test_02_sns_workflows_none_and_empty_robustness(self):
        """F2.2: sns_workflows において店舗人数が None、未設定、空文字の場合の比較・加算エラーを防御できることを検証"""
        import sns_workflows

        corner_case_stores = [
            {"name": "OLG 渋谷本店", "women": None, "men": 5, "region": "Kanto"},
            {"name": "OLG 新宿", "women": 12, "men": None, "region": "Kanto"},
            {"name": "ag 札幌", "women": "", "men": 2, "region": "Hokkaido"},
            {"name": "JIS 梅田", "women": None, "men": None, "region": "Kinki"},
            {"name": "ASOBIBAR 心斎橋", "region": "Kinki"},
        ]

        latest_data_mock = {
            "full_data": corner_case_stores,
            "last_updated": "2026-11-01 20:00:00",
        }

        for pt in ["kanto", "kansai", "anaba"]:
            try:
                res = sns_workflows.build_sns_post_payload(
                    pt, "https://aiseki-navi.com", latest_data_mock
                )
                self.assertIsInstance(res, dict)
                self.assertIn(res.get("status"), ["success", "loading"])
                text = res.get("text", "")
                self.assertTrue(len(text) > 0)
            except (TypeError, ValueError) as err:
                self.fail(
                    f"sns_workflows.build_sns_post_payload raised {type(err).__name__} for post_type {pt}: {err}"
                )

    def test_03_moat_intelligence_diff_empty_and_abnormal_handling(self):
        """F2.3: moat_intelligence において前日差分値が空文字 '' や異常値の場合の例外（ValueError）を防止できることを検証"""
        import moat_intelligence

        store_name = "OLG 渋谷本店"
        women, men = 20, 15
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)

        abnormal_diffs = [
            ("", ""),
            ("   ", "   "),
            (None, None),
            ("None", "None"),
            ("invalid", "bad"),
            (-9999, 9999),
        ]

        for wd, md in abnormal_diffs:
            try:
                pred = moat_intelligence.predict_store_60m_dynamics(
                    store_name, women, men, women_diff=wd, men_diff=md, now_jst=now_jst
                )
                self.assertIsInstance(pred, dict)
                self.assertIn("pred_60m_women", pred)
                self.assertIn("pred_60m_men", pred)
            except (ValueError, TypeError) as err:
                self.fail(
                    f"moat_intelligence.predict_store_60m_dynamics raised {type(err).__name__} for diffs ({wd!r}, {md!r}): {err}"
                )

    def test_04_local_db_gviz_network_failure_fallback(self):
        """F2.4: local_db の GViz 通信失敗（タイムアウト・DNSエラー）時の安全なフォールバックを検証"""
        import local_db
        import requests

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("Simulated DNS Failure")):
            with patch("requests.Session.get", side_effect=requests.exceptions.Timeout("Simulated Timeout")):
                try:
                    count = local_db._fetch_gviz_rows_and_insert("SELECT A, B WHERE A >= '2026-10-31'", timeout=1)
                    self.assertEqual(
                        count, 0,
                        "_fetch_gviz_rows_and_insert must return 0 on network exception without raising"
                    )
                except Exception as err:
                    self.fail(f"_fetch_gviz_rows_and_insert failed to catch network exception: {err}")

    def test_05_local_db_inflight_key_consistency(self):
        """F2.4: local_db の非同期バックフィルでインフライトキーが _active_db_key() を使用していることを検証"""
        src_path = os.path.join(REPO_ROOT, "local_db.py")
        with open(src_path, "r", encoding="utf-8") as f:
            code = f.read()

        func_match = re.search(r"def _trigger_store_backfill_async\(.*?\):(.*?)(?=\ndef |\Z)", code, re.DOTALL)
        self.assertIsNotNone(func_match, "def _trigger_store_backfill_async must exist in local_db.py")
        func_body = func_match.group(1)
        self.assertTrue(
            "_active_db_key()" in func_body,
            "_trigger_store_backfill_async must use _active_db_key() for inflight key consistency"
        )


class Tier3CrossFeatureCombinationsTests(unittest.TestCase):
    """
    Tier 3 - Cross-Feature Combinations
    障害シナリオ複合検証（スクレイピング全滅時の店舗非消失、SWオフライン整合性）
    """

    def test_01_scraper_failure_db_snapshot_compensation_store_retention(self):
        """スクレイピング全滅（外部全サイト接続障害）時でも、直近DBスナップショット補完により全店舗が消失しないことを検証"""
        import app

        master_path = os.path.join(REPO_ROOT, "static", "store_master.json")
        self.assertTrue(os.path.exists(master_path), "store_master.json must exist")
        with open(master_path, "r", encoding="utf-8") as f:
            store_master = json.load(f)

        import store_master
        valid_master = store_master.load()
        expected_active_stores = len(valid_master) if valid_master else 53
        self.assertGreaterEqual(expected_active_stores, 50, "store_master must define >= 50 stores")

        # 最大系統であるオリエンタルラウンジ/agが一時障害で取得できず、他社データのみ取れた場合をシミュレート
        partial_scraper_data = [
            {"name": "ASOBIBAR 心斎橋", "men": 5, "women": 8, "source": "asobibar"},
            {"name": "JIS SHINJUKU", "men": 10, "women": 12, "source": "jis"},
        ]
        with patch("monitor.get_all_data", return_value=partial_scraper_data), \
             patch("logger.log_data", return_value=None):

            app.update_job(force=True)

            full_data = app.latest_data.get("full_data", [])
            self.assertGreaterEqual(
                len(full_data),
                expected_active_stores,
                f"Even when Oriental Lounge scrapers fail, full_data must retain all canonical stores via DB snapshot compensation (got {len(full_data)}, expected >= {expected_active_stores})"
            )
            # オリエンタルラウンジの各店舗が消失せず残存していること
            retained_names = {s.get("name") for s in full_data if s.get("name")}
            for canonical_name in (valid_master.keys() if valid_master else stores_dict.keys()):
                self.assertIn(
                    canonical_name, retained_names,
                    f"Canonical store '{canonical_name}' must not vanish from latest_data during scraping failure"
                )

    def test_02_serviceworker_offline_cache_asset_existence(self):
        """ServiceWorker static/sw.js の CORE_ASSETS に定義されたアセットの実体ファイルがプロジェクト内に全て存在することを検証"""
        sw_path = os.path.join(REPO_ROOT, "static", "sw.js")
        with open(sw_path, "r", encoding="utf-8") as f:
            sw_text = f.read()

        core_assets_match = re.search(r"const CORE_ASSETS\s*=\s*\[(.*?)\];", sw_text, re.DOTALL)
        self.assertIsNotNone(core_assets_match, "CORE_ASSETS not found in static/sw.js")

        asset_urls = re.findall(r"['\"](/[^'\"?]+)(?:\?[^'\"]*)?['\"]", core_assets_match.group(1))
        self.assertTrue(len(asset_urls) > 0, "CORE_ASSETS must define at least one asset")

        missing_assets = []
        for url in asset_urls:
            rel_file = url.lstrip("/")
            local_path = os.path.join(REPO_ROOT, rel_file)
            if not os.path.exists(local_path):
                missing_assets.append((url, local_path))

        self.assertEqual(
            missing_assets, [],
            f"Precached assets in CORE_ASSETS do not exist locally: {missing_assets}"
        )


class Tier4RealWorldScenariosTests(unittest.TestCase):
    """
    Tier 4 - Real-World Scenarios
    本番環境相当のエンドポイント疎通性・ヘルスチェック・エラーハンドリング検証
    """

    def setUp(self):
        import app
        self.app = app.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_01_production_endpoint_health_checks(self):
        """主要公開エンドポイント（UI・API・メタデータ・SEO）がすべて正常（HTTP 200）に応答することを検証"""
        endpoints = [
            ("/", 200, "text/html"),
            ("/api/status", 200, "application/json"),
            ("/api/events", 200, "application/json"),
            ("/api/health", 200, "application/json"),
            ("/api/health/deep", 200, "application/json"),
            ("/static/store_master.json", 200, "application/json"),
            ("/robots.txt", 200, "text/plain"),
            ("/sitemap.xml", 200, "application/xml"),
            ("/columns", 200, "text/html"),
            ("/price-comparison", 200, "text/html"),
            ("/oriental-lounge", 200, "text/html"),
            ("/ag", 200, "text/html"),
            ("/jis", 200, "text/html"),
            ("/asobibar", 200, "text/html"),
        ]

        for ep, expected_code, expected_content_type in endpoints:
            resp = self.client.get(ep)
            try:
                self.assertEqual(
                    resp.status_code, expected_code,
                    f"Endpoint {ep} returned status code {resp.status_code}, expected {expected_code}"
                )
                if expected_content_type:
                    ct = resp.headers.get("Content-Type", "")
                    self.assertTrue(
                        expected_content_type in ct or (expected_content_type == "application/xml" and "text/xml" in ct),
                        f"Endpoint {ep} returned Content-Type {ct}, expected {expected_content_type}"
                    )
                if ep == "/api/status":
                    data = resp.get_json()
                    self.assertIsInstance(data, dict)
                    self.assertEqual(data.get("status"), "success")
                    self.assertIn("ranking", data)
                    self.assertIn("timestamp", data)
            finally:
                resp.close()

    def test_02_error_page_contract(self):
        """404エラーハンドラがブラウザ向けHTML要求とAPI向けJSON要求を適切に切り分けて応答することを検証"""
        # 1. ブラウザ向けHTML 404
        resp_html = self.client.get("/non-existent-probe-html-contract", headers={"Accept": "text/html"})
        try:
            self.assertEqual(resp_html.status_code, 404)
            self.assertIn("text/html", resp_html.headers.get("Content-Type", ""))
            self.assertTrue(len(resp_html.data) > 0)
        finally:
            resp_html.close()

        # 2. API向けJSON 404
        resp_json = self.client.get("/api/non-existent-probe-json-contract", headers={"Accept": "application/json"})
        try:
            self.assertEqual(resp_json.status_code, 404)
            self.assertIn("application/json", resp_json.headers.get("Content-Type", ""))
            payload = resp_json.get_json()
            self.assertIsInstance(payload, dict)
            self.assertTrue("error" in payload or "status" in payload)
        finally:
            resp_json.close()


if __name__ == "__main__":
    unittest.main()
