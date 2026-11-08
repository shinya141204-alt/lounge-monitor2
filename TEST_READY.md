# 相席ナビ (AISEKI NAVI) Gemini 4 Argon & 包括大型アップデート E2Eテストスイート準備完了報告 (TEST_READY.md)

## 1. 概要
相席ナビ（AISEKI NAVI）の要求仕様書（`ORIGINAL_REQUEST.md`: 135〜197行目）およびプロジェクト仕様書（`PROJECT.md`）に基づき、製品全体の振る舞いを不透明ボックス（Opaque-box）で検証する包括的な **4階層E2Eテストスイート (`tests/test_gemini4_argon_e2e.py`)** の設計・実装・配置が完了しました。

外部API呼び出しやネットワーク通信に一切依存せず、モック・フェイルオープン・キャッシュ・フォールバック機構を精密にシミュレートし、全56件の厳格な検証テストケースを網羅しています。

---

## 2. テスト実行コマンド

### 2.1 E2Eテストスイート単体実行（推奨）
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest tests/test_gemini4_argon_e2e.py
```

### 2.2 全テストスイート一括実行（全体回帰テスト）
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest discover -s tests -p "test_*.py"
```

---

## 3. 4層階層アプローチ（テスト設計構成）サマリー

本テストスイートは、`ORIGINAL_REQUEST.md` で義務付けられた4階層アプローチに厳格に準拠して設計されています。

| Tier | 分類 | テスト件数 | 検証対象 |
|:---|:---|:---:|:---|
| **Tier 1** | 機能網羅（Feature Coverage） | 23件 | R1〜R4の主要正常系・個別機能の孤立動作確認 |
| **Tier 2** | 境界値・極限ケース（Boundary & Corner） | 23件 | フェイルオープン・タイムアウト・異常値・安全フォールバック |
| **Tier 3** | 複数機能連携（Cross-Feature Combinations） | 5件 | 機能間インターフェース（Argon×Claude推敲、Reels×視覚検閲、予測v3×シミュレーター） |
| **Tier 4** | 実世界シナリオ（Real-World Application Scenarios） | 5件 | 深夜ハシゴ動線、毎時55分Reels自動投稿、統計白書探索、API全滅障害、薬機法・ブランド規約 |
| **合計** | **全階層合計** | **56件** | **（目標50件以上を完全達成）** |

---

## 4. テスト実行現況（2026-11-08 時点）

- **総テスト数**: 56件
- **構文・インポートエラー**: **0件（完全完走）**
- **実行結果**: **52件 PASS / 4件 FAIL** (Ran in ~9.8s)
- **FAILのステータス**:
  - テストコード側の欠陥ではなく、**「Worker M2による全63店舗データ登録待ち（1件）」** および **「正確に検出された実装バグ（3件）」** による正常な失敗です。

---

## 5. 機能別チェックリスト (R1 〜 R4)

### R1: Gemini 4 Argon 解放 & 最上位LLM協調
- [x] `gemini-4-argon` を最優先モデルとして設定・エンドポイント通信フォーマット準拠 (`test_01`)
- [x] 8段階フォールバック連鎖 (`gemini-4-argon` → `gemini-3.1-pro-preview` → `gemini-3.8-flash` → ...) (`test_02`)
- [x] Claude 2段階（Opus 5.5 / Sonnet 5.5）による人間味推敲 (`test_03`, `test_47`)
- [x] 404 / 429 レート制限 / 503 過負荷時の安全フォールバック (`test_24`, `test_25`)
- [x] Claude通信エラー時の無修正ドラフト安全返却 (`test_26`)

### R2: 視覚検閲ゲート (Visual QA Gate) & 高速マルチモーダル
- [x] 100点満点スコアリング & 70点未満/未検出時の別テンプレート自動再生成 (`test_07`, `test_48`)
- [x] 多重安全フォールバック連鎖 (`gemini-4-argon` → `gemini-2.5-flash` → `gemini-2.5-pro` → 最終Fail-Open) (`test_08`)
- [x] 7.0秒タイムアウト打ち切りとFail-Open通過 (`test_30`)
- [x] OGP 1200x630 / Reels 1080x1920 解像度・クリッピング上限検証 (`test_33`)
- [x] SVGグラフィックスの破損時PNGフォールバック (`test_32`)

### R3: 全国全63店舗 2軒目完全ガイド & ソウル対応
- [ ] `static/second_venues.json` への全63店舗完全網羅 (`test_13`: Worker M2 実装待ち)
- [x] 1店舗あたり実在2〜3候補（徒歩3〜5分・朝5時/深夜営業・夜カフェ/バー）(`test_14`)
- [x] 韓国・ソウル2店舗（カンナム・ホンデ）の2軒目完全対応 & Google Maps URL整合性 (`test_17`, `test_51`)
- [x] `/store/<slug>` 店舗詳細SSRへの2軒目案内セクション出力 (`test_18`)
- [x] LINE共有テキスト生成 (`test_16`)
- [ ] `store_name=None` 呼び出し時の防御的ハンドリング (`test_37`: **BUG-A 検出中**)

### R4: 予測モデルv3 (Empirical-Bayes) & 統計シミュレーターUI
- [x] 階層Empirical-Bayesバイアス補正 (`@brand` 単位事前分布 + 観測信頼度重み付け) (`test_19`)
- [x] 天候連動型係数（雨天 +15% / 台風 -40% 等）の乗算合成 (`test_20`)
- [x] `/stats` への動的シミュレーターUI（SVGインライン波形グラフ）のSSR展開 (`test_23`)
- [x] 28日未満新店舗・データ欠損時のフォールバック (`test_41`)
- [ ] `/stats` ページ内における直 `/link/` ゼロ厳守 (`test_50`, `test_54`: **BUG-B 検出中**)

---

## 6. エスカレーション事項（検出された実装バグ一覧）

E2Eテストスイートにより、以下の実装バグが正確に検出されました。担当ワーカーへのエスカレーションと改修を要請します。

### 6.1 【BUG-A】`second_venues.py:266` の `None` 引数起因 `AttributeError`
- **検出テスト**: `Tier2BoundaryAndCornerCasesTests.test_37_r3_none_and_empty_store_name_safety`
- **エラー内容**: `AttributeError: 'NoneType' object has no attribute 'strip'`
- **発生箇所**: `second_venues.py:266`（`get_store_anchor(store_name)` 内で `store_name.strip()` を直接実行）
- **改修要請**:
  ```python
  # second_venues.py:266
  # 修正前:
  norm = store_name.strip()
  # 修正後:
  norm = (store_name or "").strip()
  if not norm:
      return {}
  ```

### 6.2 【BUG-B】`/stats` 統計白書ページへの直 `/link/` 混入（コンプライアンス違反）
- **検出テスト**:
  - `Tier3CrossFeatureCombinationsTests.test_50_r4_forecast_v3_to_stats_simulator_integration`
  - `Tier4RealWorldScenariosTests.test_54_scenario_stats_interactive_simulator_exploration`
- **エラー内容**: `AssertionError: Stats surface must strictly avoid /link/ direct links`
- **発生箇所**: `templates/stats_report.html` または `routes/portal_bp.py` 経由のコンポーネント内において、以下の直アフィリエイトリンクが出力されている：
  - `href="/link/dating_bachelor?from=/stats"`
  - `href="/link/women_second_venue?from=/stats"`
- **違反仕様**:
  - `ORIGINAL_REQUEST.md:154, 186` にて「`/stats`（一次統計白書・学術・報道引用面）は直 `/link/` 完全ゼロ厳守」と規定。
- **改修要請**:
  - `/stats` レンダリング時に直 `/link/` を含むCTAコンポーネントを除外するか、内部ガイドページ（`/guide/second-venue` や `/columns`）への通常内部リンクに差し替えること。

---

## 7. 最終合格判定基準 (Acceptance Criteria)

Worker M2 による全63店舗データの登録完了、および BUG-A / BUG-B の改修完了後、以下のテスト実行コマンドにより **56件すべてのテストが `OK` (100% PASS)** となることを確認してください。

```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
python3 -m unittest tests/test_gemini4_argon_e2e.py
```
**期待される結果**:
```
Ran 56 tests in ~10s
OK
```
