# 相席ナビ (AISEKI NAVI) テストスイート準備完了報告 (TEST_READY.md)

## 1. 概要
相席ナビ（AISEKI NAVI）の要求仕様書（`ORIGINAL_REQUEST.md`: R1〜R5）およびプロジェクト仕様書（`PROJECT.md`: F1.1〜F4.3）に基づき、実装から独立した網羅的かつ不透明ボックス（Opaque-box）な **4階層E2Eテストスイート (`tests/test_clean_architecture_e2e.py`)** の設計・実装・配置が完了しました。

既存全228件の単体テストを一切破壊・リグレッションさせることなく、新たに16件の厳格な検証テストを追加し、総テスト数は **244件** となりました。

---

## 2. テスト実行コマンド

### 2.1 新設 E2E テストスイート単体実行
```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター
python3 -m unittest tests/test_clean_architecture_e2e.py
```

### 2.2 全テストスイート一括実行 (回帰検証・ゲート通過確認)
```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター
python3 -m unittest discover -s tests -p "test_*.py"
```

---

## 3. テスト実行結果サマリー (Testing Track 現況)

### 3.1 全体結果
- **総テスト実行数**: 244件 (既存 228件 + 新設 16件)
- **既存テスト通過率**: **100% PASS** (228 / 228 tests, リグレッション 0件)
- **新設E2Eテスト通過状況**:
  - **PASS (11件)**:
    - Tier 1: `scripts/legacy` 削除確認、デッド画像4点削除確認、.gitignore登録確認、公開UIブランド表記遵守確認、GCP有料SDKゼロ依存確認、calm.css縦スクロール維持確認
    - Tier 2: local_db GViz通信障害時の0件クリーンフォールバック確認
    - Tier 3: スクレイピング障害時の直近DBスナップショット補完による店舗非消失確認、ServiceWorkerプリキャッシュ実体整合性確認
    - Tier 4: 主要公開エンドポイント13系統の200疎通確認、404エラーハンドラ契約確認
  - **RED (5件)**:
    - 後続マイルストーン（M2: Subsystem Bug Fixes & Hardening、M3: Cache-Busting Sync）で改修されるべき潜在バグ・バージョン不整合を正確に検出中。

---

## 4. エスカレーション事項（発見された実装バグ一覧）

実装担当ワーカー（worker_r2_m2, worker_r2_m3）が改修すべき項目として以下を報告・エスカレーションします：

### 4.1 【M2改修対象】`x_poster.py` の `None` 値起因 `TypeError` (F2.1)
- **検出テスト**: `Tier2BoundaryAndCornerCasesTests.test_01_x_poster_none_and_empty_headcount_robustness`
- **エラー**: `TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'`
- **発生箇所**: `x_poster.py:271` (`tot = w + m`)
- **改修案**: `w = int(s.get("women") or 0)`, `m = int(s.get("men") or 0)` への防御的キャスト。

### 4.2 【M2改修対象】`sns_workflows.py` の `None` 値起因加算エラー (F2.2)
- **検出テスト**: `Tier2BoundaryAndCornerCasesTests.test_02_sns_workflows_none_and_empty_robustness`
- **エラー**: `TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'`
- **発生箇所**: `sns_workflows.py:83` (`tot = w + m`)
- **改修案**: `w = int(s.get('women') or 0)`, `m = int(s.get('men') or 0)` への防御的キャスト。

### 4.3 【M2改修対象】`moat_intelligence.py` の空文字型キャスト `ValueError` (F2.3)
- **検出テスト**: `Tier2BoundaryAndCornerCasesTests.test_03_moat_intelligence_diff_empty_and_abnormal_handling`
- **エラー**: `ValueError: invalid literal for int() with base 10: '   '`
- **発生箇所**: `moat_intelligence.py:1356` (`w_d15 = int(women_diff or 0)`)
- **改修案**: `int(women_diff or 0) if str(women_diff).strip() not in ('', 'None') else 0` への防御的キャスト。

### 4.4 【M2改修対象】`local_db.py` のインフライトキー不整合 (F2.4)
- **検出テスト**: `Tier2BoundaryAndCornerCasesTests.test_05_local_db_inflight_key_consistency`
- **エラー**: `AssertionError: False is not true : _trigger_store_backfill_async must use _active_db_key()`
- **発生箇所**: `local_db.py:914` (`inflight_key = (DB_PATH, store_name)`)
- **改修案**: `inflight_key = (_active_db_key(), store_name)` への統一。

### 4.5 【M3改修対象】`style.css` のキャッシュバスティングバージョン不整合 (F3.1)
- **検出テスト**: `Tier1FeatureCoverageTests.test_04_cache_busting_sync_across_templates_and_sw`
- **エラー**: `AssertionError: style.css cache-busting version mismatch`
- **発生箇所**: `templates/guide_*.html` (15ファイルで `v=20261030_1`)、`templates/index.html` (`v=20261030_5`)、`static/sw.js` (`v=20261030_5`)
- **改修案**: 全テンプレートおよび `static/sw.js` の `style.css` 参照を最新の `v=20261030_6` に完全統一。

---

## 5. 合格判定基準 (Acceptance Criteria)

M2 および M3 の実装完了後、以下のコマンドを実行して **244件すべてのテストが `OK` で通過** することを確認することで、本番デプロイ（M4）への移行が承認されます：

```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター && python3 -m unittest discover -s tests -p "test_*.py"
```
**期待される出力**:
```
Ran 244 tests in ~18s
OK
```
