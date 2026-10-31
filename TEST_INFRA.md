# 相席ナビ (AISEKI NAVI) テストインフラストラクチャ設計書 (TEST_INFRA.md)

本ドキュメントは、要求仕様書 (`ORIGINAL_REQUEST.md`) およびプロジェクト仕様書 (`PROJECT.md`) に基づき構築された、相席ナビの E2E・結合・単体テストスイート全体のインフラストラクチャ設計、4階層テストアーキテクチャ、およびフィーチャーカバレッジマトリクスを定義するものである。

---

## 1. テストアーキテクチャ概要 (4-Tier Architecture)

本プロジェクトでは、実装詳細に依存しない不透明ボックス（Opaque-box）検証を採用し、以下の4階層アプローチで品質とリグレッション耐性を担保する：

```
+-------------------------------------------------------------------------------+
|  Tier 4: Real-World Scenarios (本番相当環境・API疎通・ヘルスチェック)         |
|  - 公開API (GET /api/status, /api/events, /api/health) 200疎通 & 構造検証      |
|  - 404エラーハンドラ (HTML / JSON コンテントネゴシエーション契約)             |
+-------------------------------------------------------------------------------+
                                    ▲
+-------------------------------------------------------------------------------+
|  Tier 3: Cross-Feature Combinations (複合障害シナリオ・システム間結合)        |
|  - スクレイピング障害時の直近DBスナップショット補完による全店舗非消失検証      |
|  - ServiceWorker (sw.js) CORE_ASSETS プリキャッシュ実体ファイル実在性検証     |
+-------------------------------------------------------------------------------+
                                    ▲
+-------------------------------------------------------------------------------+
|  Tier 2: Boundary & Corner Cases (境界値・異常入力・耐障害性)                 |
|  - x_poster: 店舗人数が None / 未設定 / 空文字 時の TypeError 防御            |
|  - sns_workflows: 店舗人数が None / 空文字 時の比較・加算エラー防御           |
|  - moat_intelligence: 前日差分値が空文字 "" / 異常値時の ValueError 防御      |
|  - local_db: Google Sheets GViz API 通信障害時の 0 件クリーンフォールバック    |
|  - local_db: 非同期バックフィルにおける _active_db_key() インフライトキー整合  |
+-------------------------------------------------------------------------------+
                                    ▲
+-------------------------------------------------------------------------------+
|  Tier 1: Feature Coverage (仕様遵守・コードベース健全性・静的アセット)        |
|  - F1.1: scripts/legacy/ 配下全21スクリプト完全削除検証                       |
|  - F1.3: static/ 配下の未参照デッド画像 (thumb-note-eyecatch*) 削除検証        |
|  - F1.4: benchmark_*_result.json の Git 追跡除外 (.gitignore) 検証           |
|  - F3.1: style.css キャッシュバスティング (?v=20261030_6) 全テンプレート同期  |
|  - R3: 公開UI全域における単体「OLG」「オリラジ」「オリエンタル」ゼロ検証      |
|  - R3: Google Cloud (google.cloud) 有料SDK依存ゼロ検証                        |
|  - R3: static/calm.css の html セレクタ PC縦スクロール維持 (overflowなし)    |
+-------------------------------------------------------------------------------+
```

---

## 2. Feature Inventory とテスト対応マトリクス (Coverage Mapping)

PROJECT.md に定義された全 Feature（F1.1〜F4.3）とテストスイートの対応表：

| # | Feature | 概要 | 実装マイルストーン | テストケース (`tests/test_clean_architecture_e2e.py`) | 現況 (Testing Track) |
|---|---------|------|-------------------|------------------------------------------------------|----------------------|
| 1 | **F1.1** | `scripts/legacy/` 配下全21スクリプト完全削除 | M1 | `Tier1FeatureCoverageTests.test_01_legacy_scripts_completely_removed` | **PASS** (削除確認済) |
| 2 | **F1.2** | `monitor.py` 未参照関数削除 | M1 | 既存 `test_m2_worker_robustness.py` & 参照ゼロ | **PASS** |
| 3 | **F1.3** | `static/thumb-note-eyecatch*` 4ファイル削除 | M1 | `Tier1FeatureCoverageTests.test_02_dead_images_completely_removed` | **PASS** (削除確認済) |
| 4 | **F1.4** | `benchmark_*_result.json` の `.gitignore` 追跡除外 | M1 | `Tier1FeatureCoverageTests.test_03_benchmark_result_json_ignored` | **PASS** (登録確認済) |
| 5 | **F2.1** | `x_poster.py` の `None` 起因 `TypeError` 防御 | M2 | `Tier2BoundaryAndCornerCasesTests.test_01_x_poster_none_and_empty_headcount_robustness` | **RED** (M2改修待ち) |
| 6 | **F2.2** | `sns_workflows.py` の `None` 起因比較・加算エラー防御 | M2 | `Tier2BoundaryAndCornerCasesTests.test_02_sns_workflows_none_and_empty_robustness` | **RED** (M2改修待ち) |
| 7 | **F2.3** | `moat_intelligence.py` の空文字型キャスト `ValueError` 防御 | M2 | `Tier2BoundaryAndCornerCasesTests.test_03_moat_intelligence_diff_empty_and_abnormal_handling` | **RED** (M2改修待ち) |
| 8 | **F2.4** | `local_db.py` の GViz 通信例外ハンドリング強化 | M2 | `Tier2BoundaryAndCornerCasesTests.test_04_local_db_gviz_network_failure_fallback` | **PASS** |
| 9 | **F2.4** | `local_db.py` の `_active_db_key()` キー整合 | M2 | `Tier2BoundaryAndCornerCasesTests.test_05_local_db_inflight_key_consistency` | **RED** (M2改修待ち) |
| 10 | **F2.5** | `monitor.py` Fly.io IPv6 ガード | M2 | 既存 `test_m2_worker_robustness.py` | **PASS** |
| 11 | **F2.6** | `worker.py` graceful shutdown タイムアウト短縮 | M2 | 既存 `test_m2_worker_robustness.py` | **PASS** |
| 12 | **F2.7** | `templates/index.html` サムネイル画像軽量化 | M2 | 既存 `test_ui_audit_regression.py` | **PASS** |
| 13 | **F2.8** | `test_challenger_verification.py` ResourceWarning 解消 | M2 | 既存 `test_challenger_verification.py` | **PASS** |
| 14 | **F3.1** | `style.css` バージョン (`?v=20261030_6`) 全テンプレート・sw.js 完全同期 | M3 | `Tier1FeatureCoverageTests.test_04_cache_busting_sync_across_templates_and_sw` | **RED** (M3改修待ち) |
| 15 | **F3.2** | クリーンアーキテクチャ E2E 検証スイート新設 | M3 | `tests/test_clean_architecture_e2e.py` (全16テスト新設) | **READY** |
| 16 | **F3.3** | 既存全228件＋新設テスト全件通過確認 | M3 | `python3 -m unittest discover -s tests` | **M3完了後に全件PASS予定** |
| 17 | **F4.1** | GitHub `main` ブランチへのコミット・プッシュ | M4 | Git CI/CD | M4予定 |
| 18 | **F4.2** | Fly.io 本番デプロイ (`flyctl deploy`) | M4 | Fly.io Release Engine | M4予定 |
| 19 | **F4.3** | 本番稼働確認 & ヘルスチェック | M4 | `Tier4RealWorldScenariosTests.test_01_production_endpoint_health_checks` | **PASS** (ローカル検証済) |
| 20 | **R3** | 正式名称『オリエンタルラウンジ』遵守 | 全般 | `Tier1FeatureCoverageTests.test_05_brand_compliance_public_surface` | **PASS** |
| 21 | **R3** | GCP 有料SDK完全ゼロ依存 | 全般 | `Tier1FeatureCoverageTests.test_06_no_paid_gcp_sdk_imports` | **PASS** |
| 22 | **R3** | `static/calm.css` PC縦スクロール維持 | 全般 | `Tier1FeatureCoverageTests.test_07_calm_css_pc_vertical_scroll_rule` | **PASS** |
| 23 | **複合** | スクレイピング障害時 店舗非消失保護 | 全般 | `Tier3CrossFeatureCombinationsTests.test_01_scraper_failure_db_snapshot_compensation_store_retention` | **PASS** |
| 24 | **複合** | ServiceWorker プリキャッシュ実体整合性 | 全般 | `Tier3CrossFeatureCombinationsTests.test_02_serviceworker_offline_cache_asset_existence` | **PASS** |
| 25 | **契約** | 404エラーハンドラ (HTML / JSON 切り分け) | 全般 | `Tier4RealWorldScenariosTests.test_02_error_page_contract` | **PASS** |

---

## 3. テストファイル構成一覧

プロジェクト内のテストスイート（計14モジュール、244テスト）：

```
tests/
├── test_clean_architecture_e2e.py    # 【新規構築】4階層 E2E テストスイート (16テスト)
├── test_app.py                       # Webアプリケーション・認証・キャッシュ制御 (99テスト)
├── test_sns_editorial_upgrade.py     # SNS自動編集室・AI生成・マルチチャネル配信 (45テスト)
├── test_e2e_compliance.py            # ブランド表記・GCP依存ゼロ・スプレッドシート整合性 (18テスト)
├── test_forecast_accuracy.py         # 60分後男女比予測・状態空間モデル (12テスト)
├── test_e2e_scenarios.py             # 運用シナリオE2E (11テスト)
├── test_challenger_verification.py   # 並行負荷・リソースリーク検証 (10テスト)
├── test_second_venues.py             # 2軒目案内・Googleマップ無料インテント (8テスト)
├── test_m2_worker_robustness.py      # ワーカー耐障害性・二重実行抑止 (7テスト)
├── test_m4_performance.py           # 高速キャッシュ・SQLite WALレスポンス (7テスト)
├── test_ui_audit_regression.py       # UI崩れ防止・アセット整合性 (6テスト)
├── test_tier5_adversarial.py         # 極限負荷・敵対的入力・フェイルオーバー (5テスト)
├── benchmark_db.py                   # DBベンチマークハーネス
└── benchmark_web_api.py              # Web API負荷ベンチマークハーネス
```

---

## 4. テスト実行方法

### 4.1 新設 E2E テストスイート単体実行
```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター
python3 -m unittest tests/test_clean_architecture_e2e.py
```

### 4.2 全テストスイート一括実行 (回帰検証)
```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター
python3 -m unittest discover -s tests -p "test_*.py"
```

---

## 5. 品質保証・リグレッション判定基準

1. **Gate 1 (M1完了判定)**:
   - `test_01_legacy_scripts_completely_removed`, `test_02_dead_images_completely_removed`, `test_03_benchmark_result_json_ignored` が **PASS** すること。
2. **Gate 2 (M2完了判定)**:
   - `test_01_x_poster_none_and_empty_headcount_robustness`
   - `test_02_sns_workflows_none_and_empty_robustness`
   - `test_03_moat_intelligence_diff_empty_and_abnormal_handling`
   - `test_05_local_db_inflight_key_consistency`
   上記4件の境界値・堅牢性テストが **PASS** すること。
3. **Gate 3 (M3完了判定)**:
   - `test_04_cache_busting_sync_across_templates_and_sw` が **PASS** すること。
   - 全244件の単体・結合・E2Eテストが 100% エラーゼロで通過（`Ran 244 tests ... OK`）すること。
