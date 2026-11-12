"""
visual_qa_gate.py - Automated Pre-Delivery Visual Inspection Gate (Gemini 4 Carbon / Multimodal)

Inspects dynamic Instagram Reels (MP4 frames) and OGP cards prior to SNS delivery.
Ensures zero text overlap, no safe-zone clipping, punchy opening hooks (t=1.2s),
and crisp mobile contrast.

Fail-Open Guarantee:
Network timeouts, quota limits, or missing API keys NEVER block live delivery.
"""
import io
import os
import re
import json
import math
import time
import base64
import logging
import datetime
import requests
from typing import Optional, Dict, Any, Tuple

import sns_editorial
import reel_generator

logger = logging.getLogger(__name__)

# Template fallback cycle when QA rejects a reel format.
# Every key AND value must exist in `reel_generator.REEL_TEMPLATES` (no phantom templates).
ALTERNATIVE_REEL_TEMPLATES = {
    "live_top5": "area_battle",
    "area_battle": "ladies_zero_wait",
    "ladies_zero_wait": "live_top5",
    "tiktok_gap_shock": "tiktok_midnight_cliff",
    "tiktok_ladies_real": "tiktok_gap_shock",
    "tiktok_midnight_cliff": "tiktok_ladies_real",
}
ALTERNATIVE_REEL_TEMPLATES = {
    k: v
    for k, v in ALTERNATIVE_REEL_TEMPLATES.items()
    if k in reel_generator.REEL_TEMPLATES and v in reel_generator.REEL_TEMPLATES
}

# Captured at import so monkeypatched single-frame renderers (tests / hotfixes) are still honored
_ORIG_SINGLE_FRAME_RENDERER = getattr(reel_generator, "render_reel_preview_frame_jpeg", None)


def _normalize_recommended_template(candidate, rt: str) -> str:
    """Accept only real `REEL_TEMPLATES` keys from model output; anything else (null, '../evil') -> safe alternative."""
    fallback = ALTERNATIVE_REEL_TEMPLATES.get(rt, "area_battle")
    if not isinstance(candidate, str):
        return fallback
    cand = candidate.strip().lower()
    if cand in reel_generator.REEL_TEMPLATES:
        return cand
    return fallback


def _render_qa_frames(stores: list, rt: str, now_jst) -> Tuple[bytes, bytes]:
    """Render hook (1.2s) + data (6.0s) frames, building the scene renderer only once when possible."""
    single = getattr(reel_generator, "render_reel_preview_frame_jpeg", None)
    multi = getattr(reel_generator, "render_reel_preview_frames_jpeg", None)
    if multi is not None and single is _ORIG_SINGLE_FRAME_RENDERER:
        frames = multi(stores, reel_type=rt, secs=(1.2, 6.0), now_jst=now_jst, quality=80) or []
        if len(frames) >= 2:
            return frames[0], frames[1]
        return (frames[0] if frames else b""), b""
    frame_hook = single(stores, reel_type=rt, sec=1.2, now_jst=now_jst, quality=80)
    frame_data = single(stores, reel_type=rt, sec=6.0, now_jst=now_jst, quality=80)
    return frame_hook, frame_data

QA_MODELS_FALLBACK_CHAIN = (
    "gemini-4-carbon",
    "gemini-4-argon",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
)


MAX_QA_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB memory safety guard


def _coerce_qa_score(val: Any, default: int) -> int:
    """Coerce numeric QA score to 0..100 without turning legitimate 0 into default; non-numeric values raise."""
    if val is None or isinstance(val, bool):
        return default
    fval = float(val)
    if not math.isfinite(fval):
        raise ValueError("non-finite QA score")
    return max(0, min(100, int(round(fval))))


def _safe_fail_open_result(reason: str, recommended_template: str = "") -> Dict[str, Any]:
    """Generate safe, non-blocking default verdict (Fail-Open)."""
    return {
        "pass": True,
        "qa_status": "fail_open_unverified",
        "overall_score": 100,
        "hook_strength": 100,
        "contrast_readability": 100,
        "text_overflow": False,
        "text_overlap": False,
        "brand_compliance": True,
        "issues": [],
        "recommended_template": recommended_template or "live_top5",
        "model_used": "fail_open",
        "reason": reason,
        "latency_sec": 0.0,
    }


def _parse_qa_json_response(raw_text: str) -> Optional[Dict[str, Any]]:
    """Safely parse JSON response from Gemini Multimodal inspection."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    # Strip markdown block if wrapped
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def inspect_reel_visual_quality(
    stores: list,
    reel_type: str = "live_top5",
    now_jst: Optional[datetime.datetime] = None,
    timeout_sec: float = 7.0,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Inspect Reel frames (t=1.2s hook and t=6.0s data scene) using gemini-4-carbon / multimodal Gemini.

    Scores:
      1. text_overflow (safe-zone violations: top/bottom 15% or right-hand icon bar)
      2. text_overlap (unreadable collisions of title, metrics, store cards, bars)
      3. hook_strength (0-100, thumb-stopping power in first 1.5s)
      4. contrast_readability (0-100, text-to-background contrast on mobile)
      5. brand_compliance (strictly official names: 'オリエンタルラウンジ')

    Returns verdict dict. Guaranteed Fail-Open on any error or timeout.
    """
    t0 = time.monotonic()
    rt = reel_generator.normalize_reel_type(reel_type)
    effective_key = (api_key or sns_editorial.get_gemini_api_key(allow_blocking=False) or "").strip()

    if not effective_key:
        res = _safe_fail_open_result("api_key_not_set", recommended_template=rt)
        res["latency_sec"] = round(time.monotonic() - t0, 3)
        return res

    # 1. Render preview frames at t=1.2s (Hook scene) and t=6.0s (Data scene) purely via Pillow
    try:
        frame_hook, frame_data = _render_qa_frames(stores, rt, now_jst)
    except Exception as e:
        logger.warning(f"[visual_qa] Frame extraction failed ({rt}): {e}")
        res = _safe_fail_open_result(f"frame_extract_error: {e}", recommended_template=rt)
        res["latency_sec"] = round(time.monotonic() - t0, 3)
        return res

    if not frame_hook or not frame_data:
        res = _safe_fail_open_result("empty_frames", recommended_template=rt)
        res["latency_sec"] = round(time.monotonic() - t0, 3)
        return res
    if (len(frame_hook) + len(frame_data)) > MAX_QA_IMAGE_BYTES:
        res = _safe_fail_open_result("frames_too_large", recommended_template=rt)
        res["latency_sec"] = round(time.monotonic() - t0, 3)
        return res

    b64_hook = base64.b64encode(frame_hook).decode("ascii")
    b64_data = base64.b64encode(frame_data).decode("ascii")

    tpl_choices_str = " / ".join(reel_generator.REEL_TEMPLATES.keys())
    prompt = (
        "あなたはInstagram Reels / TikTok（9:16縦画面動画）の厳格な視覚QA検閲官です。\n"
        "提供された2枚の動画フレーム（Frame 1: 冒頭1.2秒フック画面、Frame 2: 6.0秒データ比較画面）を検閲してください。\n\n"
        "【検閲基準】\n"
        "1. text_overflow (bool): 上下15%（ヘッダー・キャプション被り）や右側UIアイコン帯に重要テキストがはみ出していないか？\n"
        "2. text_overlap (bool): タイトル、数値、店舗名、グラフ同士が不自然に重なって読めない箇所はないか？\n"
        "3. hook_strength (0-100): 冒頭1.5秒でスマホユーザーの手が止まるインパクト・視線誘導があるか？\n"
        "4. contrast_readability (0-100): 背景写真やネオングローに対して文字が十分クッキリ読めるか？\n"
        "5. brand_compliance (bool): 不正な略称（単体『オリエンタル』等）はなく『オリエンタルラウンジ』等の正式表記か？\n\n"
        "【合否判定ルール】\n"
        "- overall_score >= 70 かつ text_overflow == false かつ text_overlap == false かつ contrast_readability >= 60 かつ brand_compliance == true の場合のみ pass: true\n"
        f"- 不合格の場合は pass: false とし、recommended_template に別テンプレート名（{tpl_choices_str}）を指定してください。\n\n"
        "必ず以下のJSON形式のみで出力してください:\n"
        "{\n"
        '  "pass": true,\n'
        '  "overall_score": 85,\n'
        '  "hook_strength": 80,\n'
        '  "contrast_readability": 90,\n'
        '  "text_overflow": false,\n'
        '  "text_overlap": false,\n'
        '  "brand_compliance": true,\n'
        '  "issues": [],\n'
        f'  "recommended_template": "{rt}"\n'
        "}"
    )

    models_to_try = list(QA_MODELS_FALLBACK_CHAIN)
    last_err = ""

    for m_idx, m_name in enumerate(models_to_try):
        rem_sec = timeout_sec - (time.monotonic() - t0)
        if rem_sec <= 1.0:
            break

        gen_cfg: Dict[str, Any] = {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1024,
        }
        low_m = m_name.lower()
        if "carbon" in low_m or "argon" in low_m or low_m.startswith("gemini-4"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "LOW"}
            gen_cfg["maxOutputTokens"] = 2048

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": "image/jpeg", "data": b64_hook}},
                        {"inlineData": {"mimeType": "image/jpeg", "data": b64_data}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": gen_cfg,
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": effective_key}
        has_fallback_model = m_idx < len(models_to_try) - 1
        call_timeout = (
            min(rem_sec, max(1.6, rem_sec * 0.52))
            if has_fallback_model
            else min(rem_sec, timeout_sec)
        )

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=call_timeout)
            if resp.status_code == 200:
                cands = ((resp.json() or {}).get("candidates")) or []
                if cands:
                    parts = ((cands[0].get("content") or {}).get("parts")) or []
                    raw_txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    parsed = _parse_qa_json_response(raw_txt)
                    if parsed and isinstance(parsed, dict):
                        raw_pass = True if parsed.get("pass") is None else bool(parsed.get("pass"))
                        score = _coerce_qa_score(parsed.get("overall_score"), 85)
                        hook_score = _coerce_qa_score(parsed.get("hook_strength"), 80)
                        contrast = _coerce_qa_score(parsed.get("contrast_readability"), 80)
                        text_overflow = bool(parsed.get("text_overflow", False))
                        text_overlap = bool(parsed.get("text_overlap", False))
                        brand_ok = True if parsed.get("brand_compliance") is None else bool(parsed.get("brand_compliance"))
                        is_pass = bool(
                            raw_pass
                            and score >= 70
                            and contrast >= 60
                            and not text_overflow
                            and not text_overlap
                            and brand_ok
                        )
                        rec_template = _normalize_recommended_template(parsed.get("recommended_template"), rt)
                        return {
                            "pass": is_pass,
                            "qa_status": "inspected",
                            "overall_score": score,
                            "hook_strength": hook_score,
                            "contrast_readability": contrast,
                            "text_overflow": text_overflow,
                            "text_overlap": text_overlap,
                            "brand_compliance": brand_ok,
                            "issues": parsed.get("issues") or [],
                            "recommended_template": rec_template,
                            "model_used": m_name,
                            "reason": "inspected",
                            "latency_sec": round(time.monotonic() - t0, 3),
                        }
                    last_err = f"unparseable_response ({m_name})"
                else:
                    last_err = f"empty_candidates ({m_name})"
            else:
                last_err = f"HTTP {resp.status_code} ({m_name})"
                logger.warning(f"[visual_qa] Gemini API returned HTTP {resp.status_code} ({m_name})")
        except requests.exceptions.Timeout:
            last_err = f"timeout ({m_name})"
            logger.warning(f"[visual_qa] Gemini API timeout ({m_name})")
        except Exception as ex:
            last_err = f"Exception ({m_name}): {ex}"
            logger.warning(f"[visual_qa] Error calling {m_name}: {ex}")

    # If all models timed out, failed, or returned unparseable output: Fail-Open
    logger.info(f"[visual_qa] Fail-open activated for {rt} due to: {last_err or 'timeout'}")
    res = _safe_fail_open_result(f"fail_open_{last_err or 'timeout'}", recommended_template=rt)
    res["latency_sec"] = round(time.monotonic() - t0, 3)
    return res


def inspect_image_visual_quality(
    image_bytes: bytes,
    image_type: str = "ogp",
    timeout_sec: float = 6.0,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Inspect single still image (such as dynamic OGP card) for text overflow, overlap, and contrast.
    Guaranteed Fail-Open.
    """
    t0 = time.monotonic()
    effective_key = (api_key or sns_editorial.get_gemini_api_key(allow_blocking=False) or "").strip()
    if not effective_key or not image_bytes:
        res = _safe_fail_open_result("api_key_or_image_missing")
        res["latency_sec"] = round(time.monotonic() - t0, 3)
        return res
    if len(image_bytes) > MAX_QA_IMAGE_BYTES:
        res = _safe_fail_open_result("image_too_large")
        res["latency_sec"] = round(time.monotonic() - t0, 3)
        return res

    b64_img = base64.b64encode(image_bytes).decode("ascii")
    mime = "image/png" if image_bytes.startswith(b"\x89PNG") else "image/jpeg"

    prompt = (
        f"あなたはSNS OGP画像（1200x630 横長カード、種別: {image_type}）の視覚QA検閲官です。\n"
        "添付されたOGP画像を検閲してください。\n\n"
        "【検閲基準】\n"
        "1. text_overflow (bool): 外枠のマージン（上下左右40px）にテキストがはみ出していないか？\n"
        "2. text_overlap (bool): タイトル、日付バッジ、店舗名、数値が重なっていないか？\n"
        "3. contrast_readability (0-100): 背景グラデーションに対して文字が鮮明に読めるか？\n"
        "4. brand_compliance (bool): 『オリエンタルラウンジ』の正式名称表記を守っているか？\n\n"
        "【合否判定ルール】\n"
        "- overall_score >= 70 かつ text_overflow == false かつ text_overlap == false かつ contrast_readability >= 60 の場合のみ pass: true\n\n"
        "JSON形式のみで出力してください:\n"
        "{\n"
        '  "pass": true,\n'
        '  "overall_score": 90,\n'
        '  "contrast_readability": 90,\n'
        '  "text_overflow": false,\n'
        '  "text_overlap": false,\n'
        '  "brand_compliance": true,\n'
        '  "issues": []\n'
        "}"
    )

    models_to_try = list(QA_MODELS_FALLBACK_CHAIN)
    last_err = ""
    for m_idx, m_name in enumerate(models_to_try):
        rem_sec = timeout_sec - (time.monotonic() - t0)
        if rem_sec <= 1.0:
            break

        gen_cfg: Dict[str, Any] = {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1024,
        }
        low_m = m_name.lower()
        if "carbon" in low_m or "argon" in low_m or low_m.startswith("gemini-4"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "LOW"}
            gen_cfg["maxOutputTokens"] = 2048

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": mime, "data": b64_img}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": gen_cfg,
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": effective_key}
        has_fallback_model = m_idx < len(models_to_try) - 1
        call_timeout = (
            min(rem_sec, max(1.6, rem_sec * 0.52))
            if has_fallback_model
            else min(rem_sec, timeout_sec)
        )

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=call_timeout)
            if resp.status_code == 200:
                cands = ((resp.json() or {}).get("candidates")) or []
                if cands:
                    parts = ((cands[0].get("content") or {}).get("parts")) or []
                    raw_txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    parsed = _parse_qa_json_response(raw_txt)
                    if parsed and isinstance(parsed, dict):
                        raw_pass = True if parsed.get("pass") is None else bool(parsed.get("pass"))
                        score = _coerce_qa_score(parsed.get("overall_score"), 85)
                        contrast = _coerce_qa_score(parsed.get("contrast_readability"), 85)
                        text_overflow = bool(parsed.get("text_overflow", False))
                        text_overlap = bool(parsed.get("text_overlap", False))
                        brand_ok = True if parsed.get("brand_compliance") is None else bool(parsed.get("brand_compliance"))
                        is_pass = bool(
                            raw_pass
                            and score >= 70
                            and contrast >= 60
                            and not text_overflow
                            and not text_overlap
                            and brand_ok
                        )
                        return {
                            "pass": is_pass,
                            "qa_status": "inspected",
                            "overall_score": score,
                            "contrast_readability": contrast,
                            "text_overflow": text_overflow,
                            "text_overlap": text_overlap,
                            "brand_compliance": brand_ok,
                            "issues": parsed.get("issues") or [],
                            "model_used": m_name,
                            "reason": "inspected",
                            "latency_sec": round(time.monotonic() - t0, 3),
                        }
            else:
                last_err = f"HTTP {resp.status_code} ({m_name})"
        except requests.exceptions.Timeout:
            last_err = f"timeout ({m_name})"
        except Exception as ex:
            last_err = f"Exception ({m_name}): {ex}"

    res = _safe_fail_open_result(f"fail_open_{last_err or 'timeout'}")
    res["latency_sec"] = round(time.monotonic() - t0, 3)
    return res


def enforce_reel_visual_qa(
    stores: list,
    reel_type: str = "live_top5",
    now_jst: Optional[datetime.datetime] = None,
    timeout_sec: float = 7.0,
    api_key: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Convenience wrapper that runs Reel visual QA, and if not passing,
    automatically swaps to an alternative template.
    Returns (final_reel_type, qa_result).
    """
    orig_rt = reel_generator.normalize_reel_type(reel_type)
    qa = inspect_reel_visual_quality(
        stores, reel_type=orig_rt, now_jst=now_jst, timeout_sec=timeout_sec, api_key=api_key
    )
    if qa.get("pass", True):
        return orig_rt, qa

    rec_template = _normalize_recommended_template(qa.get("recommended_template"), orig_rt)
    if rec_template == orig_rt:
        rec_template = ALTERNATIVE_REEL_TEMPLATES.get(orig_rt, "area_battle")
        if rec_template == orig_rt:
            rec_template = "live_top5" if orig_rt != "live_top5" else "area_battle"

    logger.warning(
        f"[visual_qa] Reel template {orig_rt} rejected by visual QA (score={qa.get('overall_score')}). "
        f"Switching to alternative template: {rec_template} (issues: {qa.get('issues')})"
    )

    # 2nd-pass inspection on the alternative template to verify re-rendered frame
    try:
        qa2 = inspect_reel_visual_quality(
            stores, reel_type=rec_template, now_jst=now_jst, timeout_sec=max(3.0, timeout_sec / 2), api_key=api_key
        )
        return rec_template, qa2
    except Exception as e:
        logger.warning(f"[visual_qa] 2nd-pass inspection bypassed (fail-open): {e}")
        return rec_template, qa
