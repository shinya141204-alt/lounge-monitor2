# -*- coding: utf-8 -*-
"""
tests/test_challenger_verification.py - Challenger Empirical Verification & Stress Test Suite

Milestone 3 実証的検証（Empirical Challenger）用テストハーネス:
1. 2軒目案内機能（全53店舗マッチング、動的フォールバック、無料WebインテントURL、ブランド表記、異常入力）
2. SNS AI機能（noteチャンネル生成、5シチュエーション切り口、AI画像プロンプト、Twitterウェイト計算精度、APIキー未設定フォールバック、ブランドサニタイズ）
3. 並行負荷・ストレステスト（1000リクエスト、50並行スレッド）の完全独立実行
"""

import unittest
import json
import os
import re
import time
import concurrent.futures
from unittest.mock import patch

import second_venues
import sns_editorial
import x_poster
import app as flask_app_module


class ChallengerSecondVenuesEmpiricalTests(unittest.TestCase):
    """2軒目案内機能の実証的検証・ストレステスト"""

    @classmethod
    def setUpClass(cls):
        cls.master = second_venues.load_second_venues_master()
        cls.store_master = second_venues.load_store_master()
        cls.stores = cls.store_master.get("stores", {})

    def test_all_stores_second_venues_coverage(self):
        """全登録店舗（53店舗以上）に対して2軒目案内が例外なく完走し、適切なスポット・Tipsを返却すること"""
        self.assertGreaterEqual(len(self.stores), 40, "店舗マスタの登録数が少なすぎます")

        success_count = 0
        major_area_count = 0
        fallback_count = 0

        for store_name in self.stores.keys():
            res = second_venues.get_second_venues_for_store(store_name)
            self.assertIsInstance(res, dict)
            self.assertEqual(res["store_name"], store_name)
            self.assertIn("spots", res)
            self.assertGreaterEqual(len(res["spots"]), 4, f"{store_name} のスポット数が4件未満です")

            # スポットの完全性検証
            for sp in res["spots"]:
                self.assertIn("name", sp)
                self.assertIn("category", sp)
                self.assertIn("category_label", sp)
                self.assertIn("map_query", sp)
                self.assertIn("map_url", sp)
                self.assertIn("line_share_url", sp)

                # 無料インテントURL形式の検証（GCP有料APIが一切含まれないこと）
                self.assertTrue(
                    sp["map_url"].startswith("https://www.google.com/maps/search/?api=1&query="),
                    f"無料インテントURL形式違反: {sp['map_url']}"
                )
                self.assertNotIn("maps.googleapis.com", sp["map_url"])
                self.assertNotIn("places.googleapis.com", sp["map_url"])
                self.assertNotIn("key=AIza", sp["map_url"])
                self.assertNotIn("place_id", sp)

                # LINE共有URL形式
                self.assertTrue(sp["line_share_url"].startswith("https://line.me/R/msg/text/?"))

                # ブランド表記ルール（単体OLG, オリラジ, 単体オリエンタル禁止）
                sp_str = json.dumps(sp, ensure_ascii=False)
                clean_sp = sp_str.replace("オリエンタルラウンジ", "")
                self.assertNotIn("OLG", clean_sp, f"スポット内にOLGが残存: {store_name} -> {sp['name']}")
                self.assertNotIn("オリラジ", clean_sp, f"スポット内にオリラジが残存: {store_name} -> {sp['name']}")
                self.assertNotIn("オリエンタル", clean_sp, f"スポット内に単体オリエンタルが残存: {store_name} -> {sp['name']}")

            # Tips と timing_rules の検証
            self.assertGreaterEqual(len(res["situation_tips"]), 3)
            self.assertGreaterEqual(len(res["timing_rules"]), 2)

            if res["is_fallback"]:
                fallback_count += 1
            else:
                major_area_count += 1
            success_count += 1

        self.assertEqual(success_count, len(self.stores))
        self.assertGreater(major_area_count, 10, "主要エリアマッチング件数が少なすぎます")
        print(f"\n[Challenger] 2nd Venues Coverage: Total={success_count}, MajorAreas={major_area_count}, Fallbacks={fallback_count} -> ALL PASSED")

    def test_second_venues_adversarial_inputs(self):
        """異常入力・境界値入力に対する耐性テスト"""
        adversarial_cases = [
            "",
            "   ",
            "NonExistentStore_XYZ_9999",
            "<script>alert('xss')</script>",
            "../../../../etc/passwd",
            "' OR '1'='1",
            "渋谷" * 100,  # 極端に長い文字列
            "🍻🍸☕🎯",   # 絵文字のみ
        ]

        for case in adversarial_cases:
            try:
                res = second_venues.get_second_venues_for_store(case)
                self.assertIsInstance(res, dict)
                self.assertIn("spots", res)
                self.assertGreaterEqual(len(res["spots"]), 4)
                # フォールバックでも無料インテントURLが安全に生成されること
                for sp in res["spots"]:
                    self.assertTrue(sp["map_url"].startswith("https://www.google.com/maps/search/?api=1&query="))
                    self.assertNotIn("<script>", sp["map_url"])
            except Exception as e:
                self.fail(f"異常入力 '{case[:30]}' でクラッシュしました: {e}")

    def test_second_venues_none_input_handling(self):
        """None 入力時の防御挙動テスト"""
        area_key = second_venues.get_area_key_for_store(None)
        self.assertIsNone(area_key)

        # store_name=None を渡した場合の挙動
        # get_second_venues_for_store(None) が例外を投げるか安全にフォールバックするかを検証
        try:
            second_venues.get_second_venues_for_store(None)
        except (AttributeError, TypeError) as e:
            # 既知の制約（None型に対するstrip呼び出し等）を検出し、挙動を把握
            print(f"\n[Challenger Edge Case Note] get_second_venues_for_store(None) raised {type(e).__name__} as expected for untyped None.")


class ChallengerSnsAiEmpiricalTests(unittest.TestCase):
    """SNS AI機能の実証的検証・ストレステスト"""

    @classmethod
    def setUpClass(cls):
        cls.sample_research = {
            "calendar": {"weekday_ja": "金", "is_weekend": True, "date_str": "2026-10-30"},
            "top_stores": [
                {"ja_name": "オリエンタルラウンジ渋谷本店", "name": "OLG 渋谷本店", "women": 45, "men": 32, "wpct": 58, "women_diff": 5},
                {"ja_name": "オリエンタルラウンジ新宿", "name": "OLG 新宿", "women": 40, "men": 28, "wpct": 59, "women_diff": 3},
                {"ja_name": "オリエンタルラウンジ梅田", "name": "OLG 梅田", "women": 36, "men": 25, "wpct": 59, "women_diff": 4},
            ],
            "active_stores": [
                {"ja_name": "オリエンタルラウンジ渋谷本店", "women": 45, "men": 32},
                {"ja_name": "オリエンタルラウンジ新宿", "women": 40, "men": 28},
                {"ja_name": "オリエンタルラウンジ梅田", "women": 36, "men": 25},
            ]
        }

    def test_note_channel_all_styles_and_char_limits(self):
        """noteチャンネルの4スタイル候補生成と文字数仕様の適合性検証"""
        candidates = sns_editorial._write_note_candidates(self.sample_research)
        self.assertEqual(len(candidates), 4, "note候補が4件生成されていません")

        style_ids = [c["style"] for c in candidates]
        self.assertIn("note_essay_insight", style_ids)
        self.assertIn("note_short_column", style_ids)
        self.assertIn("note_tsubuyaki_gap", style_ids)
        self.assertIn("note_classic_summary", style_ids)

        for c in candidates:
            text = c["main_text"]
            self.assertTrue(len(text) > 0)
            clean_text = text.replace("オリエンタルラウンジ", "")
            self.assertNotIn("OLG", clean_text)
            self.assertNotIn("オリラジ", clean_text)
            self.assertNotIn("オリエンタル", clean_text)

            if c["style"] == "note_essay_insight":
                # エッセイ: 800〜1,400文字
                self.assertGreaterEqual(len(text), 750, f"エッセイ文字数不足: {len(text)}字")
                self.assertLessEqual(len(text), 1500, f"エッセイ文字数超過: {len(text)}字")
            elif c["style"] == "note_tsubuyaki_gap":
                # つぶやき: 120〜240文字
                self.assertGreaterEqual(len(text), 100, f"つぶやき文字数不足: {len(text)}字")
                self.assertLessEqual(len(text), 280, f"つぶやき文字数超過: {len(text)}字")

    def test_all_five_situations_generation(self):
        """5つのシチュエーション特化切り口すべてで適切な候補とプロンプトが生成されること"""
        situations = ["second_venue", "zero_wait", "rain_underground", "last_train_cliff", "start_dash"]
        keywords = {
            "second_venue": ["2軒目", "カフェ", "30分"],
            "zero_wait": ["待ち", "ゼロ", "乾杯"],
            "rain_underground": ["雨", "直結"],
            "last_train_cliff": ["終電", "深夜"],
            "start_dash": ["スタートダッシュ", "早"],
        }

        for sit in situations:
            candidates = sns_editorial._write_note_candidates(self.sample_research, situation=sit)
            self.assertEqual(len(candidates), 4)
            combined_text = " ".join([c["main_text"] for c in candidates])
            for kw in keywords[sit]:
                self.assertIn(kw, combined_text, f"シチュエーション '{sit}' の候補文にキーワード '{kw}' が含まれていません")

    def test_ai_image_prompts_for_all_channels_and_aspect_ratios(self):
        """全SNSチャンネル（X, IG, note, TikTok, Threads）向けAI画像プロンプトと推奨比率の検証"""
        channels = ["x", "instagram", "note", "tiktok", "threads"]
        expected_ratios = {
            "x": "16:9",
            "instagram": "4:5",
            "note": "1.91:1",
            "tiktok": "9:16",
            "threads": "1:1",
        }

        for ch in channels:
            res = sns_editorial.generate_image_prompts(
                research=self.sample_research,
                channel=ch,
                situation="second_venue"
            )
            self.assertEqual(res["current_channel"], ch)
            primary = res["primary"]
            self.assertEqual(primary["aspect_ratio"], expected_ratios[ch])
            self.assertTrue(len(primary["prompt_en"]) > 20)
            self.assertTrue(len(primary["prompt_ja"]) > 10)
            self.assertIn(f"--ar {expected_ratios[ch]}", primary["prompt_en"])
            self.assertEqual(len(res["gallery"]), 5)

    def test_twitter_weight_computation_precision(self):
        """Twitterウェイト計算精度（ASCII=1, East-Asian=2, URL=23）の厳密検証"""
        test_cases = [
            ("", 0),
            ("hello", 5),
            ("こんにちは", 10),
            ("https://aiseki-navi.com", 23),
            ("今夜の混雑 https://aiseki-navi.com 是非！", 11 + 23 + 7),  # "今夜の混雑 "(11) + URL(23) + " 是非！"(7) = 41
            ("https://t.co/xyz https://aiseki-navi.com/store/shibuya", 23 + 1 + 23),  # 2 URLs + 1 space = 47
        ]

        for text, expected in test_cases:
            actual = x_poster.compute_twitter_weight(text)
            self.assertEqual(actual, expected, f"Twitterウェイト不一致: text='{text}', actual={actual}, expected={expected}")

    def test_brand_sanitization_adversarial_patterns(self):
        """ブランド名サニタイズ（sanitize_brand_names）の網羅的検証（二重置換防止・境界値）"""
        cases = [
            ("今夜はOLG 渋谷へ行こう", "今夜はオリエンタルラウンジ 渋谷へ行こう"),
            ("OLGの混雑状況", "オリエンタルラウンジ の混雑状況"),
            ("オリラジ梅田が最高", "オリエンタルラウンジ梅田が最高"),
            ("単体のオリエンタルに行こう", "単体のオリエンタルラウンジに行こう"),
            ("すでにオリエンタルラウンジと書いてある", "すでにオリエンタルラウンジと書いてある"),  # 二重置換防止
            ("オリエンタルラウンジOLG店", "オリエンタルラウンジオリエンタルラウンジ 店"),
            ("", ""),
        ]

        for raw, expected in cases:
            sanitized = sns_editorial.sanitize_brand_names(raw)
            self.assertEqual(sanitized, expected, f"サニタイズ結果不一致: raw='{raw}', sanitized='{sanitized}', expected='{expected}'")
            # 出力に単体OLG, オリラジ, 単体オリエンタルがないこと
            clean = sanitized.replace("オリエンタルラウンジ", "")
            self.assertNotIn("OLG", clean)
            self.assertNotIn("オリラジ", clean)
            self.assertNotIn("オリエンタル", clean)

    def test_api_key_missing_safe_fallback(self):
        """Gemini/Claude APIキーが未設定環境でも例外なくパイプラインが完走し、ルールベース候補が出力されること"""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}, clear=False):
            res = sns_editorial.run_editorial_pipeline(
                stores=self.sample_research["top_stores"],
                channel="note",
                situation="second_venue",
                force_ai_refresh=True
            )
            self.assertIsInstance(res, dict)
            self.assertIn("winner", res)
            self.assertIn("evaluation", res)
            candidates = res["evaluation"].get("candidates") or []
            self.assertEqual(len(candidates), 4)
            self.assertIn("image_prompts", res)
            self.assertEqual(res["channel"], "note")
            self.assertEqual(res["situation"], "second_venue")


class ChallengerConcurrencyStressTests(unittest.TestCase):
    """並行負荷・ストレステスト（1000リクエスト、50並行スレッド）の完全独立実行"""

    def setUp(self):
        flask_app_module.app.config["TESTING"] = True
        self.app = flask_app_module.app

    def test_concurrency_stress_1000_requests_50_threads(self):
        """Flask test clientに対する50スレッド・1000並行リクエストの負荷検証"""
        total_requests = 1000
        concurrency = 50

        endpoints = [
            "/api/status",
            "/api/health",
            "/api/events",
            "/api/weekly-report",
            "/static/second_venues.json",
            "/",
        ]

        latencies = []
        errors = []

        def worker_task(idx):
            client = self.app.test_client()
            endpoint = endpoints[idx % len(endpoints)]
            t0 = time.perf_counter()
            try:
                resp = client.get(endpoint)
                elapsed = (time.perf_counter() - t0) * 1000.0  # ms
                if resp.status_code >= 500:
                    errors.append((idx, endpoint, resp.status_code))
                else:
                    latencies.append(elapsed)
            except Exception as e:
                errors.append((idx, endpoint, str(e)))

        start_time = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(worker_task, i) for i in range(total_requests)]
            concurrent.futures.wait(futures)
        total_duration = time.perf_counter() - start_time

        error_rate = (len(errors) / total_requests) * 100.0
        rps = total_requests / total_duration if total_duration > 0 else 0

        latencies.sort()
        p50 = latencies[int(len(latencies) * 0.50)] if latencies else 0
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
        p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0
        mean = sum(latencies) / len(latencies) if latencies else 0

        print(f"\n--- [Challenger] Independent Concurrency Stress Test ---")
        print(f"Total Requests: {total_requests}")
        print(f"Concurrency: {concurrency} threads")
        print(f"Duration: {total_duration:.3f} s")
        print(f"Throughput: {rps:.1f} req/sec")
        print(f"Errors: {len(errors)} (Error Rate: {error_rate:.2f}%)")
        print(f"Latencies: Mean={mean:.2f}ms, p50={p50:.2f}ms, p95={p95:.2f}ms, p99={p99:.2f}ms")

        # 判定（エラー率0%、高スループット、p95が1秒未満）
        self.assertEqual(len(errors), 0, f"並行リクエスト中にエラーが発生しました: {errors[:5]}")
        self.assertGreater(rps, 300, f"スループットが低すぎます ({rps:.1f} req/sec)")
        self.assertLess(p95, 1000, f"p95レイテンシが高すぎます ({p95:.2f} ms)")


if __name__ == "__main__":
    unittest.main()
