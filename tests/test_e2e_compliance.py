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
