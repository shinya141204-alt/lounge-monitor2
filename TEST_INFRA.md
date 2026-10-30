# TEST_INFRA — 相席ナビ (AISEKI NAVI) 包括的テストインフラ仕様書

## 1. 概要 (Overview)

本ドキュメントは、『相席ナビ (AISEKI NAVI)』システムの品質保証、不具合防止、高可用性、および長期安定運用を担保するための包括的テストインフラおよび4層テストピラミッド（Tier 1〜Tier 4）の構造・実行方法・検証基準を定義する。

テストスイートは、Flask Webサーバー、スクレイピング Worker、データアクセス層（Redis/PostgreSQL/SQLite/Google Sheets）、SNS連携・自動記事配信パイプライン、およびフロントエンド UI を網羅し、Python 標準の `unittest` フレームワークに基づいて実行される。

---

## 2. 4層テスト構成 (4-Tier Test Architecture)

相席ナビのテスト基盤は、以下の4層で構成される：

```
+-------------------------------------------------------------+
| Tier 4: 実運用シナリオ (E2E Operational Scenarios)           |
| - ユーザー巡回・コラム閲覧・AIコンシェルジュ・ヘルス監視統合   |
+-------------------------------------------------------------+
| Tier 3: 結合・複合機能 (Integration & Contract Compliance)  |
| - ブランド表記自動走査 / GCP非依存性 / Google Sheets 5列構造 |
| - Web ↔ Worker 連携 / DB・キャッシュ整合性                   |
+-------------------------------------------------------------+
| Tier 2: 境界値・異常系 (Edge Cases & Resilience)            |
| - API/Web統一エラーハンドリング (404/500/400 JSON/HTML)      |
| - 外部APIタイムアウト・ネットワーク障害・フォールバック     |
| - パース例外防護 (None, 非数値, 欠落キー)                   |
+-------------------------------------------------------------+
| Tier 1: 機能網羅 (Unit & Direct Route Coverage)             |
| - 全ルート HTTP 200 / 店舗マスタ整合性 / 集計・重み計算     |
| - 投票・レートリミット / 記事スケジューリング                |
+-------------------------------------------------------------+
```

### 2.1 Tier 1: 機能網羅 (Unit & Direct Route Coverage)
- **目的**: システムの各単体機能、マスタデータ整合性、主要な公開エンドポイントの基本動作を高速に検証する。
- **対象**:
  - `tests/test_app.py` 内の基本テスト群（`PageRouteTests`, `StoreMasterTests`, `CrowdVoteTests`, `ScheduledArticlesTests` 等）
  - 全53店舗マスタデータの構造検証（ID, 店舗名, エリア, ブランドコード, ソース）
  - Twitter文字数重み計算（CJK 2文字、ASCII 1文字、URL 23文字）
  - 店舗別混雑スナップショット生成およびランキングソートロジック
- **実行速度目標**: < 5秒（全件）

### 2.2 Tier 2: 境界値・異常系 (Edge Cases & Resilience)
- **目的**: 外部API切断、ネットワークタイムアウト、不正データ入力、例外発生時のフォールバックおよびエラーハンドリングを検証する。
- **対象**:
  - **カスタムエラーハンドリング**:
    - Webページ（`/non-existent-page`）へのアクセス時: HTTP 404 親切HTMLUI返却
    - APIエンドポイント（`/api/non-existent-endpoint`）へのアクセス時: HTTP 404 かつ JSON形式 `{"status": "error", "error": "<message>", "code": 404}`
    - サーバー内部エラー発生時: APIは JSON 500、Webは親切HTML 500
  - **外部連携タイムアウト・障害耐性**:
    - X API タイムアウト時の画像アップロードからテキスト単体ツイートへの安全なフォールバック
    - Threads / Instagram API エラー時のリトライ・フォールバック
    - note RSS 取得失敗時（HTTP 500 / タイムアウト）の安全な空リスト返却
    - 天気予報・Open-Meteo API 遅延・障害時の非同期分離とスキップ
  - **スクレイピング耐性**:
    - `None` や非数値文字列（"満席", "調整中" 等）が混入した際のアパース例外防護（`TypeError` / `ValueError` ゼロ保証）

### 2.3 Tier 3: 結合・複合機能 (Integration & Contract Compliance)
- **目的**: プロジェクト全体の厳格な制約（ブランド表記、クラウド非依存、シート列構造）および複数コンポーネント間のインターフェース契約を検証する。
- **対象**:
  - `tests/test_e2e_compliance.py`
  - **ブランド表記ルール自動全走査 (Brand Compliance)**:
    - `templates/` 配下の公開テンプレート全ファイル、`static/` 配下のJS/CSS、公開Pythonモジュール（`x_poster.py`, `sns_workflows.py`, `ogp_generator.py` 等）を自動走査。
    - 単体「`OLG`」（単語境界 `\bOLG\b`）および単体「`オリエンタル`」（後ろにラウンジがつかない `オリエンタル(?!ラウンジ)`）の露出が完全ゼロであることを保証。
    - `format_ja_store_name('OLG 渋谷本店')` 等の呼び出しに対し、単体 OLG が排除され「オリエンタルラウンジ 渋谷本店」等の正式名称に正規化されること。
  - **Google Cloud (GCP) ゼロ依存性自動検証 (GCP Zero-Dependency)**:
    - `requirements.txt`, `Dockerfile`, `fly.toml`, およびリポジトリ内の全 `.py` ファイルを静的解析走査。
    - `google.cloud`, `google-cloud-storage`, `bigquery`, `cloud_run` 等へのインポート・依存関係が完全ゼロであることを保証。
    - Google Sheets は GCP SDK を一切使用せず `gspread` + `oauth2client` のみで利用されていることを確認。
  - **Google Sheets 5列構造厳格維持 (Google Sheets 5-Column Schema)**:
    - `'Lounge Monitor Data'`（sheet1）への書き込み行要素数が常に厳格に 5列（`[timestamp, name, men, women, source]`）であることを検証。
    - gvizクエリ読み込みが `SELECT A, B, C, D, E` の5列のみを要求すること。
    - 拡張テレメトリデータは別シート `'Moat Telemetry'` に正しく分離されていること。
  - **キャッシュ整合性・Web/Worker分離 (Turbo Architecture)**:
    - WebプロセスとWorkerプロセスの役割分離（`APP_ROLE=web` / `APP_ROLE=worker`）。
    - Redis / L1 インメモリキャッシュ / PostgreSQL / SQLite のフォールバックチェーンの整合性。

### 2.4 Tier 4: 実運用シナリオ (E2E Operational Scenarios)
- **目的**: 実際のユーザー訪問、ブラウジング、記事購読、SNS自動配信、システム死活監視のライフサイクルを模倣したシナリオ検証を行う。
- **対象**:
  - `tests/test_e2e_scenarios.py`
  - **ユーザー回遊シナリオ**:
    1. トップページ（`/`）にアクセスして混雑状況ランキングを確認
    2. ブランド別詳細ページ（`/oriental-lounge`）に遷移して店舗一覧・営業状況を確認
    3. ナレッジコラム一覧（`/columns`）および個別記事に遷移して解説を読む
    4. AIコンシェルジュ（`/concierge`）へアクセスし、おすすめ店舗相談を行う
    5. バックグラウンドで `/api/status`, `/api/health`, `/api/events`, `/api/weekly-report` をポーリング取得
  - **ヘルスチェック・自動復旧シナリオ**:
    - 各スクレイパー・データストアのステータスが `/api/health` で `healthy` / `degraded` として正確に報告されること。
    - 連続障害発生時のアラートトリガーとクールダウン、復旧検知。

---

## 3. テストファイル構成と責務

| ファイルパス | 担当 Tier | 検証内容 |
|:---|:---|:---|
| `tests/test_app.py` | Tier 1, 2, 3 | 既存テストスイート全97件（ルーティング、DB、SNS、キャッシュ、投票、バッジ等） |
| `tests/test_e2e_compliance.py` | Tier 3 | 制約・整合性検証（ブランド表記自動走査、GCPゼロ依存静的解析、Google Sheets 5列構造） |
| `tests/test_e2e_scenarios.py` | Tier 2, 4 | 実運用E2Eシナリオ（主要Web画面遷移、公開API応答構造、404/500エラーハンドリング） |

---

## 4. テスト実行方法 (Test Execution)

### 4.1 全テストスイートの実行 (推奨)
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest discover -s tests -p 'test_*.py'
```

### 4.2 制約・整合性テスト (Tier 3 Compliance) の単独実行
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest tests.test_e2e_compliance -v
```

### 4.3 実運用シナリオ & エラーハンドリング (Tier 2, 4 Scenarios) の単独実行
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest tests.test_e2e_scenarios -v
```

### 4.4 既存ユニットテスト (Tier 1 Baseline) の単独実行
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest tests.test_app -v
```

---

## 5. 合格基準 (Acceptance Criteria)

1. **テスト合格率 100%**:
   全テスト（既存97件 + 新規E2Eテストスイート）が例外・エラーなく PASS すること。
2. **高速実行性**:
   全テストがローカル環境において 15秒以内 に完了すること（外部ネットワーク遮断環境でもモック/フォールバックにより完走すること）。
3. **副作用ゼロ**:
   テスト実行によって本番データストア（Redis / PostgreSQL / Google Sheets）への意図しない汚染や破壊が行われないこと（テスト用DB/モックの利用）。
4. **制約の完全担保**:
   - ブランド表記走査テストにおいて違反行が 0件 であること。
   - GCP依存性走査テストにおいて違反ファイルが 0件 であること。
   - Google Sheets 5列構造テストにおいて要素数不一致が 0件 であること。
