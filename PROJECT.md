# Project: 相席ナビ（AISEKI NAVI）全システム精査・不具合改修・不要コード整理

## Architecture
- **Web本体**: Flask, Gunicorn, RESTful API (`app.py`)
- **ワーカー/スケジューラ**: APScheduler, バックグラウンドプロセス (`worker.py`)
- **スクレイピング/監視**: 7系統の店舗データスクレイパー、DoHフォールバック (`monitor.py`)
- **予測エンジン**: 状態空間混雑予測・リアルタイム待ち時間推論 (`moat_intelligence.py`, `store_analysis.py`)
- **データ基盤**: SQLite WALモード (`local_db.py`, `lounge_data.db`)
- **UI/フロントエンド**: HTML/Jinja2テンプレート (`templates/`), CSS (`static/calm.css`, `portal.css`), PWA ServiceWorker (`static/sw.js`)
- **インフラ/デプロイ**: Fly.io (`fly.toml`, `Dockerfile`), GitHub (`main` ブランチ)

## Feature Inventory
| # | Feature / Issue | Description | Milestone | Source |
|---|-----------------|-------------|-----------|--------|
| 1 | ブランド表記是正 (events.py / events_cache.json) | `'brand_label': 'オリエンタル / ag'` を `'オリエンタルラウンジ / ag'` に是正 | M1 | Survey 1, 2 |
| 2 | ブランド表記是正 (price_comparison.py) | `"brand_label": "ヤタコイ（YATAKOI）"` を `"ヤタコイ"` に是正 | M1 | Survey 1, 3 |
| 3 | ブランド表記是正 (portal_guides.py / index.html) | `ヤタコイ（YATAKOI）` を `ヤタコイ` に是正 | M1 | Survey 1 |
| 4 | ブランド表記是正 (health.py / second_venues.py) | ログ・コメント・条件判定内の単体『オリエンタル』を『オリエンタルラウンジ』に是正 | M1 | Survey 1 |
| 5 | ブランド表記是正 (monitor.py / local_db.py / test_app.py) | ログ・フォールバック正規化・テストクラス名の単体表記（Oriental/OLG/YATAKOI）を是正 | M1 | Survey 3 |
| 6 | デッドコード削除 (worker.py) | 未登録・未呼び出しの3関数（_ig_carousel_job, _ig_ladies_job, _prewarm_slot_job）を削除 | M2 | Survey 2 |
| 7 | 未使用インポート削除 | app.py, column_editorial.py, monitor.py, worker.py, events.py の未使用 import 文を削除 | M2 | Survey 1 |
| 8 | 不要ファイル・ログ・一時ファイル削除 | app.log, flask.log, benchmark_*_result.json, data/history.db, Procfile の削除 | M2 | Survey 3 |
| 9 | .dockerignore 更新 | 不要ファイルをビルド対象外に設定 | M2 | Survey 3 |
| 10 | セキュリティ強化 (app.py) | Fly.io 本番環境での SESSION_COOKIE_SECURE 付与 | M3 | Survey 1 |
| 11 | 予測エンジン改善 (moat_intelligence.py) | _V2_STORE_PHASE_PRIORS 参照時の正式店舗名エイリアス解決を追加 | M3 | Survey 3 |
| 12 | DBモデルバージョン整合 (local_db.py) | テーブル定義 DEFAULT 値を最新 v2.2-statespace に更新 | M3 | Survey 3 |
| 13 | テスト環境変数WARNING抑止 (test_app.py) | テスト起動時の SECURITY NOTICE 警告を解消 | M3 | Survey 3 |
| 14 | 全体単体テスト検証 | 全247件テストのパス、リグレッションゼロの検証 | M4 | Survey 1, 2, 3 |
| 15 | 本番反映・デプロイ・稼働確認 | GitHub main コミット・プッシュ、Fly.io デプロイ、本番動作確認 | M5 | ORIGINAL_REQUEST |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | ブランド表記規約の完全是正 | events.py, price_comparison.py, portal_guides.py, index.html, health.py, second_venues.py, monitor.py, local_db.py, test_app.py, data/events_cache.json | none | DONE |
| M2 | デッドコード・不要ファイル整理 | worker.py, 未使用インポート, app.log, flask.log, benchmark_*_result.json, data/history.db, Procfile, .dockerignore | none | DONE |
| M3 | 潜在課題解消・セキュリティ・予測エンジン改善 | app.py, moat_intelligence.py, local_db.py, test_app.py | none | DONE |
| M4 | 全体単体テストおよびリグレッション検証 | tests/ 配下の全テスト実行・検証 | M1, M2, M3 | DONE |
| M5 | GitHub push & Fly.io 本番デプロイ・稼働確認 | git commit, git push origin main, flyctl deploy, https://aiseki-navi.com 疎通確認 | M4 | IN_PROGRESS |

## Code Layout
- `app.py`: Webアプリケーション、ルーティング、セッション
- `worker.py`: バックグラウンドワーカー
- `monitor.py`: スクレイピング監視
- `events.py`: イベントクローラー・データ整形
- `price_comparison.py`: 料金比較データロジック
- `portal_guides.py`: ポータルガイドFAQ・コンテンツ
- `moat_intelligence.py`: 混雑予測・推論エンジン
- `local_db.py`: SQLite DBアクセス・テーブル定義
- `templates/`: HTMLテンプレート
- `static/`: CSS/JS/アセット
- `tests/`: 単体テストスイート

## Interface Contracts
- **ブランド表記規約**: 公開UI・API・ログにおいて単体『オリエンタル』『OLG』『ORIENTAL』『YATAKOI』『オリラジ』を禁止し、正式名称『オリエンタルラウンジ』『ヤタコイ』を出力。
- **CSS制約**: 未定義 `var(--...)` の使用禁止、`static/calm.css` の `html` に `overflow` 系禁止。
- **静的ファイル**: 今回はCSS/JSアセットは変更しないため、既存キャッシュバスティング番号（`?v=`）および `static/sw.js` バージョンを維持。
