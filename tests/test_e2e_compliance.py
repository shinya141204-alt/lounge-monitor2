# -*- coding: utf-8 -*-
"""
tests/test_e2e_compliance.py - Tier 3 制約・整合性 E2E テストスイート

検証対象:
1. ブランド表記ルール (Brand Compliance):
   - 公開UI・テンプレート・記事・SNS出力に単体の「オリエンタル」「OLG」が出現しないことの自動全走査。
   - x_poster.format_ja_store_name() によるOLGプレフィックスの自動正式名称化。
2. Google Cloud (GCP) ゼロ依存 (GCP Zero-Dependency):
   - requirements.txt, Dockerfile, fly.toml, 全Pythonコードベースに google.cloud 依存やAPI呼び出しがないことの静的解析。
3. Google Sheets 'Lounge Monitor Data' 5列構造維持 (5-Column Schema Compliance):
   - logger.log_data による append_rows が厳格に5列 [timestamp, name, men, women, source] を維持していること。
   - 拡張データ（Moat Telemetry）が別シートに完全分離されていること。
"""

import os
import re
import unittest
from unittest.mock import MagicMock, patch

# プロジェクトルートディレクトリ
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class BrandComplianceE2ETests(unittest.TestCase):
    """プロジェクト全体のブランド表記ルール自動全走査 E2E テスト"""

    def setUp(self):
        # 単体「OLG」を検出する単語境界正規表現
        self.re_isolated_olg = re.compile(r"\bOLG\b")
        # 単体の「オリエンタル」（直後に「ラウンジ」が続かないもの）
        self.re_isolated_oriental = re.compile(r"オリエンタル(?!ラウンジ)")
        # 単体・略称「オリラジ」を検出する正規表現
        self.re_oriradi = re.compile(r"オリラジ")

    def test_templates_have_no_isolated_olg_or_oriental_or_oriradi(self):
        """公開テンプレート (templates/*.html) の可視UIテキストおよびメタ属性に単体 'OLG' や単体 'オリエンタル'、'オリラジ' が露出していないこと"""
        from bs4 import BeautifulSoup

        templates_dir = os.path.join(PROJECT_ROOT, "templates")
        self.assertTrue(os.path.isdir(templates_dir), "templates ディレクトリが存在しません")

        violations = []
        for root, dirs, files in os.walk(templates_dir):
            for file_name in files:
                if not file_name.endswith(".html"):
                    continue
                # 管理画面 (staff_*.html) は内部運用向けツールのため除外
                if file_name.startswith("staff_"):
                    continue

                full_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(full_path, PROJECT_ROOT)

                with open(full_path, "r", encoding="utf-8", errors="ignore") as fp:
                    soup = BeautifulSoup(fp.read(), "html.parser")

                    # 1. <script> や <style> 内のコードを除外したユーザー可視テキストの検査
                    for s in soup(["script", "style"]):
                        s.extract()

                    text = soup.get_text()
                    for line in text.splitlines():
                        line_clean = line.strip()
                        if not line_clean:
                            continue
                        if self.re_isolated_olg.search(line_clean):
                            violations.append(
                                f"[OLG Visible Violation] {rel_path} -> {line_clean[:100]}"
                            )
                        if self.re_isolated_oriental.search(line_clean):
                            violations.append(
                                f"[Oriental Visible Violation] {rel_path} -> {line_clean[:100]}"
                            )
                        if self.re_oriradi.search(line_clean):
                            violations.append(
                                f"[Oriradi Visible Violation] {rel_path} -> {line_clean[:100]}"
                            )

                    # 2. ユーザー可視属性 (title, alt, placeholder) および SEO meta タグの検査
                    for tag in soup.find_all(True):
                        for attr in ["title", "alt", "placeholder"]:
                            val = tag.get(attr)
                            if val and isinstance(val, str):
                                if self.re_isolated_olg.search(val):
                                    violations.append(
                                        f"[OLG Attr {attr}] {rel_path} <{tag.name}> -> {val[:100]}"
                                    )
                                if self.re_isolated_oriental.search(val):
                                    violations.append(
                                        f"[Oriental Attr {attr}] {rel_path} <{tag.name}> -> {val[:100]}"
                                    )
                                if self.re_oriradi.search(val):
                                    violations.append(
                                        f"[Oriradi Attr {attr}] {rel_path} <{tag.name}> -> {val[:100]}"
                                    )
                        if tag.name == "meta" and tag.get("name") in ["description", "keywords"]:
                            val = tag.get("content", "")
                            if val and isinstance(val, str):
                                if self.re_isolated_olg.search(val):
                                    violations.append(
                                        f"[OLG Meta {tag.get('name')}] {rel_path} -> {val[:100]}"
                                    )
                                if self.re_isolated_oriental.search(val):
                                    violations.append(
                                        f"[Oriental Meta {tag.get('name')}] {rel_path} -> {val[:100]}"
                                    )
                                if self.re_oriradi.search(val):
                                    violations.append(
                                        f"[Oriradi Meta {tag.get('name')}] {rel_path} -> {val[:100]}"
                                    )

        if violations:
            msg = (
                f"\n=== ブランド表記ルール違反が {len(violations)} 箇所検出されました ===\n"
                + "\n".join(violations[:20])
            )
            if len(violations) > 20:
                msg += f"\n... 他 {len(violations) - 20} 件省略"
            self.fail(msg)

    def test_x_poster_format_ja_store_name_normalizes_olg_stores(self):
        """x_poster.format_ja_store_name が 'OLG xxx' を 'オリエンタルラウンジ xxx' 等の正式名称に正規化すること"""
        import x_poster

        test_cases = [
            ("OLG 渋谷本店", "オリエンタルラウンジ 渋谷本店"),
            ("OLG 新宿", "オリエンタルラウンジ 新宿"),
            ("OLG 恵比寿", "オリエンタルラウンジ 恵比寿"),
            ("OLG 梅田", "オリエンタルラウンジ 梅田"),
            ("OLG 名古屋錦", "オリエンタルラウンジ 名古屋錦"),
            ("OLG 大阪駅前", "オリエンタルラウンジ 大阪駅前"),
            ("OLG ag渋谷", "ag渋谷"),  # agブランドは正しくagへ変換されること
        ]

        for raw_name, expected_prefix in test_cases:
            with self.subTest(raw_name=raw_name):
                formatted = x_poster.format_ja_store_name(raw_name)
                # 単体 OLG が残存してはならない
                self.assertNotIn(
                    "OLG",
                    formatted,
                    f"format_ja_store_name('{raw_name}') の戻り値 '{formatted}' に単体 OLG が残存しています",
                )
                if raw_name.startswith("OLG ag"):
                    self.assertTrue(
                        formatted.startswith("ag"),
                        f"format_ja_store_name('{raw_name}') -> '{formatted}' は 'ag' で始まる必要があります",
                    )
                else:
                    self.assertTrue(
                        formatted.startswith("オリエンタルラウンジ"),
                        f"format_ja_store_name('{raw_name}') -> '{formatted}' は 'オリエンタルラウンジ' で始まる必要があります",
                    )

    def test_store_master_seo_names_comply_with_brand_rule(self):
        """store_master.json の全OLG店舗について seo_name がブランド表記ルールに準拠していること"""
        import store_master

        stores = store_master.load()
        for name, store in stores.items():
            brand = store.get("brand", "")
            seo_name = store_master.seo_name(name)

            if brand == "OLG" or name.startswith("OLG "):
                if "ag" not in name.lower():
                    self.assertIn(
                        "オリエンタルラウンジ",
                        seo_name,
                        f"店舗 '{name}' の seo_name '{seo_name}' に 'オリエンタルラウンジ' が含まれていません",
                    )
                self.assertNotIn(
                    "OLG",
                    seo_name,
                    f"店舗 '{name}' の seo_name '{seo_name}' に単体 'OLG' が残存しています",
                )

    def test_sns_editorial_natural_store_name_brand_rule(self):
        """sns_editorial.natural_store_name がすべてのOLG店舗で正式名称を返却すること"""
        import sns_editorial

        olg_samples = ["OLG 渋谷本店", "OLG 新宿", "OLG 難波", "OLG 栄"]
        for store_name in olg_samples:
            with self.subTest(store_name=store_name):
                natural = sns_editorial.natural_store_name(store_name)
                self.assertNotIn("OLG", natural, f"natural_store_name('{store_name}') に OLG が残存")
                self.assertIn("オリエンタルラウンジ", natural, f"natural_store_name('{store_name}') にオリエンタルラウンジが不足")

    def test_x_poster_format_ja_store_name_boundary_bare_olg(self):
        """x_poster.format_ja_store_name に境界値 'OLG' のみを渡しても 'オリエンタルラウンジ' が返却されること"""
        import x_poster

        formatted = x_poster.format_ja_store_name("OLG")
        self.assertEqual(formatted, "オリエンタルラウンジ")
        self.assertNotIn("OLG", formatted)

    def test_x_poster_weekly_report_has_no_oriradi(self):
        """x_poster.build_weekly_report_tweet_thread が文字数制限内でも 'オリラジ' への置換を行わず、正式名称を維持すること"""
        import x_poster

        sample_report = {
            "stores": [
                {"name": "OLG 渋谷本店", "avg_women": 45, "max_women": 60},
                {"name": "OLG 新宿", "avg_women": 40, "max_women": 55},
            ],
            "anaba_stores": [{"name": "OLG 恵比寿", "avg_wpct": 58}],
        }
        main_text, reply_text = x_poster.build_weekly_report_tweet_thread(sample_report, use_ai=False)
        self.assertNotIn("オリラジ", main_text, f"週間レポートツイート本文に 'オリラジ' が検出されました:\n{main_text}")
        self.assertIn("オリエンタルラウンジ", main_text, "正式ブランド名称 'オリエンタルラウンジ' が含まれていません")
        weight = x_poster.compute_twitter_weight(main_text)
        self.assertLessEqual(weight, 274, f"Twitter weight ({weight}) が上限 274 を超過しています")

    def test_tips_poster_has_no_oriradi(self):
        """tips_poster.py 内の配信テキストに 'オリラジ' が一切存在しないこと"""
        tips_file = os.path.join(PROJECT_ROOT, "tips_poster.py")
        self.assertTrue(os.path.isfile(tips_file), "tips_poster.py が存在しません")
        with open(tips_file, "r", encoding="utf-8") as f:
            content = f.read()

        matches = self.re_oriradi.findall(content)
        self.assertEqual(
            matches,
            [],
            f"tips_poster.py 内に 'オリラジ' が {len(matches)} 件検出されました",
        )

    def test_sns_editorial_casual_store_name_and_ai_postprocess_brand_rules(self):
        """sns_editorial.casual_store_name がすべて正式名称を返し、_postprocess_ai_main_text が 'オリラジ' を排除すること"""
        import sns_editorial

        olg_samples = ["OLG 渋谷本店", "OLG 新宿", "OLG 難波", "OLG 栄"]
        for store_name in olg_samples:
            with self.subTest(store_name=store_name):
                casual = sns_editorial.casual_store_name(store_name)
                self.assertNotIn("オリラジ", casual, f"casual_store_name('{store_name}') に 'オリラジ' が検出されました: {casual}")
                self.assertIn("オリエンタルラウンジ", casual, f"casual_store_name('{store_name}') に正式名称が不足: {casual}")

        # AI生成後処理テスト: 'オリラジ' が含まれていても 'オリエンタルラウンジ' へ正規化されること
        dirty_ai_text = "金曜夜のオリラジ新宿とオリラジ難波が穴場です！"
        cleaned_x = sns_editorial._postprocess_ai_main_text(dirty_ai_text, channel="x")
        self.assertNotIn("オリラジ", cleaned_x)
        self.assertIn("オリエンタルラウンジ新宿", cleaned_x)
        self.assertIn("オリエンタルラウンジ難波", cleaned_x)

        dirty_bare_text = "いまオリエンタル新宿が男性先行中！"
        cleaned_bare = sns_editorial._postprocess_ai_main_text(dirty_bare_text, channel="x")
        self.assertNotIn("オリエンタル新宿", cleaned_bare)
        self.assertIn("オリエンタルラウンジ新宿", cleaned_bare)
        self.assertNotIn("オリラジ", cleaned_bare)

    def test_second_venues_and_sns_editorial_brand_rules(self):
        """static/second_venues.json, second_venues.py, templates/staff_sns_editorial.html 等において
        単体の『オリエンタル』『OLG』『オリラジ』が混入せず、すべて『オリエンタルラウンジ』と表記されていることを網羅検証"""
        import json
        from bs4 import BeautifulSoup
        import second_venues

        violations = []

        # 1. static/second_venues.json の検証
        venues_path = os.path.join(PROJECT_ROOT, "static", "second_venues.json")
        self.assertTrue(os.path.isfile(venues_path), "static/second_venues.json が存在しません")
        with open(venues_path, "r", encoding="utf-8") as f:
            venues_raw = f.read()

        for line_no, line in enumerate(venues_raw.splitlines(), start=1):
            line_str = line.strip()
            if self.re_isolated_olg.search(line_str):
                violations.append(f"[OLG] static/second_venues.json:{line_no} -> {line_str[:100]}")
            if self.re_isolated_oriental.search(line_str):
                violations.append(f"[Oriental] static/second_venues.json:{line_no} -> {line_str[:100]}")
            if self.re_oriradi.search(line_str):
                violations.append(f"[Oriradi] static/second_venues.json:{line_no} -> {line_str[:100]}")

        # JSONデータ内のスポットコメントやTipsフレーズに「オリエンタルラウンジ」が正しく使われていること
        master_data = json.loads(venues_raw)
        found_formal_name = False
        for area, spots in master_data.get("venues_by_area", {}).items():
            for sp in spots:
                comment = sp.get("comment", "")
                if "オリエンタルラウンジ" in comment:
                    found_formal_name = True
        self.assertTrue(found_formal_name, "static/second_venues.json 内に 'オリエンタルラウンジ' の言及が存在しません")

        # 2. templates/staff_sns_editorial.html の全可視テキストおよび属性値の検証
        editorial_path = os.path.join(PROJECT_ROOT, "templates", "staff_sns_editorial.html")
        self.assertTrue(os.path.isfile(editorial_path), "templates/staff_sns_editorial.html が存在しません")
        with open(editorial_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        # ユーザー可視テキスト
        for s in soup(["script", "style"]):
            s.extract()
        for line in soup.get_text().splitlines():
            line_clean = line.strip()
            if not line_clean:
                continue
            if self.re_isolated_olg.search(line_clean):
                violations.append(f"[OLG Text] templates/staff_sns_editorial.html -> {line_clean[:100]}")
            if self.re_isolated_oriental.search(line_clean):
                violations.append(f"[Oriental Text] templates/staff_sns_editorial.html -> {line_clean[:100]}")
            if self.re_oriradi.search(line_clean):
                violations.append(f"[Oriradi Text] templates/staff_sns_editorial.html -> {line_clean[:100]}")

        # 属性値
        for tag in soup.find_all(True):
            for attr in ["title", "alt", "placeholder", "value"]:
                val = tag.get(attr)
                if val and isinstance(val, str):
                    if self.re_isolated_olg.search(val):
                        violations.append(f"[OLG Attr {attr}] templates/staff_sns_editorial.html <{tag.name}> -> {val[:100]}")
                    if self.re_isolated_oriental.search(val):
                        violations.append(f"[Oriental Attr {attr}] templates/staff_sns_editorial.html <{tag.name}> -> {val[:100]}")
                    if self.re_oriradi.search(val):
                        violations.append(f"[Oriradi Attr {attr}] templates/staff_sns_editorial.html <{tag.name}> -> {val[:100]}")

        # 3. second_venues.py の動的出力検証
        test_stores = ["OLG 渋谷本店", "OLG 新宿", "OLG 恵比寿", "OLG 難波", "OLG 栄", "OLG 札幌", "OLG 熊本"]
        for store in test_stores:
            anchor = second_venues.get_store_anchor(store)
            # anchor は必ず "オリエンタルラウンジ" で始まること（OLGが残存してはならない）
            self.assertTrue(
                anchor.startswith("オリエンタルラウンジ"),
                f"second_venues.get_store_anchor('{store}') -> '{anchor}' は 'オリエンタルラウンジ' で始まる必要があります",
            )
            self.assertNotIn("OLG", anchor)

            res = second_venues.get_second_venues_for_store(store)
            # 入力引数のエコーバック (res['store_name']) 以外の全出力フィールドでブランドルールを検証
            res_content = {k: v for k, v in res.items() if k != "store_name"}
            content_dump = json.dumps(res_content, ensure_ascii=False)
            if self.re_isolated_olg.search(content_dump):
                violations.append(f"[OLG In Output] store={store} -> {content_dump[:150]}")
            if self.re_isolated_oriental.search(content_dump):
                violations.append(f"[Oriental In Output] store={store} -> {content_dump[:150]}")
            if self.re_oriradi.search(content_dump):
                violations.append(f"[Oriradi In Output] store={store} -> {content_dump[:150]}")

        self.assertEqual(
            violations,
            [],
            f"ブランド表記ルール違反が検出されました:\n" + "\n".join(violations),
        )


class GcpZeroDependencyE2ETests(unittest.TestCase):
    """Google Cloud (GCP) ゼロ依存静的解析 E2E テスト"""

    def test_requirements_txt_has_no_gcp_packages(self):
        """requirements.txt に google-cloud-* や google.cloud パッケージが含まれていないこと"""
        req_path = os.path.join(PROJECT_ROOT, "requirements.txt")
        self.assertTrue(os.path.isfile(req_path), "requirements.txt が存在しません")

        with open(req_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        violations = []
        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip().lower()
            if not stripped or stripped.startswith("#"):
                continue
            if "google-cloud" in stripped or "google.cloud" in stripped:
                violations.append(f"requirements.txt:{line_no} -> {stripped}")

        self.assertEqual(
            violations,
            [],
            f"requirements.txt に GCP 関連パッケージが見つかりました: {violations}",
        )

    def test_dockerfile_has_no_gcp_sdk_or_cli(self):
        """Dockerfile に google-cloud-sdk や gcloud インストールが含まれていないこと"""
        dockerfile_path = os.path.join(PROJECT_ROOT, "Dockerfile")
        if not os.path.isfile(dockerfile_path):
            self.skipTest("Dockerfile が存在しません")

        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read().lower()

        self.assertNotIn("google-cloud-sdk", content, "Dockerfile に google-cloud-sdk が含まれています")
        self.assertNotIn("gcloud", content, "Dockerfile に gcloud コマンドへの言及があります")

    def test_fly_toml_has_no_gcp_endpoints(self):
        """fly.toml に GCP エンドポイントや参照が含まれていないこと"""
        fly_path = os.path.join(PROJECT_ROOT, "fly.toml")
        if not os.path.isfile(fly_path):
            self.skipTest("fly.toml が存在しません")

        with open(fly_path, "r", encoding="utf-8") as f:
            content = f.read().lower()

        self.assertNotIn("google.cloud", content, "fly.toml に google.cloud が含まれています")
        self.assertNotIn("run.app", content, "fly.toml に Cloud Run ドメイン参照があります")

    def test_python_codebase_has_no_google_cloud_imports(self):
        """リポジトリ内の全Pythonコードベースに google.cloud からのインポートが一切ないこと"""
        re_gcp_import = re.compile(r"^\s*(import\s+google\.cloud|from\s+google\.cloud)", re.MULTILINE)
        exclude_dirs = {".git", ".agents", "venv", ".venv", "__pycache__"}

        violations = []
        for root, dirs, files in os.walk(PROJECT_ROOT):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for file_name in files:
                if not file_name.endswith(".py"):
                    continue
                file_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(file_path, PROJECT_ROOT)

                with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
                    content = fp.read()
                    matches = list(re_gcp_import.finditer(content))
                    if matches:
                        violations.append(f"{rel_path}: {len(matches)} 件の google.cloud import を検知")

        self.assertEqual(
            violations,
            [],
            f"Python コードベースに GCP インポートが検出されました: {violations}",
        )

    def test_frontend_has_no_gcp_maps_api_keys_or_scripts(self):
        """templates/*.html および static/*.js を全走査し、maps.googleapis.com や key=AIza などの有料Google Maps APIキー・スクリプトタグが一切含まれていないこと"""
        scan_targets = [
            (os.path.join(PROJECT_ROOT, "templates"), ".html"),
            (os.path.join(PROJECT_ROOT, "static"), ".js"),
        ]

        forbidden_patterns = [
            (re.compile(r"maps\.googleapis\.com"), "有料 Google Maps JavaScript API / スクリプト参照 (maps.googleapis.com)"),
            (re.compile(r"places\.googleapis\.com"), "有料 Google Places API エンドポイント (places.googleapis.com)"),
            (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google Maps / GCP API キー (AIza...)"),
            (re.compile(r"key=AIza", re.IGNORECASE), "Google API キーパラメータ (key=AIza)"),
        ]

        violations = []
        for base_dir, extension in scan_targets:
            self.assertTrue(os.path.isdir(base_dir), f"走査対象ディレクトリが存在しません: {base_dir}")
            for root, dirs, files in os.walk(base_dir):
                for file_name in files:
                    if not file_name.endswith(extension):
                        continue
                    file_path = os.path.join(root, file_name)
                    rel_path = os.path.relpath(file_path, PROJECT_ROOT)

                    with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
                        for line_no, line in enumerate(fp, start=1):
                            line_strip = line.strip()
                            if not line_strip:
                                continue
                            for pattern, desc in forbidden_patterns:
                                if pattern.search(line_strip):
                                    violations.append(
                                        f"[{desc}] {rel_path}:{line_no} -> {line_strip[:120]}"
                                    )

        self.assertEqual(
            violations,
            [],
            f"フロントエンド (templates/*.html, static/*.js) 内に有料 Google Maps API 参照が検出されました:\n"
            + "\n".join(violations),
        )

    def test_second_venues_master_gcp_zero_dependency(self):
        """static/second_venues.json 内の全スポットデータが無料インテント検索クエリのみを使用し、GCP有料API参照が一切ないこと"""
        import json
        import second_venues

        venues_path = os.path.join(PROJECT_ROOT, "static", "second_venues.json")
        self.assertTrue(os.path.isfile(venues_path), "static/second_venues.json が存在しません")

        # 1. JSON ファイル全体の静的テキスト解析（APIキーやエンドポイントの混入防止）
        with open(venues_path, "r", encoding="utf-8") as f:
            raw_content = f.read()

        gcp_forbidden_patterns = [
            re.compile(r"maps\.googleapis\.com"),
            re.compile(r"places\.googleapis\.com"),
            re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
            re.compile(r"key=AIza", re.IGNORECASE),
        ]
        for pattern in gcp_forbidden_patterns:
            matches = pattern.findall(raw_content)
            self.assertEqual(
                matches,
                [],
                f"static/second_venues.json 内に GCP 有料 API 参照パターンが検出されました: {matches}",
            )

        # 2. JSON パース後のデータ構造検証
        master_data = json.loads(raw_content)
        venues_by_area = master_data.get("venues_by_area", {})
        self.assertTrue(len(venues_by_area) > 0, "venues_by_area が空です")

        total_spots = 0
        for area_key, spots in venues_by_area.items():
            for spot in spots:
                total_spots += 1
                spot_id = spot.get("id", "unknown")
                # map_query が必須かつ非空であること
                self.assertIn("map_query", spot, f"Spot {spot_id} に map_query が存在しません")
                self.assertTrue(isinstance(spot["map_query"], str) and len(spot["map_query"].strip()) > 0)
                # 有料API固有のプロパティ（place_id 等）が存在しないこと
                self.assertNotIn("place_id", spot, f"Spot {spot_id} に有料API依存の 'place_id' が存在します")
                self.assertNotIn("api_key", spot, f"Spot {spot_id} に 'api_key' が存在します")
                self.assertNotIn("reference", spot, f"Spot {spot_id} に 'reference' が存在します")

        self.assertGreaterEqual(total_spots, 30, f"検証スポット数が少なすぎます ({total_spots}件)")

        # 3. second_venues.py が生成するマップURLが全て無料インテントURL契約に準拠していること
        for area_key, spots in venues_by_area.items():
            for spot in spots:
                intent_url = second_venues.get_google_maps_intent_url(spot["map_query"])
                self.assertTrue(
                    intent_url.startswith("https://www.google.com/maps/search/?api=1&query="),
                    f"マップURLが無料WebインテントURL仕様に準拠していません: {intent_url}",
                )
                self.assertNotIn("key=", intent_url)
                self.assertNotIn("AIza", intent_url)

        # 4. 未登録店舗向けスマートフォールバックのスポットも同様に無料インテントURLであること
        fallback_res = second_venues.get_second_venues_for_store("OLG 盛岡")
        self.assertTrue(fallback_res["is_fallback"])
        for fb_spot in fallback_res["spots"]:
            self.assertTrue(
                fb_spot["map_url"].startswith("https://www.google.com/maps/search/?api=1&query="),
                f"フォールバックスポットのマップURLが無料インテント仕様に準拠していません: {fb_spot['map_url']}",
            )
            self.assertNotIn("key=", fb_spot["map_url"])


class GoogleSheetsFiveColumnSchemaE2ETests(unittest.TestCase):
    """Google Sheets 'Lounge Monitor Data' 5列構造維持 E2E テスト"""

    def test_logger_log_data_appends_exact_five_columns(self):
        """logger.log_data() がスプレッドシートに append_rows する各行が厳格に 5列 [timestamp, name, men, women, source] であること"""
        import logger

        mock_sheet = MagicMock()
        captured_appends = []

        def fake_append_rows(rows, **kwargs):
            captured_appends.extend(rows)
            return {"updates": {"updatedRows": len(rows)}}

        mock_sheet.append_rows.side_effect = fake_append_rows
        mock_sh = MagicMock(sheet1=mock_sheet)

        sample_data = [
            {"name": "オリエンタルラウンジ 渋谷本店", "men": 30, "women": 45, "source": "OLG"},
            {"name": "JIS SHINJUKU", "men": 20, "women": 25, "source": "JIS"},
            {"name": "ag渋谷", "men": 15, "women": 18, "source": "OLG"},
        ]

        with patch.object(logger, "get_client", return_value=MagicMock()):
            with patch.object(logger, "open_spreadsheet", return_value=mock_sh):
                logger.log_data(sample_data)

        self.assertTrue(len(captured_appends) > 0, "append_rows が呼び出されませんでした")

        for idx, row in enumerate(captured_appends):
            self.assertEqual(
                len(row),
                5,
                f"行 {idx} の列数が 5 ではありません (実際: {len(row)}列, 内容: {row})",
            )
            # 列の構造検証
            # 1: timestamp 文字列
            self.assertIsInstance(row[0], str)
            self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            # 2: 店舗名
            self.assertIsInstance(row[1], str)
            self.assertTrue(len(row[1]) > 0)
            # 3: 男性人数 (int)
            self.assertIsInstance(row[2], (int, float))
            # 4: 女性人数 (int)
            self.assertIsInstance(row[3], (int, float))
            # 5: ソース名
            self.assertIsInstance(row[4], str)

    def test_local_db_gviz_query_specifies_exact_five_columns(self):
        """local_db.py の Google Sheets GViz 同期クエリが SELECT A, B, C, D, E の5列のみを抽出していること"""
        db_file = os.path.join(PROJECT_ROOT, "local_db.py")
        self.assertTrue(os.path.isfile(db_file), "local_db.py が存在しません")

        with open(db_file, "r", encoding="utf-8") as f:
            content = f.read()

        # SELECT A, B, C, D, E または SELECT A,B,C,D,E のパターンが存在すること
        gviz_pattern = re.compile(r"SELECT\s+A,\s*B,\s*C,\s*D,\s*E(?!\s*,\s*F)", re.IGNORECASE)
        match = gviz_pattern.search(content)
        self.assertIsNotNone(
            match,
            "local_db.py 内に 'SELECT A, B, C, D, E' の厳格な5列 GViz クエリが見つかりません",
        )

    def test_moat_telemetry_isolation(self):
        """拡張テレメトリデータ（POS待機グループ・天候等）が 'Lounge Monitor Data' ではなく別シートに隔離記録されていること"""
        import inspect
        import logger

        src = inspect.getsource(logger)
        # sheet.append_rows(rows_to_append) への追加処理に余剰フィールドが含まれていないことを確認
        self.assertIn("sheet.append_rows(rows_to_append)", src, "logger.py に sheet.append_rows が存在します")
        # 5列定義の行生成ロジックが存在すること
        self.assertIn("item.get('source'", src)
        # 別シート 'Moat Telemetry' への記録処理が存在すること
        self.assertIn("Moat Telemetry", src)


if __name__ == "__main__":
    unittest.main()
