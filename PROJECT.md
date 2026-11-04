# Project: 相席ナビ（AISEKI NAVI）全システム現状評価・収益化監査および緊急改善

## Architecture
- **Web本体**: Flask, Gunicorn, RESTful API (`app.py`)
- **ワーカー/スケジューラ**: APScheduler, バックグラウンドプロセス (`worker.py`)
- **スクレイピング/監視**: 7系統53店舗データスクレイパー、DoHフォールバック (`monitor.py`, `health.py`)
- **予測エンジン**: 状態空間混雑予測・リアルタイム待ち時間推論 (`moat_intelligence.py`, `store_analysis.py`)
- **データ基盤**: Fly Postgres + Upstash Redis, SQLite WALモード (`local_db.py`, `turbo_store.py`)
- **UI/フロントエンド**: HTML/Jinja2テンプレート (`templates/`), CSS (`static/calm.css`, `portal.css`), PWA ServiceWorker (`static/sw.js`), JS (`static/app.js`, `static/affiliate_config.js`)
- **アフィリエイト/収益化**: `affiliate_manager.py`, `pdca_engine.py`, `/link/<item_id>`
- **インフラ/デプロイ**: Fly.io (`fly.toml`, `Dockerfile`), GitHub (`main` ブランチ)

## Feature Inventory
| # | Issue / Improvement | Description | Milestone | Source |
|---|---------------------|-------------|-----------|--------|
| 1 | 総合評価・収益化改善レポート作成 | UI/UX、SEO・集客、収益化導線、運用体制の現状分析とロードマップ（REPORT_MONETIZATION_AUDIT.md） | M1 | Survey UI, SEO, Monetization |
| 2 | 料金比較キャッシュキー動的化 | `price_comparison_page`（`app.py:4600`）のキャッシュキーを `f"price_comp_{selected_area}"` に修正 | M2 | Survey SEO |
| 3 | アフィリエイト転送URLのクロール遮断 | `robots.txt` に `Disallow: /link/` 追加、`/link/<id>` に `X-Robots-Tag: noindex, nofollow` 付与 | M2 | Survey SEO |
| 4 | 女性向けアフィリエイトURLミスマッチ是正 | `affiliate_manager.py` / `app.py` の女性向けスロットデフォルトURLを男性用から正常な女性向け/公式へ分離 | M2 | Survey Monetization |
| 5 | grooming.html の景表法PR表記＆A8計測ピクセル追加 | 身だしなみガイド各カードへのPRバッジ・注記明記およびA8.net `0.gif` 計測用 `<img>` 設置 | M2 | Survey Monetization |
| 6 | トップ画面おすすめモーダルの景表法PR表記明記 | `static/app.js` の `renderGuideModal()` 内カードにPRバッジを追加 | M2 | Survey Monetization |
| 7 | 店舗詳細ページの LocalBusiness 構造化データ追加 | `templates/store_detail.html` に `LocalBusiness` / `BarOrPub` `openingHoursSpecification` JSON-LD を追加 | M2 | Survey SEO, Challenger 2 |
| 8 | キャッシュバスティング同期更新 | `static/app.js` 変更に伴う `static/sw.js`、テンプレート、回帰テストのバージョン番号同期 | M2 | 制約事項 R4 |
| 9 | 単体テストスイートおよびUI監査実行 | 全テスト（261件）および `audit_anti_ai_ui.py` の全通過確認 | M3 | 要件 R5 |
| 10 | Gitコミット・プッシュ・Fly.io本番デプロイ・稼働確認 | GitHub main 反映、Fly.io 本番デプロイ、`https://aiseki-navi.com` 正常稼働確認 | M4 | 要件 R5 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | 総合評価・収益化改善提案レポートの作成 | `REPORT_MONETIZATION_AUDIT.md` | none | DONE |
| M2 | 即時対応可能な緊急改善の実装・改修 | `app.py`, `affiliate_manager.py`, `templates/grooming.html`, `templates/store_detail.html`, `static/app.js`, `static/sw.js`, `store_hours.py` 等 | M1 | DONE |
| M3 | 単体テストスイート・UI監査・規約検証 | `tests/` 全261テスト、`audit_anti_ai_ui.py`、Challenger実証テスト | M2 | DONE |
| M4 | GitHub push & Fly.io 本番デプロイ・稼働確認 | `git commit`, `git push origin main`, `flyctl deploy`, `https://aiseki-navi.com` 疎通確認 | M3 | IN_PROGRESS |

## Code Layout
- `app.py`: Webアプリケーション、ルーティング、キャッシュ、リダイレクト
- `affiliate_manager.py`: アフィリエイト設定・リンク管理
- `store_hours.py`: 営業時間スケジュール、`OpeningHoursSpecification` 生成ロジック
- `templates/grooming.html`: 身だしなみ・事前準備ガイド（景表法PR表記・A8ピクセル）
- `templates/store_detail.html`: 店舗詳細ページ・`LocalBusiness` 構造化データ
- `static/app.js`: クライアントUI制御・モーダル描画（PRバッジ）
- `static/sw.js`: ServiceWorkerキャッシュ（v86）
- `REPORT_MONETIZATION_AUDIT.md`: 総合評価および収益化改善ロードマップ報告書
- `tests/test_emergency_improvements.py`: 緊急改善回帰テストスイート

## Interface Contracts
- **ブランド表記規約**: 単体の『オリエンタル』『OLG』『ORIENTAL』『YATAKOI』『オリラジ』を禁止し、正式名称『オリエンタルラウンジ』『ヤタコイ』を使用。
- **CSS制約**: 未定義 `var(--...)` の使用禁止、`static/calm.css` の `html` に `overflow` 系禁止。
- **静的アセット**: JS/CSSを変更した場合は、全テンプレート、`static/sw.js`、テストコードのキャッシュバスティング番号（`?v=`）を漏れなく同期更新。
