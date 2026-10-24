---
name: article
description: >-
  Full-pipeline article generator for Aiseki Navi (`https://aiseki-navi.com`).
  Activate this skill whenever the user types `/article <テーマ>` or asks to generate
  a new note.com + website column article on a specific theme. It automatically
  extracts real SQLite crowd data, generates a 16:9 eyecatch image, builds the
  SEO column HTML template, registers the route in `app.py`, `/columns`, `/sitemap.xml`,
  `x_poster.py`, and `tips_poster.py`, deploys to GitHub `main`, and outputs the
  ready-to-paste note.com manuscript and hashtags.
---

# `/article <テーマ>` 全自動記事生成・サイト実装・X登録・デプロイ Runbook

ユーザーが **`/article <テーマ>`** と入力したときは、確認質問で止めずに以下の **Step 1 〜 Step 6** を一気通貫で自動実行して完了させてください。

## プロジェクトパス
* リポジトリルート: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`

---

## Step 1: 実測データ（SQLite 24万件ログ）の抽出とスラッグ決定
1. テーマから英語のURLスラッグ `<slug>`（例: `rainy-day-strategy`, `umeda-vs-shinsaibashi` など）とテンプレート名 `templates/guide_<slug_underscored>.html`、アイキャッチ名 `static/eyecatch-<slug>.jpg`（および `.webp`）を決定する。
2. 以下のヘルパースクリプトを実行して、直近の実測データ（全国TOP5、高女子率穴場TOP5、曜日別・時間帯別統計、テーマ関連店舗の数値）を取得し、記事内の根拠データとして活用する：
   ```bash
   python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/article/scripts/fetch_theme_stats.py "<テーマ>"
   ```

---

## Step 2: 16:9 専用アイキャッチ画像の自動生成＆最適化保存
1. `invoke_subagent`（`TypeName: "image-generator"`）を呼び出し、記事タイトル・サブタイトルの日本語タイポグラフィを美しく入れた **16:9（1280x720）ラグジュアリー×データ分析風アイキャッチ画像** をアーティファクトディレクトリに生成する。
2. 生成された画像を Pillow で読み込み、以下の2形式で保存する：
   * `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/static/eyecatch-<slug>.jpg`（RGB, quality=86）
   * `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/static/eyecatch-<slug>.webp`（WEBP, quality=84）

---

## Step 3: サイト内コラムHTMLの作成（SEO構造化データ＋内部リンク＋収益導線）
[`templates/guide_beauty_stage.html`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/templates/guide_beauty_stage.html) または [`templates/guide_club_midnight.html`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/templates/guide_club_midnight.html) をベースにして、`templates/guide_<slug_underscored>.html` を作成する。必ず以下を含めること：
1. **メタタグ＆OGP**：
   * `<link rel="canonical" href="https://aiseki-navi.com/guide/<slug>">`
   * `<meta property="og:image" content="https://aiseki-navi.com/static/eyecatch-<slug>.jpg">`
   * `<meta name="twitter:card" content="summary_large_image">`
2. **JSON-LD 構造化データ**：`Article` + `BreadcrumbList` + `FAQPage`
3. **ナビゲーション＆フッター**：
   * 上部に `{% set nav_active = 'columns' %}` と `{% include '_portal_nav.html' %}`
   * 下部に `{% include '_portal_footer.html' %}`
4. **店舗個別ページ・統計白書・関連コラムへの内部リンク（サイト内回遊の最大化）**：
   * 記事内の店舗リンクは必ず [`store_master.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/store_master.py) の `STORE_SLUGS` に定義された正しいスラッグ（例: `/store/oriental-lounge-shinjuku`, `/store/jis-umeda`, `/store/ag-shibuya`）を使用する。
   * **【重要】記事内には外部アフィリエイト広告カード（`/link/...`）を直接貼らないこと。** まずは独自のデータ考察メディアとしての信頼性・権威性とサイト内PV（`/` リアルタイムランキング、`/stats` 統計白書、`/store/...` 店舗ページへの回遊）を最優先する。身だしなみに触れる場合は、内部ページ `/grooming` または `/guide/first-10-minutes` への自然なテキストリンク1本のみにとどめる（2ステップ構造）。

---

## Step 4: ルート・一覧・サイトマップ・X自動投稿への登録（即時公開 or 予約公開）
### A. 即時公開する場合（デフォルト）
1. **[`app.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/app.py)**：
   * `@app.route('/guide/<slug>')` を追加してテンプレートを返す。
   * `/sitemap.xml` に `https://aiseki-navi.com/guide/<slug>` を追加する。
2. **[`templates/columns.html`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/templates/columns.html)**：
   * `.articles-grid` の先頭に新着コラムカードを追加する。
3. **[`x_poster.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/x_poster.py)**：
   * `COLUMN_PROMO_LIST` の先頭に新コラムの告知スレッド定義を追加する（リプ欄はターゲットURL 1本のみにして専用OGPカードが確実に出るようにする）。
4. **[`tips_poster.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/tips_poster.py)**：
   * `TIPS_LIST` にテーマに関連する人間味のある豆知識ポストを1〜2個追加し、`get_tip_reply_text()` で該当インデックスのリプ欄URLが `/guide/<slug>` になるよう紐付ける。

### B. 日時指定で予約公開する場合（複数本まとめ作成・Scaled Content Abuse対策）
ユーザーから公開日時の指定がある場合や、複数記事を数日おきに分散公開したい場合は、[`scheduled_articles.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/scheduled_articles.py) を使う：
1. **`scheduled_articles.SCHEDULED_ARTICLES`** にメタ情報（`slug`, `path`, `template`, `publish_at` (JST), `publish_at_ja`, `lastmod`, `eyecatch_jpg`, `eyecatch_webp`, `badge`, `read_time`, `tag_primary`, `tag_secondary`, `title`, `short_title`, `desc`, `x_title`, `x_main_text`）を追加する（`x_main_text` は Twitter weight `<= 280` 厳守）。
2. **`app.py`** で `@app.route('/guide/<slug>')` を `_render_scheduled_guide('<slug>')` に紐付ける。テンプレート側には `{% if is_scheduled_preview %}<meta name="robots" content="noindex, nofollow">{% endif %}` とプレビューバナーを入れておく。
   * 予約日時前は一般アクセス `HTTP 404`（`/staff` ログイン中のみプレビュー可能）、予約日時到達後は自動で `/columns`, `/`, `/sitemap.xml` に掲載され、APScheduler が15分おきに検知して自動でXへ新着告知2連スレッドを投稿する。
3. **[`analytics.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/analytics.py)** の `_PAGE_TITLES` に `/guide/<slug>` を追加する。

---

## Step 5: テスト検証 ＆ GitHub本番デプロイ（＋Xへの即時紹介投稿）
1. Flask `test_client()` で `/guide/<slug>`, `/columns`, `/sitemap.xml` がすべて `HTTP 200` を返し、追加したTipの文字数ウェイトが `<= 280` であることを検証する。
2. `git add` → `git commit` → `git push origin main` で本番環境（Render）へデプロイする。

---

## Step 6: ユーザーへの完了報告（noteコピペ用原稿＋アイキャッチ＋ハッシュタグ）
チャットへの返答では以下を分かりやすく提示する：
1. 生成した **16:9 アイキャッチ画像のプレビュー**
2. 公開された **サイト内コラムURL（`https://aiseki-navi.com/guide/<slug>`）**
3. **【そのままコピペ用】note記事タイトル・ハッシュタグ一式・本文（Markdown）**
