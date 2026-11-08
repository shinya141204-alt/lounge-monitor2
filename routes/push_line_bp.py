"""Blueprint for 0-yen Web Push condition-watch subscriptions and Official LINE Auto-Responder webhook."""

import hashlib
import hmac
import json
import logging
import threading
import time

from flask import Blueprint, jsonify, request, session

import local_db
import push_watch
import store_master
import turbo_store
from routes._bridge import core

logger = logging.getLogger(__name__)

push_line_bp = Blueprint("push_line_bp", __name__)

# 60s in-process memo of the (public, non-secret) VAPID application server key.
# Only non-empty keys are memoized so a cold/unconfigured job_state never pins an empty value.
_VAPID_PUB_MEMO_TTL_SEC = 60.0
_vapid_pub_memo = {"mono": 0.0, "key": "", "db_key": None}
_vapid_pub_memo_lock = threading.Lock()


def _get_vapid_public_key_memoized() -> str:
    now_m = time.monotonic()
    try:
        db_key = local_db._active_db_key()
    except Exception:
        db_key = None
    with _vapid_pub_memo_lock:
        memo_key = _vapid_pub_memo["key"]
        if (
            memo_key
            and _vapid_pub_memo.get("db_key") == db_key
            and (now_m - _vapid_pub_memo["mono"]) < _VAPID_PUB_MEMO_TTL_SEC
        ):
            return memo_key
    pub_key = push_watch.get_vapid_public_key(allow_blocking=True)
    if isinstance(pub_key, str) and pub_key:
        with _vapid_pub_memo_lock:
            _vapid_pub_memo["key"] = pub_key
            _vapid_pub_memo["mono"] = now_m
            _vapid_pub_memo["db_key"] = db_key
    return pub_key


@push_line_bp.route("/api/push/vapid-public-key", methods=["GET"])
def api_push_vapid_public_key():
    pub_key = _get_vapid_public_key_memoized()
    resp = jsonify({"ok": True, "status": "success", "public_key": pub_key, "publicKey": pub_key})
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@push_line_bp.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    client_ip = core.get_client_ip()
    if not turbo_store.check_sliding_rate_limit("push_sub_ip", client_ip, limit=15, window_sec=600):
        return jsonify({"ok": False, "status": "rate_limited", "error": "Too many subscription requests"}), 429

    payload = request.get_json(silent=True) or {}
    raw_store = str(payload.get("store_name") or "").strip()
    raw_slug = str(payload.get("store_slug") or "").strip()
    if not raw_store and raw_slug:
        raw_store = store_master.store_by_slug(raw_slug) or ""
    target_cond = str(payload.get("target_cond") or payload.get("condition_type") or "female_55").strip()
    if not raw_store or len(raw_store) > 80 or target_cond not in ("female_55", "zero_wait", "evening_peak"):
        return jsonify({"ok": False, "status": "error", "error": "Invalid store or condition"}), 400

    import ops_governance
    store_name = store_master.to_internal_store_name(raw_store)
    if ops_governance.is_store_hidden(store_name):
        return jsonify({"ok": False, "status": "error", "error": "Unknown store"}), 400
    with core.data_lock:
        known_names = {s.get("name") for s in (core.latest_data.get("full_data") or [])}
    if known_names:
        if store_name not in known_names and not store_master.get(store_name):
            return jsonify({"ok": False, "status": "error", "error": "Unknown store"}), 400
    elif not store_master.get(store_name):
        return jsonify({"ok": False, "status": "error", "error": "Unknown store"}), 400

    sub_obj = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else payload
    endpoint = str(sub_obj.get("endpoint") or "").strip()
    keys_obj = sub_obj.get("keys") if isinstance(sub_obj.get("keys"), dict) else {}
    p256dh = str(keys_obj.get("p256dh") or "").strip()
    auth = str(keys_obj.get("auth") or "").strip()

    ok_ep, ep_reason = push_watch.validate_push_endpoint(endpoint)
    if not ok_ep:
        return jsonify({"ok": False, "status": "error", "error": f"Invalid endpoint ({ep_reason})"}), 400
    ok_keys, key_reason = push_watch.validate_client_keys(p256dh, auth)
    if not ok_keys:
        return jsonify({"ok": False, "status": "error", "error": f"Invalid encryption keys ({key_reason})"}), 400

    sec_bytes = str(core.app.secret_key).encode("utf-8")
    ip_hash = hmac.new(sec_bytes, f"push_ip:{client_ip}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    is_measurement_exempt = (
        request.args.get("staff_test") == "1"
        or bool(payload.get("staff_test"))
        or bool(session.get("is_staff"))
        or bool(session.get("is_master"))
        or core._is_bot_request()
        or core._is_prefetch_request()
    )

    ok, sub_id = local_db.upsert_web_push_subscription(
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        store_name=store_name,
        target_cond=target_cond,
        ip_hash=ip_hash,
        ttl_days=30,
        record_watch_intent=not is_measurement_exempt,
    )
    if not ok:
        return jsonify({"ok": False, "status": "error", "error": "Subscription persistence failed"}), 500

    return jsonify({
        "ok": True,
        "status": "success",
        "store_name": store_master.seo_name(store_name),
        "target_cond": target_cond,
        "cond_label": push_watch.CONDITION_LABELS_JA.get(target_cond, target_cond),
        "ttl_days": 30,
    })


@push_line_bp.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    client_ip = core.get_client_ip()
    if not turbo_store.check_sliding_rate_limit("push_unsub_ip", client_ip, limit=25, window_sec=600):
        return jsonify({"ok": False, "status": "rate_limited", "error": "Too many requests"}), 429

    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint") or "").strip()
    raw_store = str(payload.get("store_name") or "").strip()
    raw_slug = str(payload.get("store_slug") or "").strip()
    if not raw_store and raw_slug:
        raw_store = store_master.store_by_slug(raw_slug) or ""
    ok_ep, _ = push_watch.validate_push_endpoint(endpoint)
    if not ok_ep:
        return jsonify({"ok": False, "status": "error", "error": "Invalid endpoint"}), 400

    store_name = store_master.to_internal_store_name(raw_store) if raw_store else None
    removed = local_db.delete_web_push_subscription(endpoint=endpoint, store_name=store_name)
    return jsonify({"ok": True, "status": "success", "removed": removed})


@push_line_bp.route("/api/line/webhook", methods=["POST"])
def api_line_webhook():
    if not push_watch.is_line_bot_configured():
        return jsonify({"status": "disabled", "message": "LINE credentials not configured"}), 503

    content_len = request.content_length
    if content_len is not None and content_len > 65536:
        return jsonify({"status": "error", "error": "Payload too large"}), 413
    raw_body = request.stream.read(65537)
    if len(raw_body) > 65536:
        return jsonify({"status": "error", "error": "Payload too large"}), 413

    sig = request.headers.get("X-Line-Signature", "")
    if not push_watch.verify_line_signature(raw_body, sig):
        return jsonify({"status": "error", "error": "Invalid signature"}), 403

    try:
        body_json = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return jsonify({"status": "error", "error": "Malformed JSON"}), 400

    events = body_json.get("events") if isinstance(body_json, dict) else []
    if not isinstance(events, list):
        return jsonify({"status": "ok", "replied": 0})

    core.restore_latest_data_if_empty()
    import ops_governance
    with core.data_lock:
        raw_copy = list(core.latest_data.get("full_data") or [])
        last_updated = str(core.latest_data.get("last_updated") or "")
    live_stores = ops_governance.filter_visible_stores(raw_copy)

    replied = 0
    for ev in events[:3]:
        if not isinstance(ev, dict):
            continue
        reply_token = str(ev.get("replyToken") or "").strip()
        if not reply_token:
            continue
        ev_type = ev.get("type")
        query_text = ""
        if ev_type == "message":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if msg.get("type") == "text":
                query_text = str(msg.get("text") or "").strip()[:80]
            else:
                continue
        elif ev_type == "postback":
            pb = ev.get("postback") if isinstance(ev.get("postback"), dict) else {}
            query_text = str(pb.get("data") or "").strip()[:80]
        elif ev_type == "follow":
            query_text = "ランキング"
        else:
            continue

        reply_msg = push_watch.build_line_auto_reply_text(query_text, live_stores, last_updated=last_updated)
        if push_watch.send_line_reply(reply_token, reply_msg, timeout_sec=3.0):
            replied += 1

    return jsonify({"status": "ok", "replied": replied})
