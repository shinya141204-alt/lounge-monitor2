# Project: 相席ナビ (AISEKI NAVI) システム改善・高速化・安定化・本番デプロイ

## Architecture
本システムは Fly.io 東京リージョン（`nrt`）上で、2つの独立したプロセスロールとして稼働する：
- **Web プロセス (`APP_ROLE=web`)**: Gunicorn (2w × 16t, 2 CPU)。Flask HTTPサーバー、Redis/L1キャッシュからのリアルタイム混雑データ返却 (<0.5ms)、SSR、公開API、管理UI。
- **Worker プロセス (`APP_ROLE=worker`)**: APScheduler による毎分データ収集（全53店舗スクレイピング）、Redis/PostgreSQL/Google Sheets への最新データ保存、非同期SNSジョブキュー消費。
- **共有データストア**: Upstash Redis (リアルタイム状態・SNSキュー)、Fly PostgreSQL (長期ログ)、Google Sheets (生ログ5列アーカイブ)、ローカル SQLite (フォールバック)。

## Feature Inventory
Phase 0 調査で特定された全改修項目および要件の割り当て一覧（抜け漏れゼロ保証）：
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | F1.1 | `x_poster.py` の `format_ja_store_name()` から OLG プレフィックスを排除し正式名称化 | M1 | survey_constraints_sns |
| 2 | F1.2 | `templates/guide_last_train_cliff.html` の9箇所の単体 OLG を「オリエンタルラウンジ」等に置換 | M1 | survey_constraints_sns, survey_web_api |
| 3 | F1.3 | `templates/index.html`, `static/app.js`, `app.py` のUIバッジ・タブにおける OLG/オリラジ露出を適正化 | M1 | survey_web_api |
| 4 | F2.1 | Google Cloud (GCP) 依存ゼロの永続的バリデーションテスト追加 | M1 | survey_constraints_sns |
| 5 | F2.2 | Google Sheets 'Lounge Monitor Data' 5列構造維持の厳格検証テスト追加 | M1 | survey_constraints_sns |
| 6 | F2.3 | 単体「オリエンタル」「OLG」全自動走査テストの追加 | M1 | survey_constraints_sns |
| 7 | F3.1 | BUG-01: `DISABLE_SCHEDULER=1` の本番Worker誤設定解消（本番Workerとテスト環境のフラグ分離） | M2 | survey_db_worker |
| 8 | F3.2 | BUG-02: `local_db.py` の `insert_logs` 内での Open-Meteo 同期API呼び出しを完全分離・非同期化 | M2 | survey_db_worker |
| 9 | F3.3 | BUG-04: CLOVERSスクレイパーの `_parse_int_count` 適用による非数値パース例外防護 | M2 | survey_db_worker |
| 10 | F3.4 | BUG-05: XIX/ALFA/Yatakoi スクレイパーの `None` パース `TypeError` 防護（`or 0` 適用） | M2 | survey_db_worker |
| 11 | F3.5 | BUG-06: `monitor.py` の `_olg_cache` への排他ロック（スレッドセーフ化）適用 | M2 | survey_db_worker |
| 12 | F3.6 | BUG-07 / ISSUE-05: `health.py` の `_state` および `_known_unknowns` の排他ロックスコープ完全化 | M2 | survey_db_worker, survey_web_api |
| 13 | F3.7 | BUG-09: `local_db.py` / `turbo_store.py` の PostgreSQL コネクション滞留（idle in transaction）防護 | M2 | survey_db_worker |
| 14 | F4.1 | ISSUE-04: カスタムエラーハンドラー（404, 500, 400）実装（APIパス/JSONはJSON返却、Webは親切画面） | M3 | survey_web_api |
| 15 | F4.2 | ISSUE-06: `app.py` の秘密鍵・パスコード設定管理のフォールバック安全性向上 | M3 | survey_web_api |
| 16 | F5.1 | BUG-03: `get_snapshot_around` の Window関数（ROW_NUMBER OVER）最適化（5万行→53行、95%高速化） | M4 | survey_db_worker |
| 17 | F5.2 | ISSUE-01: `app.py` の `after_request` 統合による `ENABLE_PUBLIC_EDGE_CACHE=1` 完全稼働 | M4 | survey_web_api |
| 18 | F5.3 | ISSUE-07: `/concierge` および `/api/concierge` への短時間キャッシュ導入 | M4 | survey_web_api |
| 19 | F5.4 | 静的アセット直接配信: `fly.toml` への `[[statics]]` 定義追加による Fly.io プロキシ直接配信 | M4 | survey_web_api |
| 20 | F5.5 | BUG-08: SQLiteコネクション取得時の `os.stat` 頻度低減・最適化 | M4 | survey_db_worker |
| 21 | F6.1 | 既存ユニットテスト全97件の合格維持および新規テスト全件合格確認 | M5 | ORIGINAL_REQUEST |
| 22 | F6.2 | 最適化前後の処理時間・レイテンシベンチマーク計測と改善結果レポート作成 | M5 | ORIGINAL_REQUEST |
| 23 | F6.3 | E2Eテストスイート（Tiers 1-4）全件合格およびTier 5カバレッジ検証 | M5 | Project Pattern |
| 24 | F7.1 | GitHub main ブランチへのコミット・プッシュ | M6 | ORIGINAL_REQUEST |
| 25 | F7.2 | Fly.io 本番環境（`aiseki-navi-tokyo`）へのデプロイ実行 | M6 | ORIGINAL_REQUEST |
| 26 | F7.3 | 本番環境でのエンドポイント応答・稼働確認 | M6 | ORIGINAL_REQUEST |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Brand & Compliance | F1.1, F1.2, F1.3, F2.1, F2.2, F2.3 | none | DONE |
| M2 | Worker & DB Robustness | F3.1, F3.2, F3.3, F3.4, F3.5, F3.6, F3.7 | none | DONE |
| M3 | Web & API Robustness | F4.1, F4.2 | none | DONE |
| M4 | Performance & Optimization | F5.1, F5.2, F5.3, F5.4, F5.5 | M2, M3 | DONE |
| M5 | Verification & Benchmark | F6.1, F6.2, F6.3, E2E Tests Pass | M1, M2, M3, M4, TEST_READY | DONE |
| M6 | Production Deployment | F7.1, F7.2, F7.3 | M5 | DONE |

## Interface Contracts
### Web ↔ Worker (Redis / DB)
- `aiseki:live_snapshot`: 最新53店舗スナップショットのJSON文字列（キー: 店舗名、値: dict(men, women, updated, etc.)）
- `aiseki:sns_queue`: 非同期SNS投稿ジョブのリスト（LPUSH/RPOP）
- Google Sheets 'Lounge Monitor Data': 厳格に5列 [timestamp, name, men, women, source]

### Web API Error Responses
- APIエンドポイント（`/api/*`）でのエラーは必ず JSON 形式: `{"status": "error", "error": "<message>", "code": <int>}` を HTTP ステータスコード（400, 404, 500等）と共に返却。

### Brand Display Rule
- 公開UI・記事・SNS出力において、単体の「オリエンタル」や「OLG」「オリラジ」を出力しない。「オリエンタルラウンジ」と表記する。

## Code Layout
- `app.py`: Flask Webサーバー、エンドポイント、SSRレンダリング、エラーハンドラー、キャッシュヘッダー
- `worker.py`: データ収集・定期ジョブワーカー
- `monitor.py`: スクレイピングエンジン、キャッシュ
- `local_db.py`: SQLite / PostgreSQL クエリ、データアクセス層
- `health.py`: 内部状態・死活監視
- `x_poster.py`, `sns_editorial.py`: SNS投稿・店舗名フォーマット
- `templates/`: Jinja2 テンプレート
- `static/`: フロントエンド JS/CSS
- `fly.toml`: Fly.io インフラ設定
- `tests/`: ユニットテスト・E2Eテストスイート
