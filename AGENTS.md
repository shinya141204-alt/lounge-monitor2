# 相席ナビ (AISEKI NAVI) 共通開発・編集ルール (`AGENTS.md` / `CLAUDE.md`)

本ドキュメントは **Antigravity** および **Claude Code** の双方が常時読み込む共通ルールブック（上限 `24,000 bytes`）です。どちらのエージェントで作業する場合も、以下の規約を最優先で遵守してください。

## 1. 基本コミュニケーション・ファクト厳守規約
- ユーザーへの返答・レポート・コミット説明・アーティファクトは **すべて日本語** で行うこと。
- **推測値・ダミー数値の完全禁止**: 本番UI・コラム本文・一次統計CSV/JSON・白書において、根拠のない推測値やフォールバック固定値（例：未算出時の仮の予測誤差など）を絶対に出力しないこと。データ不足時は `null`、`data_status: "insufficient"`、または「検証ログ蓄積中」と正直に表記する。

## 2. システム構成・ディレクトリ地図
- **本番インフラ**: `https://aiseki-navi.com`（Fly.io 東京リージョン `nrt`）
  - アプリ本体: `aiseki-navi-tokyo`（`web` + `worker` の2プロセス構成）
  - データベース: Fly Postgres `aiseki-navi-tokyo-db`（**512MB VM**：メモリ上限を意識し大量行の一括 `fetchall()` を避けること）＋ Redis（高速キャッシュ）＋ SQLite WAL（ローカル/フォールバック）＋ Google Sheets（バックアップ同期）
- **コアサーバー & Blueprint 分割**:
  - `app.py`: Flask 初期化・認証・セキュリティヘッダー・`/`（SSR軽量描画）・`/staff`・コア `/api/status`・`/api/vote`・`/api/watch-intent`・`/link/<item_id>`
  - `routes/portal_bp.py`: ブランド別・`/store/<slug>`・`/areas`・`/columns`・`/stats`・`/stats/monthly.csv`・`/stats/year-end-2026`・`/price-comparison`・`/for-women`・`/concierge`・`/grooming`・`/about`・`/contact`・全 `/guide/*`
  - `routes/seo_feeds_bp.py`: `/robots.txt`・`/sitemap.xml`・`/llms.txt`・`/llms-full.txt`・`/api/llms-facts.json`（`schema_version="2.1"`）・`/ads.txt`
  - `routes/staff_bp.py`: スタッフ・マスター管理APIおよび IG スライドキャッシュ
  - `routes/sns_jobs_bp.py`: 動的 OGP / リール動画配信、非同期 SNS ジョブキュー、全 `/api/trigger-*`
  - `routes/push_line_bp.py` / `push_watch.py`: Web Push 条件ウォッチ購読（RFC 8292 VAPID ES256 + RFC 8291 `aes128gcm`）＆公式 LINE Messaging API 0円自動応答 Webhook
  - `routes/_bridge.py`: `sys.modules['app']` 動的プロキシ（既存テストの `mock.patch.object(app_module, ...)` 互換維持用）
- **独自データモート・分析モジュール**:
  - `local_db.py` / `turbo_store.py`: 生ログ90日保持＋1時間粒度永久ロールアップ（`store_hourly_rollups`）、日次文脈（`day_context_logs`）、現地投票（`crowd_votes`）、条件ウォッチ（`store_watch_intents`）、60分後予測精度検証
  - `arena_calendar.py` / `events.py` / `moat_intelligence.py`: 全国主要ドーム・アリーナ公演収集（`arena_schedule_events`）、JR主要路線遅延×来店サージ相関、公式イベント差分履歴（`store_event_history`）、リアルタイム60分予測＆待ち時間推論
  - `price_comparison.py`: 国内全店舗の料金体系（`STORE_PRICE_TABLE`）＋ 別業態横断比較（`ADJACENT_FORMAT_SPECS`：マッチングアプリ・街コン・審査制デート等との時間単価・当日性・会話保証比較）
  - `store_master.py` / `store_hours.py` / `store_insights.py` / `data_audit.py`: 店舗マスタ・営業時間判定・店舗別実測分析・ドリフト監査
  - `affiliate_manager.py` / `analytics.py` / `pdca_engine.py`: アフィリエイト枠管理・アクセス計測・自律グロースPDCA参謀
  - `logger.py`: `google-auth`（8秒タイムアウト `_BoundedGoogleAuthRequest`）による Google Sheets 同期（`oauth2client` は使用禁止）

## 3. ブランド正式名称・著者クレジット・業務分離ルール（厳守）
公開HTML・CSV・JSON・SNS投稿文・コラム・note原稿において、以下の略称・旧称は **一切使用禁止**：
- **NG表記**: 単体の `オリエンタル`（例: `オリエンタル新宿`）、内部コード `OLG`、英語表記 `YATAKOI`、旧略称 `オリラジ`
- **OK（正式表記）**: **`オリエンタルラウンジ`**、**`ag（アグ）`**、**`JIS（ジス）`**、**`ASOBIBAR（アソビバー）`**、**`ヤタコイ`**（各記事・ページの本文初出時は `ag（アグ）`・`JIS（ジス）`・`ASOBIBAR（アソビバー）` とカナ併記すること）
- **運営・著者クレジット統一（プライバシー＆独立性保護）**:
  - 運営主体は **`相席ナビ編集部`**、編集長・執筆責任者は **`くわもん（相席ナビ編集長）`**（JSON-LD `Person.name = "くわもん"`）で統一する。
  - 個人本名ローマ字表記や「店舗現場での勤務経験」は公開HTML・記事・SNSに絶対に記載しないこと（専門性は「Webエンジニアリング・時系列統計データ分析」として記載する）。
  - グローバル環境に存在する店舗内部業務系スキル（`lounge-monthly`・`lounge-pdca`・`kds-update`）の社内数値や文脈を、公開メディア『相席ナビ』に一切混入させないこと。

## 4. セキュリティ・本番アクセス計測汚染防止ルール（厳守）
1. **機密情報の保護**:
   - `SECRET_KEY`・`STAFF_PASSCODE`・`MASTER_PASSCODE`・`ADMIN_API_SECRET`・`VAPID_PRIVATE_KEY_PEM`・`LINE_CHANNEL_SECRET`・`LINE_CHANNEL_ACCESS_TOKEN` 等の実値をコード・Markdown・ログ・チャットに出力しないこと。
   - `?passcode=` によるURLクエリログインは廃止済みのため復活させないこと（管理APIはセッションまたは `X-Admin-Secret` ヘッダーを使用）。
2. **本番アクセス・アフィリエイト計測の自己汚染防止（全エンドポイント適用）**:
   - 本番 `https://aiseki-navi.com` の **すべてのURL**（`/link/<id>` だけでなく `/api/status`・`/api/llms-facts.json`・`/stats/*`・`/guide/*` 等すべて）へ疎通確認や監査スクリプトでアクセスする際は、**必ず `-A "curl/8.0"` 系の明示ボット/監査UA ＋ `?staff_test=1` を付与** すること。
   - スクリプト内で実ブラウザ（Chrome / Safari 等）の User-Agent を偽装して本番を叩くことは禁止（`analytics.py` の人間PVやCTRが汚染されるため）。
   - `robots.txt` の全ボット個別グループの `Disallow: /link/` および `_is_bot_request()` の AI フェッチャー除外設定を弱めないこと。
   - 個別データコラム（`templates/guide_*.html`・`scheduled_articles.py`）および一次統計ページ（`templates/stats_*.html`）には `/link/...` を直接貼らず、`/grooming`・`/for-women` 等の内部クッションへの自然な2ステップ導線にとどめること。

## 5. フロントエンド・UI/UX・キャッシュバスティング完全同期ルール
- **脱AIデザイン基準**: 見出し・ボタンへの装飾絵文字乱用、派手なネオングラデーション、3重カード入れ子、未定義 `var(--...)` の使用を禁止。ダーク（`#111113`）とライト（`body.theme-light` `#f6f6f7`）双方で WCAG コントラスト基準（`>= 4.5:1`、主要文字 `>= 5.5:1`）を満たし、人数・比率・料金・統計値には `font-variant-numeric: tabular-nums` を適用すること。`static/calm.css` の `html` 要素には `overflow` を指定しないこと。
- **キャッシュバスティング完全同期**: `static/calm.css`・`static/portal.css`・`static/style.css`・`static/app.js`・`static/affiliate_config.js`・`static/sw.js` のいずれかを更新した際は、以下を **すべて同時に同期更新** すること（特定バージョン文字列を `SKILL.md` にハードコードせず、必ず現行正本を参照して揃える）：
  1. `templates/index.html` および全公開テンプレート（`templates/*.html`）内の該当 `?v=...` クエリ文字列
  2. `templates/index.html` と `templates/_nav_loader.html` 両方の `/sw.js?v=...`
  3. `static/sw.js` 内の `CACHE_NAME`（`aiseki-navi-v...`）および `CORE_ASSETS` 配列内の `?v=...`
  4. `tests/test_ui_audit_regression.py`（および関連テスト）の期待バージョン定数

## 6. テスト・監査・本番デプロイ標準手順
コードやテンプレートを変更した際は、本番汚染防止フラグ（`DISABLE_SHEETS=1 DISABLE_SCHEDULER=1`）を付けて以下を実行し、全件合格を確認してからデプロイすること（または `/ops-sentinel` の `--pre-deploy` で一括実行可能）：

```bash
# 1. アフィリエイト整合性・薬機法・直 /link/ 禁止・ブランド表記監査
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --audit

# 2. UIコントラスト・未定義CSS変数・キャッシュ同期・スクロール安全性監査
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py

# 3. 店舗マスタ・営業時間・料金・別業態比較定義の整合性監査
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/store-audit/scripts/run_full_store_audit.py

# 4. 全単体テストスイート実行
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests

# 5. GitHub 反映 ＆ Fly.io 東京本番デプロイ ＆ 非汚染スモーク確認
git push origin main
/Users/kuwanoshinya/.fly/bin/flyctl deploy . --config fly.toml --remote-only -a aiseki-navi-tokyo --ha=false
python3 .agents/skills/ops-sentinel/scripts/run_ops_sentinel.py --live
```

## 7. 専用スキル一覧（全8スキル）＆ Claude Fable 5.1 運用基準
詳細なワークフローは各 `.agents/skills/<name>/SKILL.md` を参照すること：
1. **`article` (`/article`)**: 90日モート＆本番 `llms-facts` 実測抽出 → 16:9図解アイキャッチ → 4層因果コラムHTML → Fable/Opus推敲 → note独立エッセイ生成
2. **`editorial-ui-ux` (`/ui`)**: Mobbin実在UI参照 × クリーン・ミニマルUX改修 × 未定義CSS変数・キャッシュ完全同期・Playwright回帰監査
3. **`affiliate` (`/affiliate`)**: ASPタグ解析・薬機法＆直 `/link/` 検閲・コード4箇所＋ガイド同期更新・非汚染リダイレクト確認
4. **`store-audit` (`/store-audit`)**: 店舗マスタ・営業時間（14日ロールアップドリフト含む）・料金表・別業態比較定義の一括点検
5. **`claude-review` (`/claude-review`)**: Antigravity × Claude Code 連携（ファイル自動インライン添付＆`stdin` 高速渡し対応。日常レビューは `claude-opus-5-5`、切り札4領域 ①`--mode strategy`［全体戦略］・②`--mode audit`［数理・最終セキュリティ監査］・③`--mode flagship`［看板コラム・note徹底推敲］・④`--mode architect`［汎用設計・ルール策定］は最上位 **`claude-fable-5-1`** を自動使用）
6. **`data-release` (`/data-release`)**: 月次・年末白書の一次統計リリース原稿＋CC BY 4.0 引用キット生成＋本番CSV/JSON整合性検証（被リンク獲得エンジン）
7. **`growth-pdca` (`/growth-pdca`)**: ウォッチ登録条件・投票・アクセス回遊 × アフィリエイト枠別EPCを横断分析し、次週の必勝3手（記事・リライト・導線）を算出
8. **`ops-sentinel` (`/ops-sentinel`)**: 本番エンドポイント非汚染プローブ・データモート欠損/14日匿名化検査・デプロイ前ワンコマンド品質ゲート

## 8. データモート運用 ＆ Fly Postgres OOM防止規約（厳守）
- **保持期間とロールアップ順序**: `monitor_logs` 生ログは90日保持とし、期限切れ削除（`prune_old_logs`）は必ず `rollup_store_hourly_stats` による1時間粒度ロールアップ（`store_hourly_rollups` 永久保持）完了後にのみ実行すること。
- **連続日付バッチ分割（OOM防止）**: Fly Postgres（512MB VM）のメモリ枯渇を防ぐため、数週間〜90日分のログを集計・移送する処理では全期間一括 `SELECT *` を書かず、必ず `local_db._contiguous_date_ranges` / `_fetch_rows_by_date_ranges` の **最大7日バッチ分割** 方式を踏襲すること。
- **プライバシー匿名化とレート制限の維持**: `crowd_votes` の14日経過後の識別子（IPハッシュ等）自動匿名化処理、および `/api/watch-intent` の純粋IP単位レート制限（20件/10分超過時 HTTP 429）を無効化・緩和しないこと。
- **外部データの出典保持**: ドーム公演・JR運行情報・店舗公式イベント等の外部収集レコードは `source_url` と取得時刻を保持し、出典不明のレコードを生成しないこと。

## 9. 一次統計公開 ＆ CC BY 4.0 引用規約
- `/stats/monthly.csv` および `/stats/year-end-2026` は **CC BY 4.0** ライセンス（帰属表記：`出典: 相席ナビ編集部（https://aiseki-navi.com）`）で公開する一次統計インフラである。
- 報道機関・研究者・ブロガー・AI検索からの引用信頼性と被リンク価値を守るため、**一次統計ページ・CSV・`/api/llms-facts.json` には `/link/` アフィリエイトリンクを一切含めないこと**。
- DB一時障害等で集計結果が0件（空データ）となった場合、空レスポンスを長時間メモリキャッシュしないこと。CSVカラム構造を変更する場合は `llms-facts.json` の `schema_version` 更新とセットで行うこと。

## 10. Antigravity カスタマイズ制約 ＆ スキル2階層同期ルール
- **サイズ上限管理**: `AGENTS.md`（`CLAUDE.md` シンボリックリンク共有）は上限 `24,000 bytes` 未満（目標 `15,000 bytes` 以下）を維持すること。編集後は `wc -c AGENTS.md` と `test -L CLAUDE.md` でサイズとリンク維持を確認する。
- **2階層スキル同期**: ワークスペース未選択状態でも全スキルが認識されるよう、リポジトリ内正本 `.agents/skills/<name>/SKILL.md` とグローバル `~/.gemini/config/skills/<name>/SKILL.md`（`name`・`description`・正本パス案内のみの薄いポインタ）を常にペアで作成・更新すること。
