"""
affiliate_manager.py - Centralized Affiliate Slot Routing, Override Persistence & Cannibalization Detector.

Solves URL overlap across distinct user intents (e.g. 'immediate tonight drink app' vs 'screened 1-on-1 cafe date')
and allows the owner to update or test ASP destination URLs directly from `/staff/analytics` with zero redeploy.

Priority order for `/link/<item_id>`:
  1. Runtime override saved via `/staff/analytics` (persisted in `job_state` across Render redeploys, 0ms RAM cache)
  2. Render environment variables (`AFFILIATE_URL_*`)
  3. Built-in fallback URLs
"""

import json
import os
import threading
import urllib.parse

import job_state

AFFILIATE_OVERRIDES_STATE_KEY = "aff_slot_overrides"

_lock = threading.Lock()
_mem_overrides = None  # dict[slot_id, {"url": str, "memo": str, "updated_at": str}]

# Canonical 8 affiliate slots (5 men's slots + 3 women's slots) aligned with site UI & reader intent
AFFILIATE_SLOTS = [
    {
        "slot_id": "dating_instant",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["dating", "dating_casual"],
        "category_label": "🍻 即時合流・定番恋活アプリ枠",
        "title": "今日これから・近くで飲める恋活アプリ（満席・男余り時の受け皿）",
        "placement": "トップPR枠 / コラム記事末尾（「近くで今日飲めるアプリ」） / 身だしなみガイド①",
        "recommended_asp_examples": "タップル / ペアーズ / Dine（ダイン） / with 等の即時マッチング系案件",
        "env_keys": ["AFFILIATE_URL_DATING_INSTANT", "AFFILIATE_URL_DATING_CASUAL", "AFFILIATE_URL_DATING"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9",
    },
    {
        "slot_id": "dating_bachelor",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": [],
        "category_label": "🥂 審査制・1対1カフェデート枠",
        "title": "メッセージ不要・週1回デートが決まる審査制アプリ（バチェラーデート等）",
        "placement": "おすすめツールモーダル / 身だしなみガイド②（1対1・メッセージ不要訴求）",
        "recommended_asp_examples": "バチェラーデート / 東カレデート 等の審査制・ハイクラス系案件",
        "env_keys": ["AFFILIATE_URL_DATING_BACHELOR", "AFFILIATE_URL_DATING"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9",
    },
    {
        "slot_id": "mens_hair_removal",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["hair_removal", "hair_removal_trial"],
        "category_label": "🪒 メンズ医療ヒゲ脱毛枠",
        "title": "仕事帰り・距離70cmの青ヒゲ対策（メンズ医療ヒゲ脱毛 初回プラン）",
        "placement": "トップPR枠 / 最初の10分・美容エリア・男性先行コラム / 身だしなみガイド",
        "recommended_asp_examples": "ゴリラクリニック / メンズエミナル / 湘南美容クリニックヒゲ脱毛 等",
        "env_keys": ["AFFILIATE_URL_HAIR_REMOVAL", "AFFILIATE_URL_HAIR_REMOVAL_TRIAL"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D",
    },
    {
        "slot_id": "mens_skincare_clinic",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["orbis"],
        "category_label": "🧴 メンズスキンケア枠",
        "title": "夕方のテカリ・乾燥ケア（ORBIS Mr. トライアルセット等）",
        "placement": "最初の10分・美容エリアコラム / 身だしなみガイド / おすすめモーダル",
        "recommended_asp_examples": "ORBIS Mr.（オルビス ミスター） / バルクオム 初回トライアル等",
        "env_keys": ["AFFILIATE_URL_ORBIS"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX",
    },
    {
        "slot_id": "spu_styleup",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["spu"],
        "category_label": "👔 メンズ服・コーデ定期便枠",
        "title": "相席で浮かないきれいめ私服（SPU スタイルアップ便等）",
        "placement": "身だしなみガイド / おすすめモーダル（服装・身だしなみタブ）",
        "recommended_asp_examples": "SPU スタイルアップ便 / UWear（ユーウェア） 等",
        "env_keys": ["AFFILIATE_URL_SPU"],
        "default_url": "https://h.accesstrade.net/sp/cc?rk=0100oo8500p144",
    },
    {
        "slot_id": "women_dating_bachelor",
        "audience": "women",
        "audience_label": "💄 女性向け",
        "aliases": ["women_dating"],
        "category_label": "🥂 女性向け｜審査制・完全無料ハイクラスデート枠",
        "title": "【女性完全無料】顔写真非公開・審査制で週1回ハイスペック男性と1対1カフェデート",
        "placement": "/for-women 女性特設ガイド / トップ『💄女性目線モード』おすすめモーダル",
        "recommended_asp_examples": "バチェラーデート（女性無料登録案件） / 東カレデート / ゴージャス 等",
        "env_keys": ["AFFILIATE_URL_WOMEN_DATING", "AFFILIATE_URL_DATING_BACHELOR", "AFFILIATE_URL_DATING"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9",
    },
    {
        "slot_id": "women_beauty_clinic",
        "audience": "women",
        "audience_label": "💄 女性向け",
        "aliases": ["women_clinic", "women_hair_removal"],
        "category_label": "💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠",
        "title": "ラウンジの間接照明でも映える水光肌・医療脱毛（初回カウンセリング＆体験プラン）",
        "placement": "/for-women 女性特設ガイド / トップ『💄女性目線モード』おすすめモーダル",
        "recommended_asp_examples": "湘南美容クリニック（美肌/医療脱毛） / エミナルクリニック / 品川美容外科 等",
        "env_keys": ["AFFILIATE_URL_WOMEN_BEAUTY_CLINIC", "AFFILIATE_URL_WOMEN_CLINIC"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D",
    },
    {
        "slot_id": "women_cosme_salon",
        "audience": "women",
        "audience_label": "💄 女性向け",
        "aliases": ["women_cosme", "women_skincare"],
        "category_label": "🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠",
        "title": "夜遊び帰りのメイク崩れ・乾燥リセット（人気スキンケア・ヘアケア初回セット・サロン予約）",
        "placement": "/for-women 女性特設ガイド / トップ『💄女性目線モード』おすすめモーダル",
        "recommended_asp_examples": "サロン専売ヘアケア・高保湿スキンケア初回トライアル / ネイル・ヘアサロン予約 等",
        "env_keys": ["AFFILIATE_URL_WOMEN_COSME"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BERA3+6J68QA+5GZ4+5YZ75",
    },
]

_SLOT_BY_ID = {s["slot_id"]: s for s in AFFILIATE_SLOTS}
_ALIAS_TO_CANONICAL = {}
for _s in AFFILIATE_SLOTS:
    _ALIAS_TO_CANONICAL[_s["slot_id"]] = _s["slot_id"]
    for _al in _s["aliases"]:
        _ALIAS_TO_CANONICAL[_al] = _s["slot_id"]


def canonical_slot_id(item_id: str) -> str:
    clean = (item_id or "").strip().lower()
    return _ALIAS_TO_CANONICAL.get(clean, "")


def validate_destination_url(raw_url: str) -> tuple:
    """Validate that an affiliate destination URL uses http/https and has a valid network host."""
    u = (raw_url or "").strip()
    if not u:
        return True, ""
    if len(u) > 1000 or any(ord(c) < 32 for c in u):
        return False, "URLに不正な制御文字が含まれているか長すぎます。"
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or "." not in parsed.netloc:
        return False, "https:// または http:// から始まる有効なASPリンクを入力してください。"
    return True, u


def _load_overrides(allow_blocking: bool = False) -> dict:
    global _mem_overrides
    with _lock:
        if _mem_overrides is not None:
            return dict(_mem_overrides)
    raw = job_state.get_value(AFFILIATE_OVERRIDES_STATE_KEY, allow_blocking=allow_blocking, default="") or ""
    parsed = {}
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                parsed = data
        except Exception:
            parsed = {}
    if raw or getattr(job_state, "_sheets_loaded", False) or job_state.sheets_store.is_disabled():
        with _lock:
            _mem_overrides = dict(parsed)
    return parsed


def reset_cache_for_tests():
    global _mem_overrides
    with _lock:
        _mem_overrides = None


def _resolve_slot_info(slot_meta: dict, overrides: dict) -> dict:
    sid = slot_meta["slot_id"]
    ov = overrides.get(sid) if isinstance(overrides.get(sid), dict) else {}
    ov_url = (ov.get("url") or "").strip()
    ov_memo = (ov.get("memo") or "").strip()
    ov_updated = (ov.get("updated_at") or "").strip()

    if ov_url:
        return {
            "url": ov_url,
            "source": "override",
            "source_label": "🟢 管理画面カスタム設定",
            "memo": ov_memo,
            "updated_at": ov_updated,
        }

    # Check primary dedicated env var first (index 0) before fallback shared env var
    for idx, env_k in enumerate(slot_meta["env_keys"]):
        env_val = (os.environ.get(env_k) or "").strip()
        if env_val:
            return {
                "url": env_val,
                "source": "env" if idx == 0 else "env_fallback",
                "source_label": f"🔵 環境変数 ({env_k})",
                "memo": ov_memo,
                "updated_at": ov_updated,
            }

    return {
        "url": slot_meta["default_url"],
        "source": "default",
        "source_label": "⚪ 初期デフォルトURL",
        "memo": ov_memo,
        "updated_at": ov_updated,
    }


def resolve_affiliate_url(item_id: str, allow_blocking: bool = False) -> str:
    """Resolve `/link/<item_id>` to its active destination URL in <0.05ms."""
    cid = canonical_slot_id(item_id)
    if not cid:
        return ""
    slot_meta = _SLOT_BY_ID[cid]
    overrides = _load_overrides(allow_blocking=allow_blocking)
    return _resolve_slot_info(slot_meta, overrides)["url"]


def set_affiliate_slot_override(slot_id: str, target_url: str, memo: str = "") -> dict:
    """Save or reset (when target_url is empty) a destination URL override for a canonical slot."""
    import datetime
    cid = canonical_slot_id(slot_id)
    if not cid:
        return {"ok": False, "error": f"不明なアフィリエイト枠IDです: {slot_id}"}
    ok_url, cleaned_url_or_err = validate_destination_url(target_url)
    if not ok_url:
        return {"ok": False, "error": cleaned_url_or_err}

    cleaned_memo = str(memo or "").strip()[:80]
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")

    overrides = _load_overrides(allow_blocking=True)
    if cleaned_url_or_err:
        overrides[cid] = {
            "url": cleaned_url_or_err,
            "memo": cleaned_memo,
            "updated_at": now_str,
        }
    else:
        if cleaned_memo:
            overrides[cid] = {"url": "", "memo": cleaned_memo, "updated_at": now_str}
        else:
            overrides.pop(cid, None)

    with _lock:
        global _mem_overrides
        _mem_overrides = dict(overrides)
    job_state.set_value(AFFILIATE_OVERRIDES_STATE_KEY, json.dumps(overrides, ensure_ascii=False))

    slot_meta = _SLOT_BY_ID[cid]
    resolved = _resolve_slot_info(slot_meta, overrides)
    return {
        "ok": True,
        "slot_id": cid,
        "resolved": resolved,
        "message": (
            f"『{slot_meta['category_label']}』の遷移先URLを更新しました（再デプロイ不要・即時反映済み）！"
            if cleaned_url_or_err
            else f"『{slot_meta['category_label']}』をデフォルトURLへリセットしました。"
        ),
    }


def get_affiliate_slots_status(analytics_data: dict = None, allow_blocking: bool = False) -> dict:
    """
    Build complete status table for `/staff/analytics`:
      - Current resolved URL, source badge, memo, and period click count (summing aliases)
      - Automatic detection when two distinct slots share the exact same URL (cannibalization alert)
    """
    overrides = _load_overrides(allow_blocking=allow_blocking)

    # Aggregate clicks per canonical slot from `analytics.summary()` raw `by_label['affiliate_click']`
    slot_clicks = {s["slot_id"]: 0 for s in AFFILIATE_SLOTS}
    if isinstance(analytics_data, dict):
        raw_aff_pairs = (analytics_data.get("by_label") or {}).get("affiliate_click") or []
        pair_iter = raw_aff_pairs.items() if isinstance(raw_aff_pairs, dict) else raw_aff_pairs
        for raw_lbl, cnt in pair_iter:
            item_part = str(raw_lbl).split("@", 1)[0].strip().lower()
            cid = canonical_slot_id(item_part)
            if cid in slot_clicks:
                slot_clicks[cid] += int(cnt or 0)

    resolved_map = {s["slot_id"]: _resolve_slot_info(s, overrides) for s in AFFILIATE_SLOTS}
    # Group duplicate detection by (audience, url) so men's vs women's funnels don't trigger false cross-gender alerts,
    # while still catching any URL cannibalization inside the men's 5 slots or inside the women's 3 slots!
    url_to_slots = {}
    for s in AFFILIATE_SLOTS:
        sid = s["slot_id"]
        aud = s.get("audience") or "men"
        u = resolved_map[sid]["url"]
        url_to_slots.setdefault((aud, u), []).append(sid)

    slots_out = []
    duplicate_pair_count = 0
    for s in AFFILIATE_SLOTS:
        sid = s["slot_id"]
        aud = s.get("audience") or "men"
        rinfo = resolved_map[sid]
        shared_with = [other for other in url_to_slots.get((aud, rinfo["url"]), []) if other != sid]
        is_dup = len(shared_with) > 0
        if is_dup:
            duplicate_pair_count += 1
        shared_labels = [_SLOT_BY_ID[o]["category_label"] for o in shared_with]
        slots_out.append({
            "slot_id": sid,
            "audience": aud,
            "audience_label": s.get("audience_label") or ("💄 女性向け" if aud == "women" else "🕺 男性向け"),
            "aliases": s["aliases"],
            "test_path": f"/link/{sid}",
            "category_label": s["category_label"],
            "title": s["title"],
            "placement": s["placement"],
            "recommended_asp_examples": s["recommended_asp_examples"],
            "active_url": rinfo["url"],
            "has_override": rinfo["source"] == "override",
            "override_url": rinfo["url"] if rinfo["source"] == "override" else "",
            "source": rinfo["source"],
            "source_label": rinfo["source_label"],
            "memo": rinfo["memo"],
            "updated_at": rinfo["updated_at"],
            "clicks": slot_clicks.get(sid, 0),
            "is_duplicate_url": is_dup,
            "shared_with_labels": shared_labels,
        })

    return {
        "slots": slots_out,
        "total_slots": len(slots_out),
        "has_duplicates": duplicate_pair_count > 0,
        "duplicate_slot_count": duplicate_pair_count,
    }
