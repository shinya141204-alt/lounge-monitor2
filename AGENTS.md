# 相席ナビ (AISEKI NAVI) 共通開発・編集ルール (`AGENTS.md` / `CLAUDE.md`)

本ドキュメントは **Antigravity** および **Claude Code** の双方が自動的に読み込む共通ルールブックです。どちらのエージェントで作業する場合も、以下の規約を最優先で遵守してください。

## 1. 基本コミュニケーション・言語規約
- ユーザーへの返答・レポート・コミット要約説明は **すべて日本語** で行うこと。
- 根拠のない推測値やダミー数値を本番表示・記事本文に使わないこと（データ不足時は空欄または集計対象外とする）。

## 2. システム構成・ディレクトリ地図
- **本番URL**: `https://aiseki-navi.com`（Fly.io 東京リージョン `nrt`、アプリ名 `aiseki-navi-tokyo`：`web` + `worker` の2プロセス構成）
- **コアサーバー & Blueprint 分割**:
  - `app.py`: Flask 初期化・認証・セキュリティヘッダー・`/`（上位20店舗SSR軽量描画）・`/staff`・コア `/api/status` 等のエントリポイント
  - `routes/portal_bp.py`: ブランド別・`/store/<slug>`・`/areas`・`/columns`・`/stats`・`/price-comparison`・`/for-women`・`/concierge`・`/about`・`/contact`・全 `/guide/*` ルート
  - `routes/seo_feeds_bp.py`: `/robots.txt`・`/sitemap.xml`・`/llms.txt`・`/llms-full.txt`・`/api/llms-facts.json`・`/ads.txt`
  - `routes/staff_bp.py`: スタッフ・マスター管理API（計26ルート）および IG スライドキャッシュ
  - `routes/sns_jobs_bp.py`: 動的 OGP / リール動画配信、非同期 SNS ジョブキュー、全 `/api/trigger-*`
  - `routes/_bridge.py`: `sys.modules['app']` 動的プロキシ（既存テストの `mock.patch.object(app_module, ...)` 互換維持用）
- **主要データ・分析モジュール**:
  - `local_db.py` / `turbo_store.py`: PostgreSQL・Redis・SQLite WAL・Google Sheets の多層データストア
  - `logger.py`: `google-auth`（8秒タイムアウト `_BoundedGoogleAuthRequest`）による Google Sheets 同期（`oauth2client` は使用禁止）
  - `store_master.py` / `store_hours.py` / `store_insights.py`: 店舗マスタ・営業時間判定・店舗別28日実測分析生成

## 3. ブランド正式名称・表記・著者クレジットルール（厳守）
公開HTML・JSON・SNS投稿文・コラム・note原稿において、以下の略称・旧称は **一切使用禁止**：
- NG: 単体の `オリエンタル`（例: `オリエンタル新宿`）、内部コード `OLG`、英語表記 `YATAKOI`、旧略称 `オリラジ`
- OK（正式表記）: **`オリエンタルラウンジ`**、**`ag（アグ）`**、**`JIS（ジス）`**、**`ASOBIBAR（アソビバー）`**、**`ヤタコイ`**（各記事・ページの本文初出時は `ag（アグ）`・`JIS（ジス）`・`ASOBIBAR（アソビバー）` とカナ併記すること）
- **運営・著者クレジット統一（プライバシー保護）**:
  - サイト全体の運営主体は **`相席ナビ編集部`**、編集長・執筆責任者は **`くわもん（相席ナビ編集長）`**（JSON-LD `Person.name = "くわもん"`）で統一する。
  - 個人本名ローマ字表記や「店舗現場での勤務経験」は公開HTML・記事・SNSに絶対に記載しないこと（専門性は「Webエンジニアリング・時系列統計データ分析」として記載する）。

## 4. セキュリティ・計測汚染防止ルール（厳守）
1. **機密情報の保護**:
   - `SECRET_KEY`・`STAFF_PASSCODE`・`MASTER_PASSCODE`・`ADMIN_API_SECRET` などの実値をコード・Markdown・チャット履歴に出力しないこと。
   - `?passcode=` によるURLクエリログインは廃止済みのため復活させないこと（管理APIはセッションまたは `X-Admin-Secret` ヘッダーを使用）。
2. **アフィリエイト計測・クリック汚染の防止**:
   - 本番の `/link/<item_id>` へ疎通確認でアクセスする際は、**必ず `?staff_test=1` を付与** し、A8.net 等のインプレッション・クリック計測を絶対に汚さないこと。
   - `robots.txt` の全ボット個別グループの `Disallow: /link/` および `_is_bot_request()` の AI フェッチャー除外設定を弱めないこと。
   - コラム記事（`templates/guide_*.html`）本文には `/link/...` を直接貼らず、内部ページ（`/grooming` 等）への自然な導線にとどめること。

## 5. フロントエンド・UI/UX・キャッシュ同期ルール
- **脱AIデザイン基準**: 見出しへの絵文字乱用、派手なネオングラデーション、未定義 `var(--...)` の使用を禁止。ダーク（`#111113`）とライト（`body.theme-light` `#f6f6f7`）双方で WCAG コントラスト基準を満たすこと。
- **キャッシュバスティング3点同期**: `static/app.js` を更新した際は、`templates/index.html` の `app.js?v=...`、`sw.js?v=...`、および `static/sw.js` 内の `CACHE_NAME` / プリキャッシュ配列を必ず同時に更新すること。

## 6. テスト・監査・本番デプロイ手順
コードやテンプレートを変更した際は、デプロイ前に以下を実行して全件合格を確認すること：

```bash
# 1. アフィリエイト整合性・薬機法・景表法監査
python3 .agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --audit

# 2. UIコントラスト・未定義CSS変数・スクロール安全性監査
python3 .agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py

# 3. 全単体テストスイート実行（本番汚染防止フラグ付き）
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests

# 4. GitHub 反映 ＆ Fly.io 東京本番デプロイ
git push origin main
/Users/kuwanoshinya/.fly/bin/flyctl deploy . --config fly.toml --remote-only -a aiseki-navi-tokyo --ha=false
```

## 7. 専用スキル一覧（`.agents/skills/`）＆ Claude Fable 5.1 運用基準
詳細なワークフローは各スキルの `SKILL.md` を参照すること：
- `.agents/skills/article/SKILL.md`: 新規データ考察コラム＋16:9図解アイキャッチ＋note独立エッセイ一気通貫生成（勝負記事は Fable 5.1 で徹底推敲）
- `.agents/skills/editorial-ui-ux/SKILL.md`: クリーン・ミニマル UI/UX 改修＆自動回帰監査
- `.agents/skills/affiliate/SKILL.md`: アフィリエイト案件登録・薬機法監査
- `.agents/skills/store-audit/SKILL.md`: 全ブランドの店舗マスタ・営業時間・料金ドリフト点検
- `.agents/skills/claude-review/SKILL.md`: Antigravity × Claude Code 連携（日常レビューは `claude-opus-5-5`、以下の **切り札3領域** は 100万トークン対応の最上位モデル **`claude-fable-5-1`** を自動使用）
  1. **`--mode strategy`**: 収益化・SEO・導線設計の「全体戦略」
  2. **`--mode audit`**: 複雑な数理ロジック・大型改修の「最終バグ・セキュリティ監査」
  3. **`--mode flagship`**: 看板コラム・note 勝負記事の「構成＆脱AIの徹底推敲」
  - レート使用量確認: `python3 .agents/skills/claude-review/scripts/run_claude_review.py --usage`
