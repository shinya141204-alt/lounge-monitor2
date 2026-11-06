---
name: editorial-ui-ux
description: >-
  相席ナビ（https://aiseki-navi.com）専用の「脱AIデザイン＆クリーン・ミニマルUX設計」スキル。
  ユーザーが `/ui <対象画面・課題>` と入力したときや、サイトのデザイン改善・UI/UX改修・AIっぽさの解消・使いやすさ向上を依頼したときに必ず有効化する。
  Mobbinプラグインの実在アプリUIリサーチと、相席ナビ固有のクリーン・ミニマル基準（ニュートラル黒 #111113 ／昼・ライト #f6f6f7 切替・PC 2ペイン追従パネル・スマホボトムシート）・未定義CSS変数＆キャッシュ完全同期＆Playwright自動UI回帰監査を組み合わせて改修を実行する。
---

# 相席ナビ 脱AIデザイン ＆ クリーン・ミニマルUX スキル

AI特有の「絵文字だらけ・均一な角丸カード・派手なグラデーション・説明過多」を徹底排除し、**実在する一流プロダクトの操作導線（Mobbin根拠）× クリーン・ミニマル（ニュートラル黒 `#111113` ＆ 昼・ライト `#f6f6f7` ワンタップ切替）**で相席ナビの画面を設計・改修する手順書です。

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **詳細デザイン規約・禁止パターン**: [references/design_tokens_and_anti_ai.md](./references/design_tokens_and_anti_ai.md)（実装前に必ず一読すること）
- **自動監査スクリプト**: [scripts/audit_anti_ai_ui.py](./scripts/audit_anti_ai_ui.py)

---

## Step 1: 対象画面の「1行ジョブ定義」と高速現状診断

1. [references/design_tokens_and_anti_ai.md](./references/design_tokens_and_anti_ai.md) を読み、相席ナビの脱AI7大ルールとカラートークンを確認する。
2. 改修対象の画面・セクションについて、**「誰が・どんな状況で開き・何秒で何を決められれば完了か」**を1行で定義する。
3. 対象テンプレートに対して高速静的診断（`--quick`：PC縦スクロール安全性・絵文字/派手グラデ密度・未定義CSS変数・キャッシュバスティング完全同期を1秒で検証）を実行する：
   ```bash
   python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py --target <対象ファイルパス> --quick
   ```

---

## Step 2: Mobbin 実在アプリUIパターンの参照（Mobbin連携時）

構造・操作導線の変更を伴う場合は **Mobbin プラグイン**を活用する：
1. Mobbin の `best-practices` / `redesign-screen` / `search` スキルの手順に従い、対象コンポーネント（ランキング行・フィルタバー・比較テーブル・店舗詳細シート等）の実在アプリ画面を参照する。
2. **鉄則（構造のみ借りて、世界観は相席ナビを守る）**：
   - Mobbin の参照画面からは**「情報の優先順位・グルーピング・ボタン配置・余白比率」だけ**を取り入れる。
   - 色・フォント・日本語コピー・リアルタイムデータ構造は、必ず相席ナビ自身のデザインシステム（ニュートラル黒 `#111113` × 純白ピルCTA ／ 昼・ライト `#f6f6f7` × 純黒ピルCTA ／ モダン・サンセリフ × `tabular-nums`）で組む。

---

## Step 3: 脱AI・機能的UXの実装ルール

1. **引き算のレイアウト**：問題解決に不要なアイコン・絵文字（`✨` `🔥` `⭐` 等）・サブタイトル・3重の囲み枠を足さない。情報整理は「余白の差」と「繊細な `1px` 罫線」で行う。
2. **視線の主役を1つに絞る**：1画面・1カードの中で最も目立つ要素は1つだけ。補助バッジは最大2個まで。
3. **数字の縦揃え（`tabular-nums` 徹底）**：`/`・`/store/<slug>`・`/stats`・`/stats/year-end-2026`・`/price-comparison` 等における人数・比率・予測値・料金・時刻・統計サンプル数には `font-variant-numeric: tabular-nums` を徹底する。
4. **スマホ片手操作（390px）＆ PC一覧性（1280px）の両立**：
   - タップ領域は最低 `38px〜44px` を確保し、スマホ幅での1〜2文字孤立改行を防ぐ。
   - `static/calm.css` の `html` 要素には絶対に `overflow` 系プロパティを書かない（PCマウスホイール縦スクロール維持）。
5. **ダークモード ＆ ライトモード同時仕上げ**：`body.theme-light` でのコントラスト比（`>= 4.5:1`、主要文字 `>= 5.5:1`）を同時に満たす。
6. **キャッシュバスティング完全同期**：
   - `static/calm.css`, `static/portal.css`, `static/style.css`, `static/app.js`, `static/affiliate_config.js`, `static/sw.js` のいずれかを編集したら、`templates/index.html`・`templates/_nav_loader.html`・全 `templates/*.html`・`static/sw.js`（`CORE_ASSETS` ＆ `CACHE_NAME`）・`tests/test_ui_audit_regression.py` の期待バージョン番号を必ず同時にバンプする。

---

## Step 4: 自動UI回帰テスト ＆ 実ブラウザ検証（必須ゲート）

編集後、必ず以下の監査スクリプトをフル実行して **エラーゼロ** を確認してから完了報告（または本番デプロイ）を行うこと：

```bash
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py
```

これにより、未定義CSS変数ゼロ・キャッシュ番号完全一致・ダーク/ライトWCAGコントラスト・PCマウスホイール縦スクロール・スマホ390px表示崩れ防止がPlaywright実ブラウザ込みで自動保証されます。
