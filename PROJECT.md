# Project: 相席ナビ (AISEKI NAVI) SNS AI機能強化 & 2軒目案内UI/UX刷新 & 本番デプロイ

## Architecture
本システムは Fly.io 東京リージョン（`nrt`）上で、2つの独立したプロセスロールとして稼働する：
- **Web プロセス (`APP_ROLE=web`)**: Gunicorn (2w × 16t, 2 CPU)。Flask HTTPサーバー、Redis/L1キャッシュからのリアルタイム混雑データ返却 (<0.5ms)、SSR、公開API、スタッフ用生成UI、店舗詳細モーダル。
- **Worker プロセス (`APP_ROLE=worker`)**: APScheduler による毎分データ収集（全53店舗スクレイピング）、Redis/PostgreSQL/Google Sheets への最新データ保存、非同期SNSジョブキュー消費。
- **データストア**: Upstash Redis (リアルタイム状態・SNSキュー)、Fly PostgreSQL (長期ログ)、Google Sheets (生ログ5列アーカイブ)、ローカル SQLite (フォールバック)、静的マスター JSON (`store_master.json`, `second_venues.json`)。

## Feature Inventory
Phase 0 調査で特定された全改修項目および要件の割り当て一覧（抜け漏れゼロ保証）：
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | F1.1 | 2軒目静的マスター策定: 主要エリア（渋谷, 新宿, 恵比寿, 銀座, 横浜, 道頓堀, 梅田, 栄, 博多等）の厳選スポット（夜カフェ, バー, シーシャ等）およびシチュエーション別Tips・切り出しフレーズ・タイミング定義 (`static/second_venues.json`) | M1 | explorer_survey_second_party |
| 2 | F1.2 | 2軒目バックエンド連携: マスター読み込み、エリアマッチング、未登録店舗向けスマートフォールバック（動的マップ検索クエリ＋汎用Tips）を提供する `second_venues.py` 実装 | M1 | explorer_survey_second_party |
| 3 | F1.3 | 店舗詳細モーダルUI/UX刷新: カテゴリタブ（夜カフェ・バー・シーシャ）、厳選スポットカード（徒歩分数、予算、タグ、GoogleマップインテントURL、店名コピー）、シチュエーション別Tipsカード、ワンタップコピー、LINE友達共有の実装 (`templates/index.html`, `static/app.js`, `static/style.css`) | M1 | explorer_survey_second_party |
| 4 | F1.4 | 店舗詳細個別ページ連携: `templates/store_detail.html` の周辺スポット枠を静的マスターと連動し、実用的なスポットカード・フレーズを表示 | M1 | explorer_survey_second_party |
| 5 | F2.1 | note チャンネル新設: `sns_editorial.py` に `note` を追加し、リアルタイム混雑データ・夜の動向を反映したnoteショートエッセイ・つぶやき下書きの自動生成ロジックを実装 | M2 | explorer_survey_sns |
| 6 | F2.2 | シチュエーション特化型クイック切り口セレクター: 2軒目移動・夜カフェハシゴ、女子待ちゼロ即乾杯、雨天・地下直結、終電崖逆転等の切り口に応じたプロンプト・生成パイプライン追加 | M2 | explorer_survey_sns |
| 7 | F2.3 | スタッフ編集部UI強化: `/staff/sns-editorial` に note タブ追加、シチュエーションセレクター、文面のインライン編集（Editable Textarea）、リアルタイム文字数・Twitterウェイトカウンター、Web Intent（X/Threads）直接投稿ボタン、アイキャッチ画像プロンプト帳を実装 (`templates/staff_sns_editorial.html`) | M2 | explorer_survey_sns |
| 8 | F2.4 | クイックSNSモーダル連携: `templates/index.html` のスタッフ用SNSモーダル（`#sns-modal`）にシチュエーション別フレーズ・新切り口を連動 | M2 | explorer_survey_sns |
| 9 | F3.1 | 新規テストスイート追加: 2軒目案内機能テスト (`tests/test_second_venues.py`)、SNS AI強化テスト (`tests/test_sns_editorial_upgrade.py`) の実装 | M3 | explorer_survey_constraints |
| 10 | F3.2 | 制約検査の拡充: フロントエンドへのGCP有料Maps APIキー漏洩防止テスト、2軒目マスターのGCP非依存検証、全テキストでのブランド表記ルール（『オリエンタルラウンジ』厳守）テストを `tests/test_e2e_compliance.py` に追加 | M3 | explorer_survey_constraints |
| 11 | F3.3 | 既存全143テストおよび新設テストの100%合格、Challengerによる負荷・整合性検証、Forensic Auditorによる完全性監査 | M3 | explorer_survey_constraints |
| 12 | F4.1 | GitHub main ブランチへのコミット・プッシュ | M4 | ORIGINAL_REQUEST |
| 13 | F4.2 | Fly.io 本番環境（`aiseki-navi-tokyo`）へのデプロイ実行 (`/Users/kuwanoshinya/.fly/bin/flyctl deploy`) | M4 | ORIGINAL_REQUEST |
| 14 | F4.3 | 本番環境 (`https://aiseki-navi.com`) でのヘルスチェックおよび新機能（2軒目案内UI、SNS編集部UI、API等）の稼働確認 | M4 | ORIGINAL_REQUEST |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | 2nd Party Venue Guide UI/UX | F1.1, F1.2, F1.3, F1.4 | none | DONE |
| M2 | SNS AI & Workflow Upgrade | F2.1, F2.2, F2.3, F2.4 | none | DONE |
| M3 | Comprehensive Testing & Verification | F3.1, F3.2, F3.3 | M1, M2 | DONE |
| M4 | Production Deployment & Healthcheck | F4.1, F4.2, F4.3 | M3 | PLANNED |

## Interface Contracts
### 2nd Party Master Data (`static/second_venues.json`)
```json
{
  "venues_by_area": {
    "shibuya": [
      {
        "id": "shibuya-cafe-01",
        "name": "宇田川カフェ suite",
        "category": "cafe",
        "category_label": "夜カフェ",
        "walk_minutes": 3,
        "budget": "¥1,000〜¥2,000",
        "features": ["ソファ席あり", "深夜2時まで営業", "落ち着いた照明"],
        "map_query": "宇田川カフェ suite 渋谷",
        "comment": "相席ラウンジから徒歩3分。落ち着いた薄暗い照明とゆったりソファで自然と距離が縮まる定番。"
      }
    ]
  },
  "situation_tips": [
    {
      "id": "phrase_cafe",
      "situation": "甘いもの・落ち着いた場所へ誘う場合",
      "tag": "王道・高成功率",
      "phrase": "ここ音楽で少し声張らないとだから、30分だけ近くの静かな夜カフェで美味しいスイーツ食べない？",
      "point": "「30分だけ」と出口を示し、お酒ではなく甘いもの・静かさを理由にすることで警戒心を解除。"
    }
  ],
  "timing_rules": [
    {
      "rule": "40〜50分ルール",
      "desc": "話題が出尽くす前、会話が一番盛り上がっているピーク時に切り出すのが鉄則。"
    },
    {
      "rule": "23:10終電前ルール",
      "desc": "終電（23:30前後）の20分前までに2軒目へ入店するか、無理せず解散して後日デートを確約する。"
    }
  ]
}
```

### Free Google Maps Intent URL Contract
- 全てのマップリンクはGoogle公式の無料WebインテントURLを採用（GCP有料API呼び出しは完全ゼロ）：
  `https://www.google.com/maps/search/?api=1&query={encodeURIComponent(venue.map_query)}`

### Brand Display Rule
- 公開UI・記事・SNS出力において、単体の「オリエンタル」や「OLG」「オリラジ」を出力しない。「オリエンタルラウンジ」と表記する。

## Code Layout
- `static/second_venues.json`: 2軒目厳選スポット＆Tips静的マスター
- `second_venues.py`: 2軒目マスターローダー・フォールバックヘルパー
- `templates/index.html`: 店舗詳細モーダル（2軒目セクション刷新、SNSクイックモーダル）
- `static/app.js`: 2軒目モーダルタブ・カード・Tipsレンダリング、クリップボードコピー
- `static/style.css`: 2軒目UI用スタイル定義
- `templates/store_detail.html`: 個別店舗詳細ページ
- `sns_editorial.py`: SNS AI合議エンジン（noteチャンネル新設、シチュエーションセレクター）
- `templates/staff_sns_editorial.html`: スタッフ用SNS編集部UI（noteタブ、インライン編集、文字数カウンター）
- `tests/test_second_venues.py`: 2軒目機能テスト
- `tests/test_sns_editorial_upgrade.py`: SNS AI強化テスト
- `tests/test_e2e_compliance.py`: 制約検証テスト
