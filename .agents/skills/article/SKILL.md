---
name: article
description: >-
  相席ナビ（https://aiseki-navi.com）専用の全自動データ考察コラム＆note独立エッセイ一気通貫生成スキル。
  ユーザーが `/article <テーマ>` と入力したときや、特定テーマのWebコラム＋note原稿作成を依頼したときに必ず有効化する。
  90日実測ログ・1h永久ロールアップ・6大ドーム日程・現地待ち分数・本番 `llms-facts.json` を自動抽出し、
  16:9図解アイキャッチ生成、4層因果コラムHTML構築、Claude Fable 5.2 / Opus 5.5 脱AI推敲、
  Blueprintルート・サイトマップ・X/Tips自動投稿登録、非汚染テスト＆本番デプロイ、note書き下ろし原稿出力まで完結させる。
---

# `/article <テーマ>` 全自動記事生成・サイト実装・X登録・デプロイ Runbook

ユーザーが **`/article <テーマ>`** と入力したときは、確認質問で止めずに以下の **Step 1 〜 Step 6** を一気通貫で自動実行して完了させてください。

## プロジェクトパス
* リポジトリルート: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`

---

## Step 1: 独自データモート（90日実測＋ロールアップ＋本番 `llms-facts`）の抽出とスラッグ決定
1. テーマから英語のURLスラッグ `<slug>`（例: `rainy-day-strategy`, `umeda-vs-shinsaibashi`）とテンプレート名 `templates/guide_<slug_underscored>.html`、アイキャッチ名 `static/eyecatch-<slug>.jpg`（および `.webp`）を決定する。
2. 以下のヘルパースクリプトを実行し、本番 `/api/llms-facts.json?staff_test=1`（非汚染UA）およびローカルDBから実測統計（店舗別30日平均/ピーク、高女子率穴場、曜日・時間帯波形、90日予測精度、直近ドーム公演、現地待ち分数、別業態比較）を抽出する：
   ```bash
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/article/scripts/fetch_theme_stats.py "<テーマ>" --days 30 --source both
   ```
   * **鉄則**: 出力JSONで `null` となっている項目（サンプル未到達の指標など）は、記事内で推測数値化せず言及対象から外すこと。

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
2. 以下の2形式が出力されたことを `read_file` で視覚確認し、文字の折り返し崩れや見切れがゼロであることを確かめる：
   * `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/static/eyecatch-<slug>.jpg`
   * `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/static/eyecatch-<slug>.webp`

---

## Step 3: サイト内コラムHTMLの作成（4層因果ピラミッド構造考察＋脱・金太郎飴＋内部リンク）
[`templates/guide_last_train_cliff.html`](file:///Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/templates/guide_last_train_cliff.html) はHTML/CSSのタグ構造のみ参考にし、**文章構成・導入シーン・結論パターンは絶対にクローンしないこと（金太郎飴化の禁止）**。必ず以下を含めること：
1. **【最重要】3軸ディープ考察＆脱マンネリ執筆ルール**：
   * **手垢テンプレ・固定フレーズの流用禁止**：既存記事で多用された「エレベーターが開いた瞬間の重たい空気」「メイク90分」「徒歩4分移動」「とりあえず40分見切り」「サンクコスト錯誤」をテーマ無関係に使い回さないこと。
   * **3軸×4層因果ピラミッドで深掘りする**：
     - **軸1（60〜120分の時間波形ドラマ）**：20時の女性先行立ち上がり・22時の男性合流・深夜0時の男女クロス反転・終電後の第2波やドーム終演サージを実測数値で追う。
     - **軸2（男女それぞれの『本音の非対称』フラット観察）**：男女どちらの味方にも偏らない観察者として、双方の図星と本音を対等に描く。
     - **軸3（同じ街のブランド・隣接業態カルチャー相性）**：オリエンタルラウンジ・ag（アグ）・JIS（ジス）・ヤタコイ・ASOBIBAR（アソビバー）（必要に応じて審査制デート等との比較）を「年齢感・ノリ・強い時間帯の相性」として踏み込む。
   * 記事内に **`<div class="dialogue-box">...</div>` を使った男女双方の自然な会話・心の声を2〜4箇所** 織り込むこと。
   * **誇張・煽り表現の禁止**：「徹底解説」「完全攻略」「完全ガイド」「間違いなし」「必見」「3秒で」等のAI量産煽り語は禁止。
   * **ブランド呼称＆本文初出カナ併記ルール厳守**：単体の『オリエンタル』、内部コード `OLG`、英語表記 `YATAKOI`、旧略称 `オリラジ` は一切使用禁止。必ず正式名称 **『オリエンタルラウンジ』**・**『ヤタコイ』** を使用し、本文初出時は **『ag（アグ）』**・**『JIS（ジス）』**・**『ASOBIBAR（アソビバー）』** とカナ併記する。
   * **著者クレジット統一**：画面上の著者表記は `執筆・検証：<a href="/about">くわもん（相席ナビ編集長）</a>` とし、JSON-LD `Article.author` は `{"@type": "Person", "name": "くわもん", "jobTitle": "相席ナビ編集長", "url": "https://aiseki-navi.com/about"}`（`datePublished` / `dateModified` 必須）に統一する（本名ローマ字や店舗勤務経験は絶対に記載しない）。
2. **メタタグ＆OGP＆LLMO 3点リンク必須**：
   * `<link rel="canonical" href="https://aiseki-navi.com/guide/<slug>">`
   * `<link rel="alternate" type="text/markdown" href="https://aiseki-navi.com/llms.txt" title="相席ナビ AI要約ドキュメント (llms.txt)">`
   * `<link rel="alternate" type="text/markdown" href="https://aiseki-navi.com/llms-full.txt" title="相席ナビ 全店舗リアルタイムデータ＆完全ガイド (llms-full.txt)">`
   * `<link rel="alternate" type="application/json" href="https://aiseki-navi.com/api/llms-facts.json" title="相席ナビ AIグラウンディング構造化JSON (llms-facts.json)">`
   * `<meta property="og:image" content="https://aiseki-navi.com/static/eyecatch-<slug>.jpg?v=3">`
   * `<meta name="twitter:card" content="summary_large_image">`
3. **JSON-LD 構造化データ**：`Article`（`SpeakableSpecification`・`author: くわもん`・`datePublished`・`dateModified` 付） + `BreadcrumbList` + `FAQPage`
4. **ナビゲーション・共通CSS動的同期・ライトモード暗転防止・`<main>`・フッター**：
   * **【重要：固定バージョン直書き禁止】** `style.css?v=...`、`portal.css?v=...`、`calm.css?v=...`、`manifest.json?v=...` 等のクエリ文字列は、必ず直近の既存コラムまたは `tests/test_ui_audit_regression.py` の期待定数を `grep` して **現行正本と1文字違わず一致** させること。
   * `<head>` に `<script>try{if(localStorage.getItem('olg_view_theme')==='light')document.documentElement.classList.add('theme-light');}catch(e){}</script><style>html.theme-light{background-color:#f6f6f7;}</style>`、`<body>` 直後に `<script>try{if(localStorage.getItem('olg_view_theme')==='light')document.body.classList.add('theme-light');}catch(e){}</script>` を入れる。
   * 上部に `{% set nav_active = 'columns' %}` と `{% include '_portal_nav.html' %}`、主コンテンツを `<main> ... </main>` で囲み、下部に `{% include '_portal_footer.html' %}`。
   * スタッフ非表示ガード `{% if not is_staff and not session.get('is_staff') %}`（予約記事なら `and not is_scheduled_preview` も追加）で AdSense タグを囲む。
5. **内部リンク ＆ 直 `/link/` 完全禁止（2ステップ構造）**：
   * 店舗リンクは `store_master.STORE_SLUGS` の正規スラッグを使用し、統計引用時は `/stats`（または `/stats/year-end-2026`）へのリンクを添える。
   * **記事内には `/link/...` を絶対に直貼りしない**（`parse_and_audit_affiliate.py --audit` でエラー検出される）。身だしなみ・他サービス検討に触れる場合は `/grooming`・`/for-women`・`/price-comparison` への自然な内部リンクにとどめる。

---

## Step 3.5: Claude Fable 5.2 / Opus 5.5 辛口デスク自動推敲ゲート
デプロイ前に読み取り専用デスク校閲を通し（ファイルは自動インライン添付される）、指摘された構成の甘さ・AI優等生構文・会話の硬さを Antigravity の `edit_file` で磨き上げる：
* **看板コラム・ピラー比較記事・note 勝負記事（デフォルト推奨）** → **`claude-fable-5-2`（`--mode flagship`）**：
  ```bash
  python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
    --mode flagship \
    --files templates/guide_<slug_underscored>.html \
    --prompt "編集長くわもんの語り口として自然か、冒頭15秒の引き・3軸因果構成・AI優等生構文の排除・男女会話の体温を徹底推敲し、コピペ置換できる改稿案を出して"
  ```
* **軽量・速報系の通常記事（スピード優先時）** → **`claude-opus-5-5`（`--mode article`）** を使用する。

---

## Step 4: ルート・一覧・サイトマップ・アクセス集計タイトル・X自動投稿への登録
### A. 即時公開する場合（デフォルト）
1. **`routes/portal_bp.py` ＆ `routes/seo_feeds_bp.py`**：
   * `routes/portal_bp.py` に `@portal_bp.route('/guide/<slug>')` を追加。
   * `routes/seo_feeds_bp.py` の `sitemap_xml()` に `https://aiseki-navi.com/guide/<slug>` を追加。
2. **`analytics.py`**：
   * `_PAGE_TITLES` に `'/guide/<slug>'` と日本語タイトルを追加する（即時公開でも漏れなく登録すること）。
3. **`templates/columns.html`**：
   * `.articles-grid` の先頭に新着コラムカードを追加。
4. **`x_poster.py` ＆ `tips_poster.py`**：
   * `x_poster.COLUMN_PROMO_LIST` の先頭に告知スレッド定義を追加し、`tips_poster.TIPS_LIST` ＆ `get_tip_reply_text()` に豆知識ポスト（Twitter weight `<= 280`）を紐付ける。

### B. 日時指定で予約公開する場合
* `scheduled_articles.SCHEDULED_ARTICLES`、`routes/portal_bp.py`（`_render_scheduled_guide('<slug>')`）、`analytics.py`（`_PAGE_TITLES`）に登録し、テンプレートに `{% if is_scheduled_preview %}<meta name="robots" content="noindex, nofollow">{% endif %}` を入れる。

---

## Step 5: 自動監査・単体テスト ＆ GitHub・東京本番（Fly.io）デプロイ
1. 本番汚染防止フラグ付きで監査・テストを一括実行し、全件合格を確認する：
   ```bash
   cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター && \
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --audit && \
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py --quick && \
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests
   ```
2. `git push origin main` 後、統一コマンドで本番へデプロイし、**必ず `-A "curl/8.0"` ＋ `?staff_test=1`** で疎通確認する：
   ```bash
   /Users/kuwanoshinya/.fly/bin/flyctl deploy . --config fly.toml --remote-only -a aiseki-navi-tokyo --ha=false
   curl -sI -A "curl/8.0" "https://aiseki-navi.com/guide/<slug>?staff_test=1" | head -n 5
   ```

---

## Step 6: ユーザーへの完了報告（note専用・独立書き下ろしエッセイ原稿＋アイキャッチ＋ハッシュタグ）
1. 生成した **16:9 アイキャッチ画像のプレビュー**
2. 公開された **サイト内コラムURL（`https://aiseki-navi.com/guide/<slug>`）**
3. **【そのままコピペ用】note専用・独立書き下ろしエッセイ原稿（タイトル・ハッシュタグ・本文Markdown）**
   * Web記事のコピペ転載は厳禁。note版は「編集長・くわもんの一人称による人間観察・データ考察エッセイ（番号なし `## ` 見出し・`> ` 引用・1,700〜2,500文字）」として完全に別原稿で書き分けること。
