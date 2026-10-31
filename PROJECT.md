# Project: 相席ナビ (AISEKI NAVI) 全システム不具合改修・クリーンアップ・本番デプロイ

## Architecture
相席ナビは Fly.io 東京リージョン（`nrt`）上で2つのプロセスグループとして稼働する高可用性システムである：
- **Web プロセス (`APP_ROLE=web`)**: Gunicorn (2w × 16t, 2 CPU)。Flask Webアプリケーション (`app.py`)。リアルタイム混雑状況配信、公開API、店舗詳細モーダル、2軒目案内、スタッフ管理画面、静的アセット配信。
- **Worker プロセス (`APP_ROLE=worker`)**: APScheduler によるバックグラウンドタスク (`worker.py`)。全8系統（53〜63店舗）の毎分スクレイピング (`monitor.py`)、PostgreSQL / SQLite / Google Sheets へのデータ永続化、Upstash Redis による L1/L2 共有キャッシュおよび非同期SNSジョブキュー消費。
- **データストア / 外部連携**: Upstash Redis (東京), Fly PostgreSQL (東京), Google Sheets (5列生ログ + Moat Telemetry), ローカル SQLite (WALモードフォールバック), Open-Meteo 気象API (無料枠), Google Maps Web Intent (無料インテントURL, GCP有料API依存ゼロ)。

## Feature Inventory
Phase 0 調査で特定された全改修項目および要件の割り当て一覧（抜け漏れゼロ保証）：
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | F1.1 | `scripts/legacy/` 配下の全21スクリプト（非推奨・未参照）の完全削除 | M1 | survey_r2_worker_db / survey_r2_sns_ai_tools |
| 2 | F1.2 | `monitor.py` 内の未参照関数（`_run_with_timeout`, `find_store_with_max_women`）の安全な削除 | M1 | survey_r2_worker_db |
| 3 | F1.3 | `static/` 内の未参照デッド画像（`thumb-note-eyecatch*.jpg/webp` 4ファイル、計166KB）の削除 | M1 | survey_r2_web_api |
| 4 | F1.4 | ベンチマーク実行動的JSONファイル（`benchmark_*_result.json`）のGit追跡解除および `.gitignore` 追加 | M1 | survey_r2_worker_db / survey_r2_sns_ai_tools |
| 5 | F2.1 | `x_poster.py` における `None` 値起因の `TypeError` 防御改修（`int(s.get("women") or 0)`） | M2 | survey_r2_sns_ai_tools |
| 6 | F2.2 | `sns_workflows.py` における `None` 値起因の比較・加算エラー防御改修 | M2 | survey_r2_sns_ai_tools |
| 7 | F2.3 | `moat_intelligence.py` における空文字列パラメータ型キャスト例外（`ValueError`）防御改修 | M2 | survey_r2_sns_ai_tools |
| 8 | F2.4 | `local_db.py` のインフライトキー不整合（`_active_db_key()` 統一）および GViz 通信例外ハンドリング強化 | M2 | survey_r2_worker_db |
| 9 | F2.5 | `monitor.py` の Fly.io IPv6 環境におけるグローバル IPv4 強制の上書きスキップ（`logger.py` と統一） | M2 | survey_r2_worker_db |
| 10 | F2.6 | `worker.py` の SIGTERM 受信時 graceful shutdown 待機時間短縮（15秒 → 2秒） | M2 | survey_r2_worker_db |
| 11 | F2.7 | `templates/index.html` のサムネイルフォールバック画像指定修正（フルサイズ218KB → サムネイル62KB） | M2 | survey_r2_web_api |
| 12 | F2.8 | `tests/test_challenger_verification.py` の未クローズレスポンスによる `ResourceWarning` 解消 | M2 | survey_r2_web_api / survey_r2_sns_ai_tools |
| 13 | F3.1 | `style.css` キャッシュバスティングバージョン（`?v=20261030_6`）の全18テンプレートおよび `static/sw.js` 間での完全同期 | M3 | survey_r2_web_api |
| 14 | F3.2 | 新規検証・リグレッションテストの追加（キャッシュバスティング同期、削除ファイル非存在、NoneType防御、型安全性） | M3 | survey_r2_web_api / survey_r2_worker_db / survey_r2_sns_ai_tools |
| 15 | F3.3 | 既存全228件および新規テストの100%全件合格確認、および Forensic Auditor による完全性監査 | M3 | ORIGINAL_REQUEST |
| 16 | F4.1 | 変更内容の GitHub `main` ブランチへのコミットおよびプッシュ | M4 | ORIGINAL_REQUEST |
| 17 | F4.2 | Fly.io 本番環境（`aiseki-navi-tokyo`）へのデプロイ実行 (`flyctl deploy --remote-only`) | M4 | ORIGINAL_REQUEST |
| 18 | F4.3 | 本番環境 (`https://aiseki-navi.com`) での正常稼働確認およびヘルスチェック検証 | M4 | ORIGINAL_REQUEST |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Dead Code & Asset Cleanup | F1.1, F1.2, F1.3, F1.4 | none | DONE |
| M2 | Subsystem Bug Fixes & Hardening | F2.1, F2.2, F2.3, F2.4, F2.5, F2.6, F2.7, F2.8 | M1 | DONE |
| M3 | Cache-Busting Sync & Test Expansion | F3.1, F3.2, F3.3 | M2 | DONE |
| M4 | Production Deploy & Live Healthcheck | F4.1, F4.2, F4.3 | M3 | IN_PROGRESS |

## Interface Contracts
### Cache-Busting Version Contract
- `style.css`: 最新の `/static/style.css?v=20261030_6` に全テンプレートおよび `static/sw.js`（`CORE_ASSETS`）で統一。
- `calm.css`: `/static/calm.css?v=22`
- `portal.css`: `/static/portal.css?v=10`
- `app.js`: `/static/app.js?v=20261101_1`
- `sw.js`: `/sw.js?v=73`

### Operational Constraints & Brand Rules
- GCP有料SDK（`google.cloud`）は一切使用しない。
- 公開UI・SNS投稿では『オリエンタル』等の略称を用いず、正式名称『オリエンタルラウンジ』と表記する。
- `static/calm.css` の `html` セレクタには `overflow` / `overflow-x` / `overflow-y` を指定しない。

## Code Layout
- `scripts/legacy/*`: 【削除対象】全21ファイル
- `static/thumb-note-eyecatch*`: 【削除対象】4画像ファイル
- `monitor.py`: スクレイピングパイプライン（未参照関数削除、IPv4ガード）
- `worker.py`: バックグラウンドワーカー（シャットダウンタイムアウト短縮）
- `x_poster.py`, `sns_workflows.py`: SNS投稿ロジック（NoneType防御）
- `moat_intelligence.py`: 予測・待ち時間推論（空文字キャスト防御）
- `local_db.py`: DB永続化・GViz通信（キー統一、例外ハンドリング保護）
- `templates/*.html`: 全HTMLテンプレート（`style.css` バージョン同期、サムネイル修正）
- `static/sw.js`: ServiceWorker（`CORE_ASSETS` バージョン同期）
- `tests/*`: 単体・結合・検証テストスイート（全228件+α）
- `.gitignore`: ベンチマークJSON追跡除外
