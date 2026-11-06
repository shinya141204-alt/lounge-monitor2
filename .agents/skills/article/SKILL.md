---
name: article
description: >-
  Full-pipeline article generator for Aiseki Navi (`https://aiseki-navi.com`).
  Activate this skill whenever the user types `/article <テーマ>` or asks to generate
  a new note.com + website column article on a specific theme. It automatically
  extracts real SQLite crowd data, generates a 16:9 eyecatch image, builds the
  SEO column HTML template, registers the route in `routes/portal_bp.py`, `/columns`,
  `routes/seo_feeds_bp.py` (`/sitemap.xml`), `x_poster.py`, and `tips_poster.py`, deploys to GitHub `main`, and outputs the
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

## Step 2: 16:9 専用アイキャッチ画像（クリーンミニマル実測図解カード）の自動生成＆保存
1. ネオン・発光夜景・カクテルグラス・金枠明朝体・AI生成画像の文字化けを排し、サイト全体のブランドトーン（`#121215` マットブラック × オフホワイト `#f4f4f5` × ミュートローズ `#e29aa8` × ソフトスレート `#8ab4f8`）と統一された **16:9（1280×720）ミニマル実測図解カード** を Playwright レンダラーで直接生成する：
   ```bash
   python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/article/scripts/render_editorial_eyecatch.py \
     --slug "<slug>" \
     --cat "<英語カテゴリ表記（例: DATA STUDY / AREA STRATEGY）>" \
     --sample "LOG n = <検証件数>" \
     --title "<22〜28文字以内の1行メインタイトル>" \
     --sub "<サブコピー（1行）>" \
     --k1-kicker "<指標1見出し>" --k1-head "<項目1>" --k1-val "<数値1>" --k1-unit "<単位1>" --k1-desc "<補足1>" \
     --k2-kicker "<指標2見出し>" --k2-head "<項目2>" --k2-val "<数値2>" --k2-unit "<単位2>" --k2-desc "<補足2>" \
     --k3-kicker "<指標3見出し>" --k3-head "<項目3>" --k3-val "<数値3>" --k3-unit "<単位3>" --k3-desc "<補足3>"
   ```
   （よりテーマ固有の波形チャート・スペクトラム図を使う場合は [`render_editorial_eyecatch.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/article/scripts/render_editorial_eyecatch.py) の `build_all_asset_specs()` に定義を追加してもよい）
2. 以下の2形式が出力されたことを `read_file` で視覚確認し、文字の折り返し崩れや見切れがゼロであることを確かめる：
   * `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/static/eyecatch-<slug>.jpg`
   * `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/static/eyecatch-<slug>.webp`

---

## Step 3: サイト内コラムHTMLの作成（4層因果ピラミッド構造考察＋脱・金太郎飴＋内部リンク）
[`templates/guide_last_train_cliff.html`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/templates/guide_last_train_cliff.html) はHTML/CSSのタグ構造のみ参考にし、**文章構成・導入シーン・結論パターンは絶対にクローンしないこと（金太郎飴化の禁止）**。必ず以下を含めること：
1. **【最重要】3軸ディープ考察＆脱マンネリ執筆ルール**：
   * **手垢テンプレ・固定フレーズの流用禁止**：既存記事で多用された「エレベーターが開いた瞬間の重たい空気」「メイク90分」「徒歩4分移動」「とりあえず40分見切り」「サンクコスト錯誤」をテーマ無関係に使い回さないこと。必ず今回のテーマ固有の現場シーンと生データから始めること。
   * **3軸×4層因果ピラミッドで深掘りする**：
     - **軸1（60〜120分の時間波形ドラマ）**：20時の女性先行立ち上がり・22時の男性合流・深夜0時の男女クロス反転・終電後の第2波を実測数値で追う。
     - **軸2（男女それぞれの『本音の非対称』フラット観察）**：男女どちらの味方にも偏らないフラットな観察者として、「男性にとっての余裕店＝女性には受付待ちリスク」「男性先行帯＝女性組には待ちゼロ即乾杯」という双方の図星と本音を対等に描く。
     - **軸3（同じ街のブランド別カルチャー・時間帯相性）**：オリエンタルラウンジ・ag・JIS・ヤタコイ・ASOBIBARを優劣ではなく「年齢感・ノリ・強い時間帯の相性」として具体的に踏み込む。
   * 記事内に **`<div class="dialogue-box">...</div>` を使った男女双方の自然な会話・心の声を2〜4箇所** 織り込むこと。
   * **誇張・煽り表現の禁止**：「徹底解説」「完全攻略」「完全ガイド」「間違いなし」「必見」「3秒で」等のAI量産っぽい煽り語は使わず、実測ログにもとづく落ち着いた検証トーンで書くこと。
   * **ブランド呼称＆本文初出カナ併記ルール厳守**：単体の『オリエンタル』（例: `オリエンタル新宿`）、内部コード `OLG`、英語表記 `YATAKOI`、旧略称 `オリラジ` は一切使用禁止。必ず正式名称 **『オリエンタルラウンジ』**・**『ヤタコイ』** を使用し、本文初出時は **『ag（アグ）』**・**『JIS（ジス）』**・**『ASOBIBAR（アソビバー）』** とカナ併記すること。
   * **著者クレジット統一**：画面上の著者表記は `執筆・検証：<a href="/about">くわもん（相席ナビ編集長）</a>` とし、JSON-LD `Article.author` は `{"@type": "Person", "name": "くわもん", "jobTitle": "相席ナビ編集長", "url": "https://aiseki-navi.com/about"}`（`datePublished` / `dateModified` 必須）に統一する（本名ローマ字や店舗勤務経験は絶対に記載しない）。
2. **メタタグ＆OGP＆LLMO**：
   * `<link rel="canonical" href="https://aiseki-navi.com/guide/<slug>">`
   * `<link rel="alternate" type="text/markdown" href="https://aiseki-navi.com/llms.txt" title="相席ナビ AI要約ドキュメント (llms.txt)">`
   * `<meta property="og:image" content="https://aiseki-navi.com/static/eyecatch-<slug>.jpg?v=3">`
   * `<meta name="twitter:card" content="summary_large_image">`
3. **JSON-LD 構造化データ**：`Article`（`SpeakableSpecification`・`author: くわもん`・`datePublished`・`dateModified` 付） + `BreadcrumbList` + `FAQPage`
4. **ナビゲーション・共通CSS・ライトモード暗転防止・`<main>`・フッター**：
   * `<link rel="manifest" href="/static/manifest.json?v=3">`, `<link rel="icon" type="image/png" href="/static/icon-192.png?v=3">`, `<link rel="apple-touch-icon" href="/static/icon.png?v=3">`, `<meta name="theme-color" content="#111113">`
   * `<link rel="stylesheet" href="/static/style.css?v=20261030_6">`, `<link rel="stylesheet" href="/static/portal.css?v=14">`, `<link rel="stylesheet" href="/static/calm.css?v=35">`（既存ページと完全一致のクエリ文字列）を読み込み、`<head>` に `<script>try{if(localStorage.getItem('olg_view_theme')==='light')document.documentElement.classList.add('theme-light');}catch(e){}</script><style>html.theme-light{background-color:#f6f6f7;}</style>`、`<body>` 直後に `<script>try{if(localStorage.getItem('olg_view_theme')==='light')document.body.classList.add('theme-light');}catch(e){}</script>` を入れること
   * 上部に `{% set nav_active = 'columns' %}` と `{% include '_portal_nav.html' %}`、主コンテンツ全体を `<main> ... </main>` で囲み、下部に `{% include '_portal_footer.html' %}`
   * スタッフ非表示ガード `{% if not is_staff and not session.get('is_staff') %}`（予約記事なら `and not is_scheduled_preview` も追加）で AdSense タグを囲むこと
5. **店舗個別ページ・統計白書・関連コラムへの内部リンク（サイト内回遊の最大化）**：
   * 記事内の店舗リンクは必ず [`store_master.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/store_master.py) の `STORE_SLUGS` に定義された正しいスラッグ（例: `/store/oriental-lounge-shinjuku`, `/store/jis-umeda`, `/store/ag-shibuya`）を使用する。
   * **【重要】記事内には外部アフィリエイト広告カード（`/link/...`）を直接貼らないこと。** まずは独自のデータ考察メディアとしての信頼性・権威性とサイト内PV（`/` リアルタイムランキング、`/stats` 統計白書、`/store/...` 店舗ページへの回遊）を最優先する。身だしなみに触れる場合は、内部ページ `/grooming` または `/guide/first-10-minutes` への自然なテキストリンク1本のみにとどめる（2ステップ構造）。

---

## Step 3.5: Claude Fable 5.1 / Opus 5.5 辛口デスク自動推敲ゲート（構成＆脱AIの徹底推敲）
作成したコラムHTMLと書き下ろし note エッセイ原稿に対し、デプロイ前に必ず読み取り専用デスク校閲を通し、指摘された構成の甘さ・AI優等生構文・誇張語・会話の硬さを Antigravity の `edit_file` で磨き上げる：
* **看板コラム・ピラー比較記事・note 勝負記事（デフォルト推奨）** → 最上位モデル **Claude Fable 5.1（`--mode flagship`）** で構成骨格から1文単位の生身改稿案まで徹底推敲する：
  ```bash
  python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
    --mode flagship \
    --files templates/guide_<slug_underscored>.html \
    --prompt "編集長くわもんの語り口として自然か、冒頭15秒の引き・3軸因果構成・AI優等生構文の排除・男女会話の体温を徹底推敲し、コピペ置換できる改稿案を出して"
  ```
* **軽量・速報系の通常記事（スピード優先時）** → `claude-opus-5-5`（`--mode article`）を使用する：
  ```bash
  python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
    --mode article \
    --files templates/guide_<slug_underscored>.html \
    --prompt "編集長くわもんの語り口として自然か、AIっぽい煽り語や会話の不自然さがないか辛口添削して"
  ```

---

## Step 4: ルート・一覧・サイトマップ・X自動投稿への登録（即時公開 or 予約公開）
### A. 即時公開する場合（デフォルト）
1. **[`routes/portal_bp.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/routes/portal_bp.py) ＆ [`routes/seo_feeds_bp.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/routes/seo_feeds_bp.py)**（`app.py` から Blueprint 分割済み）：
   * `routes/portal_bp.py` に `@portal_bp.route('/guide/<slug>')` を追加してテンプレートを返す。
   * `routes/seo_feeds_bp.py` の `sitemap_xml()` に `https://aiseki-navi.com/guide/<slug>` を追加する。
2. **[`templates/columns.html`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/templates/columns.html)**：
   * `.articles-grid` の先頭に新着コラムカードを追加する。
3. **[`x_poster.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/x_poster.py)**：
   * `COLUMN_PROMO_LIST` の先頭に新コラムの告知スレッド定義を追加する（リプ欄はターゲットURL 1本のみにして専用OGPカードが確実に出るようにする）。
4. **[`tips_poster.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/tips_poster.py)**：
   * `TIPS_LIST` にテーマに関連する人間味のある豆知識ポストを1〜2個追加し、`get_tip_reply_text()` で該当インデックスのリプ欄URLが `/guide/<slug>` になるよう紐付ける。

### B. 日時指定で予約公開する場合（複数本まとめ作成・Scaled Content Abuse対策）
ユーザーから公開日時の指定がある場合や、複数記事を数日おきに分散公開したい場合は、[`scheduled_articles.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/scheduled_articles.py) を使う：
1. **`scheduled_articles.SCHEDULED_ARTICLES`** にメタ情報（`slug`, `path`, `template`, `publish_at` (JST), `publish_at_ja`, `lastmod`, `eyecatch_jpg`, `eyecatch_webp`, `badge`, `read_time`, `tag_primary`, `tag_secondary`, `title`, `short_title`, `desc`, `x_title`, `x_main_text`）を追加する（`x_main_text` は Twitter weight `<= 280` 厳守）。
2. **`routes/portal_bp.py`** で `@portal_bp.route('/guide/<slug>')` を `_render_scheduled_guide('<slug>')` に紐付ける（`guide_dynamic_scheduled` があるため動的解決も動作するが、明示ルートを追加してもよい）。テンプレート側には `{% if is_scheduled_preview %}<meta name="robots" content="noindex, nofollow">{% endif %}` とプレビューバナーを入れておく。
   * 予約日時前は一般アクセス `HTTP 404`（`/staff` ログイン中のみプレビュー可能）、予約日時到達後は自動で `/columns`, `/`, `/sitemap.xml` に掲載され、APScheduler が15分おきに検知して自動でXへ新着告知2連スレッドを投稿する。
3. **[`analytics.py`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/analytics.py)** の `_PAGE_TITLES` に `/guide/<slug>` を追加する。

---

## Step 5: テスト検証 ＆ GitHub・東京本番サーバー（Fly.io）デプロイ
1. Flask `test_client()` で `/guide/<slug>`, `/columns`, `/sitemap.xml` がすべて `HTTP 200` を返し、追加したTipの文字数ウェイトが `<= 280` であることを検証する。
2. `git add` → `git commit` → `git push origin main` で GitHub へ反映する。
3. 続けて `/Users/kuwanoshinya/.fly/bin/flyctl deploy -a aiseki-navi-tokyo --ha=false` を実行し、本番環境（Fly.io 東京 `nrt`：`https://aiseki-navi.com`）へ無停止デプロイする。

---

## Step 6: ユーザーへの完了報告（Webとは別構成のnote専用深層エッセイ原稿＋アイキャッチ＋ハッシュタグ）
チャットへの返答では以下を分かりやすく提示する：
1. 生成した **16:9 アイキャッチ画像のプレビュー**
2. 公開された **サイト内コラムURL（`https://aiseki-navi.com/guide/<slug>`）**
3. **【そのままコピペ用】note専用・独立書き下ろしエッセイ原稿（タイトル・ハッシュタグ・本文Markdown）**
   * **※Web記事のコピペ転載は厳禁！** Web版が「検索読者向けの構造化データリファレンス（番号付き見出し・比較表・FAQ）」であるのに対し、note版は「編集長・くわもんの一人称による人間観察・データ考察エッセイ（番号なし `## ` 見出し・`> ` 引用・1,700〜2,500文字）」として完全に別原稿で書き分けること（または `column_editorial.export_column_for_note('<slug>', mode='essay')` を活用し、勝負記事は Claude Fable 5.1 `--mode flagship`、定例記事は Opus 5.5 `--mode article` で脱AI推敲すること）。
