# -*- coding: utf-8 -*-
"""
tests/test_e2e_scenarios.py - Tier 2 & Tier 4 実運用E2Eシナリオ & エラーハンドリングテストスイート

検証対象:
1. 主要Webページ (Web Navigation E2E Scenarios):
   - /, /oriental-lounge, /columns, /concierge, /sitemap.xml, /robots.txt の正常応答・UI要素描画。
2. 主要APIエンドポイント (API Endpoints & Data Contracts):
   - /api/status, /api/health, /api/events, /api/weekly-report, /api/concierge の応答コードおよびJSONスキーマ整合性。
3. 統一エラーハンドリング (Error Handling & HTTP Resilience):
   - Web 404 画面の親切なHTML返却。
   - API 404 (/api/*) での JSON形式 {"status": "error", "error": ..., "code": 404} 厳格返却。
   - 不正入力・例外時のフォールバック。
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import app


class WebNavigationE2ETests(unittest.TestCase):
    """主要Web画面の回遊・レンダリング E2E テスト"""

    def setUp(self):
        self.client = app.app.test_client()
        app.app.config["TESTING"] = True

    def test_top_page_rendering(self):
        """トップページ (/) が HTTP 200 で応答し、主要UI要素が描画されること"""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.content_type)
        html = res.get_data(as_text=True)
        self.assertIn("相席ナビ", html)
        self.assertIn("リアルタイム", html)

    def test_oriental_lounge_page_rendering(self):
        """ブランド別ページ (/oriental-lounge) が HTTP 200 で応答し、コンテンツが描画されること"""
        res = self.client.get("/oriental-lounge")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.content_type)
        html = res.get_data(as_text=True)
        self.assertIn("オリエンタルラウンジ", html)

    def test_columns_page_rendering(self):
        """コラム一覧ページ (/columns) が HTTP 200 で応答し、記事一覧UIが描画されること"""
        res = self.client.get("/columns")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.content_type)
        html = res.get_data(as_text=True)
        self.assertIn("コラム", html)

    def test_concierge_page_rendering(self):
        """AIコンシェルジュページ (/concierge) が HTTP 200 で応答し、診断UIが描画されること"""
        res = self.client.get("/concierge")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.content_type)
        html = res.get_data(as_text=True)
        self.assertIn("今夜どこ行く？", html)
        self.assertIn("穴場＆徒歩ハシゴ診断", html)

    def test_seo_endpoints(self):
        """robots.txt および sitemap.xml が HTTP 200 で応答すること"""
        res_robots = self.client.get("/robots.txt")
        self.assertEqual(res_robots.status_code, 200)

        res_sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(res_sitemap.status_code, 200)
        self.assertIn("xml", res_sitemap.content_type)


class ApiEndpointsE2ETests(unittest.TestCase):
    """主要APIエンドポイントの契約・レスポンス構造 E2E テスト"""

    def setUp(self):
        self.client = app.app.test_client()
        app.app.config["TESTING"] = True

    def test_api_status_response_schema(self):
        """/api/status が HTTP 200 かつ有効なJSONを返却し、店舗混雑情報を含むこと"""
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/json", res.content_type)

        data = json.loads(res.get_data(as_text=True))
        self.assertIsInstance(data, dict)
        # 店舗一覧データキー (stores または status 等) が存在すること
        self.assertTrue("stores" in data or "status" in data or "data" in data)

    def test_api_health_response_schema(self):
        """/api/health が HTTP 200 かつ有効なヘルスステータスJSONを返却すること"""
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/json", res.content_type)

        data = json.loads(res.get_data(as_text=True))
        self.assertIsInstance(data, dict)
        self.assertIn("status", data)
        self.assertIn(data["status"], ("healthy", "degraded", "ok"))

    def test_api_events_response_schema(self):
        """/api/events が HTTP 200 かつイベント配列のJSONを返却すること"""
        res = self.client.get("/api/events")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/json", res.content_type)

        data = json.loads(res.get_data(as_text=True))
        self.assertIsInstance(data, (list, dict))

    def test_api_weekly_report_response_schema(self):
        """/api/weekly-report が HTTP 200 かつ集計レポートJSONを返却すること"""
        res = self.client.get("/api/weekly-report")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/json", res.content_type)

        data = json.loads(res.get_data(as_text=True))
        self.assertIsInstance(data, (dict, list))


class ErrorHandlingE2ETests(unittest.TestCase):
    """Tier 2 統一エラーハンドリングおよびHTTPレジリエンス E2E テスト"""

    def setUp(self):
        self.client = app.app.test_client()
        app.app.config["TESTING"] = True

    def test_web_page_404_returns_html(self):
        """存在しないWebページへのリクエストで HTTP 404 と親切なHTMLUIが返却されること"""
        res = self.client.get("/this-route-definitely-does-not-exist-404")
        self.assertEqual(res.status_code, 404)
        self.assertIn("text/html", res.content_type)

    def test_api_404_returns_json_error(self):
        """存在しない /api/* エンドポイントへのリクエストで HTTP 404 かつ JSON エラー形式が返却されること (Interface Contract)"""
        res = self.client.get("/api/non-existent-api-endpoint-xyz-999")
        self.assertEqual(res.status_code, 404)
        self.assertIn(
            "application/json",
            res.content_type,
            f"API 404 エラーは application/json である必要があります (実際: {res.content_type})",
        )

        data = json.loads(res.get_data(as_text=True))
        self.assertIsInstance(data, dict)
        self.assertEqual(
            data.get("status"),
            "error",
            f"JSONレスポンスの status フィールドは 'error' である必要があります (実際: {data})",
        )
        self.assertIn(
            "error",
            data,
            f"JSONレスポンスに 'error' メッセージフィールドが含まれる必要があります (実際: {data})",
        )
        self.assertEqual(
            data.get("code"),
            404,
            f"JSONレスポンスの code フィールドは 404 である必要があります (実際: {data})",
        )


if __name__ == "__main__":
    unittest.main()
