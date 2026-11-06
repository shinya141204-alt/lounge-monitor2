---
name: affiliate
description: >-
  相席ナビ（https://aiseki-navi.com）専用のアフィリエイト案件ワンタッチ登録・URL差し替え・薬機法＆直/link/＆ブランド表記監査スキル。
  ユーザーが `/affiliate <広告タグまたはURL>` と入力したときや、A8.net・アクセストレード等の広告タグを貼ってアフィリエイト案件の追加・差し替え・点検を依頼したときに必ず有効化する。
  タグ解析、薬機法・景表法チェック、コラム/一次統計への直 `/link/` 混入検閲、`affiliate_manager.py` / `app.py` / `static/affiliate_config.js` / `pdca_engine.py` / ガイドページの同期更新、キャッシュ完全同期、自己クリック汚染なしの疎通確認まで一気通貫で実行する。
---

# `/affiliate` アフィリエイト案件登録・差し替え・監査 Runbook

ユーザーが広告タグ（HTML）またはASPリンクを提示した際、以下の **Step 1 〜 Step 6** を順番に実行してミスなく本番反映してください。

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **タグ解析＆統合コンプライアンス監査スクリプト**: [scripts/parse_and_audit_affiliate.py](./scripts/parse_and_audit_affiliate.py)

---

## Step 1: 広告タグの自動解析と対象スロットの決定

1. ヘルパースクリプトで提示されたHTMLタグまたはURLを解析し、遷移先URL（`dest_url`）・バナー画像URL（`banner_img`）・1×1インプレッション計測ピクセル（`tracking_img`）を抽出する：
   ```bash
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --tag '<提示されたタグ>'
   ```
2. `affiliate_manager.py` の `AFFILIATE_SLOTS` を確認し、「既存スロットのURL・素材差し替え」か「新規スロットの追加」かを判断する。

---

## Step 2: 薬機法・景表法コンプライアンス ＆ 2ステップ導線ルールの厳守

1. **薬機法・医療広告ガイドライン・景表法の遵守**：
   - サプリ・食品で「二日酔いが治る／防ぐ」「肝臓を回復」「痩せる／脂肪が燃える」等の医薬品・痩身効能を絶対に書かない（「乾杯前後のコンディションサポート」「翌朝すっきり習慣」等で表現）。
   - 化粧品で「ニキビが治る」「シミが消える」「若返る」と書かない（「テカリ・乾燥を防ぐ」「清潔感をキープ」等で表現）。
   - 医療脱毛・アプリ等で「100%」「絶対」「永久に生えない」「必ずモテる」等の断定・保証表現を使わない。
2. **編集記事・一次統計ページとPR枠の完全分離（2ステップ導線）**：
   - 個別のデータ分析コラム（`templates/guide_*.html`・`scheduled_articles.py`）および一次統計ページ（`templates/stats_*.html`）には `/link/...` を絶対に直貼りしない。
   - PRカード・比較枠は `/grooming`（男性向け事前準備ガイド）、`/for-women`（女性向け特設ガイド）、店舗詳細等の専用枠、およびトップ画面モーダル（`static/affiliate_config.js`）に集約する。

---

## Step 3: コードベース4箇所＋ガイドページの同期更新

1. **`affiliate_manager.py`**：`AFFILIATE_SLOTS` の該当 `slot_id` の `default_url`（新規枠なら辞書エントリ＋`aliases`＋`env_keys`）を更新する。
2. **`app.py`**：`AFFILIATE_REDIRECTS` 辞書に同じ `slot_id`（およびエイリアス）とフォールバックURLを同期登録する。
3. **`static/affiliate_config.js`**：`AFFILIATE_MASTER.items` にカード情報（`url: '/link/<slot_id>'`, `bannerImg`, `trackingImg` 等）を更新し、1×1計測ピクセル（`0.gif`）がある場合は漏れなく設定する。
4. **`pdca_engine.py`**（新規枠追加時）：`OFFER_CATEGORIES` および `_classify_offer_category` に該当スロットを追加し、PDCA参謀のEPC集計対象に含める。
5. **`templates/grooming.html` / `templates/for_women.html`**（該当ターゲット層の場合）：ガイドページ内の紹介カード・`/link/<slot_id>` 導線・1×1計測ピクセルを同期する。

---

## Step 4: キャッシュバスティング完全同期

`static/affiliate_config.js` または CSS/JS を変更した場合は、以下を必ず同時にバンプする：
- `templates/index.html` および全公開テンプレート内の `affiliate_config.js?v=...`（および変更した CSS/JS）
- `templates/index.html` と `templates/_nav_loader.html` 両方の `/sw.js?v=...`
- `static/sw.js` の `CORE_ASSETS` と `CACHE_NAME`
- `tests/test_ui_audit_regression.py`（および `tests/test_app.py`）の期待バージョン定数

---

## Step 5: 自動監査 ＆ テスト実行（必須ゲート）

デプロイ前に本番汚染防止フラグ付きで以下の3つを実行し、すべてエラーゼロを確認する：

```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター && \
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --audit && \
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py && \
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests
```

---

## Step 6: 本番デプロイ ＆ 自己クリック汚染なしの疎通確認

1. `git push origin main` 後、統一コマンドで本番反映する：
   ```bash
   /Users/kuwanoshinya/.fly/bin/flyctl deploy . --config fly.toml --remote-only -a aiseki-navi-tokyo --ha=false
   ```
2. **【鉄則】本番リダイレクト確認時は必ず `-A "curl/8.0"` ＋ `?staff_test=1` を付与する**（`/grooming`・`/for-women` をブラウザ確認する場合も必ず `?staff_test=1` を付与）：
   ```bash
   curl -sI -A "curl/8.0" "https://aiseki-navi.com/link/<slot_id>?staff_test=1" | grep -iE "^http|^location"
   ```
   `HTTP/2 302` かつ `location:` が目的のASPリンクになっていることを確認して報告する。
