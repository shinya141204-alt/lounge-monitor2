"""
ops_governance.py - オーナー運用ガバナンス（緊急非表示スイッチ・お問い合わせ受信箱・スタッフ認証管理・通知Webhook）

全設定・状態は `job_state`（PostgreSQL + Upstash Redis + Google Sheets + ローカルJSON）で永続化され、
`web` ワーカー群と `worker` プロセス間で約1.5秒以内に自動同期されます。
秘密情報（カスタムスタッフパスコード・Webhook URL）は `token:*` プレフィックスにより難読化保存されます。
"""
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse

import job_state
import store_master

BRAND_OPTIONS = [
    {"key": "oriental-lounge", "label": "オリエンタルラウンジ"},
    {"key": "ag", "label": "ag（アグ）"},
    {"key": "jis", "label": "JIS（ジス）"},
    {"key": "asobibar", "label": "ASOBIBAR（アソビバー）"},
    {"key": "yatakoi", "label": "ヤタコイ"},
    {"key": "xix", "label": "相席ラウンジXIX"},
    {"key": "clovers", "label": "相席CLOVERS"},
    {"key": "alfa", "label": "相席ラウンジALFA"},
]
VALID_BRAND_KEYS = {b["key"] for b in BRAND_OPTIONS}
# Google Sheets 1セル 50,000文字上限を確実に下回るよう 30件 × 本文400字に制限
MAX_STORED_INQUIRIES = 30
MAX_INBOX_MESSAGE_CHARS = 400
HIDDEN_MEMO_TTL_SEC = 2.0

_mem_lock = threading.Lock()
_gov_write_lock = threading.Lock()
_unread_memo = {"mono": 0.0, "data": {"unread_total": 0, "unread_priority": 0}}
_hidden_lock = threading.Lock()
_hidden_memo = {"mono": 0.0, "key": None, "gen": 0, "hb": frozenset(), "hs": frozenset()}


def _now_jst() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _now_jst_str() -> str:
    return _now_jst().strftime("%Y-%m-%d %H:%M:%S")


def ops_brand_key_of(store_name: str) -> str:
    """全63店舗を8つのブランド識別子へ正規化して返す。"""
    if not store_name:
        return "other"
    info = store_master.get(store_name)
    if info:
        raw_b = (info.get("brand") or "").strip()
        if raw_b == "OLG":
            return "oriental-lounge"
        if raw_b == "ag":
            return "ag"
        if raw_b == "JIS":
            return "jis"
        if raw_b == "ASOBIBAR":
            return "asobibar"
        if raw_b == "ヤタコイ":
            return "yatakoi"
        if raw_b == "XIX":
            return "xix"
        if raw_b == "CLOVERS":
            return "clovers"
        if raw_b == "ALFA":
            return "alfa"
    s_up = store_name.strip().upper()
    if s_up.startswith("OLG AG") or s_up.startswith("AG "):
        return "ag"
    if s_up.startswith("OLG "):
        return "oriental-lounge"
    if s_up.startswith("JIS "):
        return "jis"
    if s_up.startswith("ASOBIBAR"):
        return "asobibar"
    if "YATAKOI" in s_up or "ヤタコイ" in store_name:
        return "yatakoi"
    if s_up.startswith("XIX"):
        return "xix"
    if s_up.startswith("CLOVERS"):
        return "clovers"
    if s_up.startswith("ALFA"):
        return "alfa"
    return "other"


def _parse_json_list(raw_val) -> list:
    if not raw_val:
        return []
    try:
        parsed = json.loads(raw_val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _compute_hidden_brands_uncached(env_raw: str) -> frozenset:
    out = set()
    if env_raw:
        for tok in env_raw.split(","):
            t = tok.strip().lower()
            if t in VALID_BRAND_KEYS:
                out.add(t)
    stored = _parse_json_list(job_state.get_value("ops:hidden_brands", allow_blocking=False))
    for tok in stored:
        t = str(tok or "").strip().lower()
        if t in VALID_BRAND_KEYS:
            out.add(t)
    return frozenset(out)


def _compute_hidden_stores_uncached(env_raw: str) -> frozenset:
    out = set()
    if env_raw:
        for tok in env_raw.split(","):
            canon = store_master.to_internal_store_name(tok.strip())
            if canon:
                out.add(canon)
    stored = _parse_json_list(job_state.get_value("ops:hidden_stores", allow_blocking=False))
    for tok in stored:
        canon = store_master.to_internal_store_name(str(tok or "").strip())
        if canon:
            out.add(canon)
    return frozenset(out)


def _hidden_memo_key() -> tuple:
    """env 文字列・job_state のキャッシュ世代・JOB_STATE_DIR・get_value 実体（mock差し替え）をキーに含め、
    単体テストで env / ストレージが動的に変わっても古い結果を返さない。"""
    return (
        (os.environ.get("HIDDEN_BRANDS") or "").strip(),
        (os.environ.get("HIDDEN_STORES") or "").strip(),
        getattr(job_state, "_cache_epoch", 0),
        os.environ.get("JOB_STATE_DIR") or "",
        id(getattr(job_state, "get_value", None)),
        id(getattr(store_master, "to_internal_store_name", None)),
    )


def invalidate_governance_cache() -> None:
    """非表示設定のメモリキャッシュを即時無効化する（設定変更時・テスト用）。"""
    with _hidden_lock:
        _hidden_memo["mono"] = 0.0
        _hidden_memo["key"] = None
        _hidden_memo["gen"] = _hidden_memo.get("gen", 0) + 1


def _get_hidden_sets() -> tuple:
    """(hidden_brands, hidden_stores) を 2.0秒 TTL でメモ化して返す（毎回の job_state スキャンを排除）。"""
    key = _hidden_memo_key()
    now_m = time.monotonic()
    with _hidden_lock:
        if (
            _hidden_memo["mono"] > 0
            and _hidden_memo["key"] == key
            and (now_m - _hidden_memo["mono"]) < HIDDEN_MEMO_TTL_SEC
        ):
            return _hidden_memo["hb"], _hidden_memo["hs"]
        gen_before = _hidden_memo.get("gen", 0)
    hb = _compute_hidden_brands_uncached(key[0])
    hs = _compute_hidden_stores_uncached(key[1])
    with _hidden_lock:
        # 計算中に無効化（設定変更）が入った場合は古い結果を保存しない
        if _hidden_memo.get("gen", 0) == gen_before:
            _hidden_memo.update({"mono": now_m, "key": key, "hb": hb, "hs": hs})
    return hb, hs


def get_hidden_brands() -> set:
    """非表示指定中のブランドキー集合（管理画面設定 ＋ 環境変数 `HIDDEN_BRANDS`）。"""
    return set(_get_hidden_sets()[0])


def get_hidden_stores() -> set:
    """非表示指定中の個別店舗キー集合（管理画面設定 ＋ 環境変数 `HIDDEN_STORES`）。"""
    return set(_get_hidden_sets()[1])


def is_brand_hidden(brand_slug: str) -> bool:
    if not brand_slug:
        return False
    return brand_slug.strip().lower() in get_hidden_brands()


def is_store_hidden(store_name: str, hb=None, hs=None) -> bool:
    """`hb` / `hs` を渡すとループ内でも O(1) 判定（省略時は内部メモから取得）。"""
    if not store_name:
        return False
    if hb is None:
        hb = get_hidden_brands()
    if hs is None:
        hs = get_hidden_stores()
    if not hb and not hs:
        return False
    canon = store_master.to_internal_store_name(store_name) or store_name
    if canon in hs or store_name in hs:
        return True
    bkey = ops_brand_key_of(canon)
    return bkey in hb


def filter_visible_stores(stores: list, hb=None, hs=None) -> list:
    """非表示ブランド・非表示店舗をリアルタイム一覧・SNS自動投稿対象から即時除外する。"""
    if not stores:
        return []
    if hb is None:
        hb = get_hidden_brands()
    if hs is None:
        hs = get_hidden_stores()
    if not hb and not hs:
        return list(stores)
    visible = []
    for s in stores:
        sname = (s or {}).get("name") or ""
        if not sname:
            continue
        canon = store_master.to_internal_store_name(sname) or sname
        if canon in hs or sname in hs:
            continue
        if ops_brand_key_of(canon) in hb:
            continue
        visible.append(s)
    return visible


def _purge_http_caches():
    import sys
    app_mod = sys.modules.get("app")
    if app_mod is not None and hasattr(app_mod, "clear_fast_http_caches"):
        try:
            app_mod.clear_fast_http_caches()
        except Exception:
            pass


def set_brand_hidden(brand_key: str, hidden: bool) -> dict:
    bkey = (brand_key or "").strip().lower()
    if bkey not in VALID_BRAND_KEYS:
        return {"ok": False, "error": f"不明なブランド識別子です: {bkey}"}
    with _gov_write_lock:
        cur = set(
            str(x).strip().lower()
            for x in _parse_json_list(job_state.get_value("ops:hidden_brands", allow_blocking=True))
            if str(x).strip().lower() in VALID_BRAND_KEYS
        )
        if hidden:
            cur.add(bkey)
        else:
            cur.discard(bkey)
        job_state.set_value("ops:hidden_brands", json.dumps(sorted(cur), ensure_ascii=False))
        invalidate_governance_cache()
    _purge_http_caches()
    return {"ok": True, "brand_key": bkey, "hidden": bool(hidden), "hidden_brands": sorted(get_hidden_brands())}


def set_store_hidden(store_name_or_slug: str, hidden: bool) -> dict:
    canon = store_master.to_internal_store_name((store_name_or_slug or "").strip())
    master = store_master.load()
    if not canon or (master and canon not in master):
        return {"ok": False, "error": "該当する店舗が見つかりません"}
    with _gov_write_lock:
        cur = set(
            store_master.to_internal_store_name(str(x).strip())
            for x in _parse_json_list(job_state.get_value("ops:hidden_stores", allow_blocking=True))
            if str(x).strip()
        )
        if hidden:
            cur.add(canon)
        else:
            cur.discard(canon)
        job_state.set_value("ops:hidden_stores", json.dumps(sorted(cur), ensure_ascii=False))
        invalidate_governance_cache()
    _purge_http_caches()
    return {
        "ok": True,
        "store_name": canon,
        "seo_name": store_master.seo_name(canon),
        "hidden": bool(hidden),
        "hidden_stores": sorted(get_hidden_stores()),
    }


# =====================================================================
# スタッフ用パスコード変更 ＆ 全スタッフ端末強制ログアウト
# =====================================================================
def get_effective_staff_passcode(fallback_env_passcode: str) -> str:
    override = (job_state.get_value("token:staff_passcode_override", allow_blocking=False) or "").strip()
    return override if override else fallback_env_passcode


def has_custom_staff_passcode() -> bool:
    return bool((job_state.get_value("token:staff_passcode_override", allow_blocking=False) or "").strip())


def get_staff_session_epoch() -> str:
    return str(job_state.get_value("ops:staff_session_epoch", allow_blocking=False) or "0").strip()


def force_logout_all_staff() -> dict:
    """一般スタッフの全ログインセッションを即時失効させる（マスターセッションは維持）。"""
    with _gov_write_lock:
        prev_raw = str(job_state.get_value("ops:staff_session_epoch", allow_blocking=True) or "0").strip()
        try:
            next_num = int(prev_raw.split("_")[0]) + 1
        except Exception:
            next_num = 1
        new_epoch = f"{next_num}_{secrets.token_hex(3)}"
        now_s = _now_jst_str()
        job_state.set_value("ops:staff_session_epoch", new_epoch)
        job_state.set_value("ops:last_staff_force_logout_at", now_s)
    return {"ok": True, "epoch": new_epoch, "revoked_at": now_s}


def update_staff_passcode(new_passcode: str = "", reset_to_env: bool = False) -> dict:
    """スマホ管理画面から一般スタッフ用パスコードを変更（同時に旧セッションを全端末即時失効）。"""
    if reset_to_env:
        job_state.set_value("token:staff_passcode_override", "")
        rev = force_logout_all_staff()
        return {
            "ok": True,
            "has_custom_passcode": False,
            "revoked_at": rev["revoked_at"],
            "message": "一般スタッフ用パスコードを環境変数の初期値に戻し、全スタッフ端末をログアウトしました。",
        }
    cleaned = (new_passcode or "").strip()
    if len(cleaned) < 4 or len(cleaned) > 32:
        return {"ok": False, "error": "パスコードは4文字以上・32文字以内で指定してください。"}
    job_state.set_value("token:staff_passcode_override", cleaned)
    rev = force_logout_all_staff()
    return {
        "ok": True,
        "has_custom_passcode": True,
        "revoked_at": rev["revoked_at"],
        "message": "一般スタッフ用パスコードを更新し、既存の全スタッフ端末を即時ログアウトしました。",
    }


# =====================================================================
# お問い合わせ受信ボックス（削除・訂正依頼は最優先ハイライト）
# =====================================================================
def _invalidate_unread_memo():
    with _mem_lock:
        _unread_memo["mono"] = 0.0


def list_inquiries(limit: int = 50, allow_blocking: bool = False) -> list:
    items = _parse_json_list(job_state.get_value("ops:inquiries", allow_blocking=allow_blocking))
    valid = [it for it in items if isinstance(it, dict) and it.get("id")]
    return valid[: max(1, int(limit))]


def mask_email(email: str) -> str:
    """`alice@example.com` -> `a***@example.com`（受信箱・job_state には生メールを保持しない）。"""
    e = (email or "").strip()
    if not e:
        return ""
    if "@" not in e:
        return "***"
    local, _, domain = e.rpartition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain[:100]}"


def _pii_hash_secret() -> bytes:
    import sys
    app_mod = sys.modules.get("app")
    sk = getattr(getattr(app_mod, "app", None), "secret_key", None) if app_mod is not None else None
    sk = sk or os.environ.get("SECRET_KEY") or "aiseki-navi-inquiry-pii"
    return str(sk).encode("utf-8")


def hash_email(email: str) -> str:
    """同一送信者の重複判定用 HMAC-SHA256（復元不可）。"""
    e = (email or "").strip().lower()
    if not e:
        return ""
    return hmac.new(_pii_hash_secret(), f"inquiry_email:{e}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _scrub_legacy_inquiry(it: dict) -> dict:
    """旧形式（生メール・600字本文）の保存済みエントリを書き込み時にマスク・切り詰めする。"""
    out = dict(it)
    em = str(out.get("email") or "")
    if em and "***" not in em:
        out["email"] = mask_email(em)
        out["email_hash"] = hash_email(em)
        out["has_email"] = True
    out["message"] = str(out.get("message") or "")[:MAX_INBOX_MESSAGE_CHARS]
    return out


def record_inquiry(data: dict, received_at: str, ip_hash: str, category_label: str = "") -> dict:
    """新規お問い合わせを管理画面受信箱へ記録する（最新30件・本文400字・メールはマスク＋HMACのみ保持）。
    生メールアドレスは Google Sheets の `Contact Inquiries` 専用シート側にのみ保存される。"""
    cat = str((data or {}).get("category") or "other").strip()
    is_prio = cat in ("removal", "correction")
    raw_email = str((data or {}).get("email") or "")[:120]
    entry = {
        "id": f"inq_{_now_jst().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}",
        "received_at": received_at or _now_jst_str(),
        "category": cat,
        "category_label": category_label or cat,
        "is_priority": is_prio,
        "name": str((data or {}).get("name") or "")[:60],
        "email": mask_email(raw_email),
        "email_hash": hash_email(raw_email),
        "has_email": bool(raw_email.strip()),
        "message": str((data or {}).get("message") or "")[:MAX_INBOX_MESSAGE_CHARS],
        "ip_hash": str(ip_hash or "")[:16],
        "resolved": False,
        "resolved_at": "",
    }
    with _gov_write_lock:
        existing = [_scrub_legacy_inquiry(it) for it in list_inquiries(limit=MAX_STORED_INQUIRIES, allow_blocking=True)]
        updated = [entry] + existing[: MAX_STORED_INQUIRIES - 1]
        job_state.set_value("ops:inquiries", json.dumps(updated, ensure_ascii=False))
    _invalidate_unread_memo()
    return entry


def set_inquiry_resolved(inquiry_id: str, resolved: bool = True) -> dict:
    target_id = (inquiry_id or "").strip()
    if not target_id:
        return {"ok": False, "error": "お問い合わせIDが指定されていません"}
    with _gov_write_lock:
        items = list_inquiries(limit=MAX_STORED_INQUIRIES, allow_blocking=True)
        found = False
        now_s = _now_jst_str()
        for it in items:
            if it.get("id") == target_id:
                it["resolved"] = bool(resolved)
                it["resolved_at"] = now_s if resolved else ""
                found = True
                break
        if not found:
            return {"ok": False, "error": "該当するお問い合わせが見つかりません"}
        job_state.set_value("ops:inquiries", json.dumps(items, ensure_ascii=False))
    _invalidate_unread_memo()
    return {"ok": True, "id": target_id, "resolved": bool(resolved), **get_unread_counts(force=True)}


def get_unread_counts(force: bool = False) -> dict:
    now_m = time.monotonic()
    if not force:
        with _mem_lock:
            if _unread_memo["mono"] > 0 and (now_m - _unread_memo["mono"] < 2.0):
                return dict(_unread_memo["data"])
    items = list_inquiries(limit=MAX_STORED_INQUIRIES, allow_blocking=False)
    unread_total = sum(1 for it in items if not it.get("resolved"))
    unread_prio = sum(1 for it in items if not it.get("resolved") and it.get("is_priority"))
    res = {"unread_total": unread_total, "unread_priority": unread_prio}
    with _mem_lock:
        _unread_memo["mono"] = now_m
        _unread_memo["data"] = dict(res)
    return res


# =====================================================================
# 障害・お問い合わせ通知用 Webhook URL 設定（SSRF防止バリデーション付き）
# =====================================================================
def _validate_public_https_webhook(url: str) -> str:
    """https スキームかつプライベート/ループバック宛てでない公開 Webhook URL かを検証する。"""
    u = (url or "").strip()
    if not u:
        return ""
    if len(u) > 512:
        return "URLが長すぎます（512文字以内）。"
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme.lower() != "https":
        return "セキュリティ保護のため https:// で始まるURLのみ登録できます。"
    if parsed.username or parsed.password:
        return "認証情報（ユーザー名・パスワード）を含むURLは登録できません。"
    try:
        port = parsed.port
    except ValueError:
        return "不正なポート番号が指定されています。"
    if port not in (None, 443):
        return "セキュリティ保護のため標準HTTPSポート（443）以外のURLは登録できません。"
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
        return "ローカルまたは内部ホスト宛てのURLは登録できません。"
    try:
        ip_obj = ipaddress.ip_address(host)
        if not ip_obj.is_global:
            return "プライベートIPアドレスまたは予約済みアドレス宛てのURLは登録できません。"
    except ValueError:
        if "." not in host:
            return "有効なドメイン名を含む https:// URLを指定してください。"
        # Fail-closed: 名前解決できないホストは登録・送信させない（DNS 一時障害を突いた検証すり抜け防止）
        try:
            addr_infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return "ホスト名を名前解決できませんでした。URLをご確認のうえ、時間をおいて再度お試しください。"
        except Exception:
            return "ホスト名の検証中にエラーが発生しました。時間をおいて再度お試しください。"
        if not addr_infos:
            return "ホスト名を名前解決できませんでした。URLをご確認ください。"
        for info in addr_infos:
            sockaddr = info[4] if len(info) > 4 else ()
            ip_str = sockaddr[0] if sockaddr else ""
            if not ip_str:
                continue
            try:
                resolved_ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
                if not resolved_ip.is_global:
                    return "内部ネットワークまたはループバックへ解決されるホストは登録できません。"
            except ValueError:
                return "不正なIPアドレス解決結果を含むホストは登録できません。"
    return ""


def get_effective_webhook_url() -> str:
    override = (job_state.get_value("token:alert_webhook_url", allow_blocking=False) or "").strip()
    if override:
        return override
    return (os.environ.get("ALERT_WEBHOOK_URL") or "").strip()


def mask_webhook_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urllib.parse.urlparse(u)
    path = parsed.path or ""
    tail = path[-6:] if len(path) > 12 else ""
    return f"https://{parsed.hostname}/...{tail}" if tail else f"https://{parsed.hostname}/***"


def set_webhook_url(url: str) -> dict:
    cleaned = (url or "").strip()
    err = _validate_public_https_webhook(cleaned)
    if err:
        return {"ok": False, "error": err}
    job_state.set_value("token:alert_webhook_url", cleaned)
    eff = get_effective_webhook_url()
    return {
        "ok": True,
        "configured": bool(eff),
        "masked_url": mask_webhook_url(eff),
        "message": "通知用Webhook URLを保存しました。" if cleaned else "管理画面のWebhook URL設定を解除しました。",
    }


def get_governance_dashboard_state() -> dict:
    """マスター管理画面描画用のガバナンス状態一式を返す。"""
    master = store_master.load()
    hb = get_hidden_brands()
    hs = get_hidden_stores()

    brand_counts = {}
    stores_list = []
    for sname, info in master.items():
        bkey = ops_brand_key_of(sname)
        brand_counts[bkey] = brand_counts.get(bkey, 0) + 1
        store_direct_hidden = sname in hs
        brand_hidden = bkey in hb
        stores_list.append({
            "name": sname,
            "seo_name": store_master.seo_name(sname),
            "slug": store_master.slug_of(sname),
            "brand_key": bkey,
            "area_label": info.get("area_label") or "",
            "is_direct_hidden": store_direct_hidden,
            "is_effective_hidden": store_direct_hidden or brand_hidden,
        })

    brands_state = [
        {
            "key": b["key"],
            "label": b["label"],
            "store_count": brand_counts.get(b["key"], 0),
            "is_hidden": b["key"] in hb,
        }
        for b in BRAND_OPTIONS
    ]

    eff_webhook = get_effective_webhook_url()
    inquiries = list_inquiries(limit=MAX_STORED_INQUIRIES, allow_blocking=False)
    unread = get_unread_counts(force=True)
    return {
        "brands": brands_state,
        "stores": stores_list,
        "hidden_brand_count": len(hb),
        "hidden_store_count": len(hs),
        "effective_hidden_store_count": sum(1 for s in stores_list if s["is_effective_hidden"]),
        "has_custom_staff_passcode": has_custom_staff_passcode(),
        "staff_session_epoch": get_staff_session_epoch(),
        "last_staff_force_logout_at": str(job_state.get_value("ops:last_staff_force_logout_at", allow_blocking=False) or ""),
        "webhook_configured": bool(eff_webhook),
        "webhook_masked": mask_webhook_url(eff_webhook),
        "webhook_from_ui": bool((job_state.get_value("token:alert_webhook_url", allow_blocking=False) or "").strip()),
        "inquiries": inquiries,
        **unread,
    }
