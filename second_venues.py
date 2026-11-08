# -*- coding: utf-8 -*-
"""
second_venues.py - 2軒目案内（2nd Party Venue Guide）バックエンドユーティリティ

主要エリア（渋谷、新宿、恵比寿、銀座、横浜、難波、心斎橋、梅田、栄、天神、すすきの等）
の厳選夜カフェ・バー・シーシャ・横丁スポットおよび、相席ラウンジ退店後のシチュエーション別Tips
を提供する。未登録エリア向けには動的スマートフォールバック（店舗周辺のGoogle無料インテント検索）
を自動生成する。

【制約遵守】
- Google Cloud 有料API（Places API, Maps SDK等）は一切不使用。無料のGoogle公式インテントURLのみ使用。
- ブランド表記ルール遵守: 単体『オリエンタル』『OLG』を出さず『オリエンタルラウンジ』と表記。
"""

import json
import os
import re
import urllib.parse
from typing import Dict, List, Any, Optional

import store_master

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENUES_JSON_PATH = os.path.join(BASE_DIR, "static", "second_venues.json")
STORE_MASTER_PATH = os.path.join(BASE_DIR, "static", "store_master.json")

_CACHED_MASTER: Optional[Dict[str, Any]] = None
_CACHED_STORE_MASTER: Optional[Dict[str, Any]] = None

AREA_DISPLAY_NAMES = {
    "shibuya": "渋谷・道玄坂",
    "shinjuku": "新宿・歌舞伎町",
    "ebisu": "恵比寿",
    "ginza": "銀座・有楽町",
    "yokohama": "横浜・西口",
    "namba": "道頓堀・難波",
    "shinsaibashi": "心斎橋・アメリカ村",
    "umeda": "梅田・北新地・堂山",
    "sakae": "名古屋・栄・錦",
    "tenjin": "福岡・天神・大名",
    "susukino": "札幌・すすきの",
}


def load_second_venues_master(force_reload: bool = False) -> Dict[str, Any]:
    """static/second_venues.json をロードしてキャッシュする。"""
    global _CACHED_MASTER
    if _CACHED_MASTER is not None and not force_reload:
        return _CACHED_MASTER

    if not os.path.exists(VENUES_JSON_PATH):
        return {"venues_by_area": {}, "situation_tips": [], "timing_rules": []}

    try:
        with open(VENUES_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            _CACHED_MASTER = data
            return data
    except Exception as e:
        print(f"[second_venues] Error loading {VENUES_JSON_PATH}: {e}")
        return {"venues_by_area": {}, "situation_tips": [], "timing_rules": []}


def load_store_master(force_reload: bool = False) -> Dict[str, Any]:
    """static/store_master.json をロードしてキャッシュする。"""
    global _CACHED_STORE_MASTER
    if _CACHED_STORE_MASTER is not None and not force_reload:
        return _CACHED_STORE_MASTER

    if not os.path.exists(STORE_MASTER_PATH):
        return {"stores": {}}

    try:
        with open(STORE_MASTER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            _CACHED_STORE_MASTER = data
            return data
    except Exception as e:
        print(f"[second_venues] Error loading {STORE_MASTER_PATH}: {e}")
        return {"stores": {}}


def get_area_key_for_store(store_name: str) -> Optional[str]:
    """
    店舗名やエリア情報から、対応する2軒目エリアキーを特定する。
    マッチしない場合は None を返す。
    """
    if not store_name:
        return None

    s = store_name.strip()
    s_lower = s.lower()

    # 1. 直接的な部分一致（優先度の高い固有エリア名）
    if "恵比寿" in s or "ebisu" in s_lower:
        return "ebisu"
    if "銀座" in s or "ginza" in s_lower or "有楽町" in s:
        return "ginza"
    if "横浜" in s or "yokohama" in s_lower:
        return "yokohama"
    if "道頓堀" in s or "難波" in s or "なんば" in s or "namba" in s_lower:
        return "namba"
    if "心斎橋" in s or "shinsaibashi" in s_lower:
        return "shinsaibashi"
    if "梅田" in s or "北新地" in s or "umeda" in s_lower:
        return "umeda"
    if "栄" in s or "錦" in s or "sakae" in s_lower:
        return "sakae"
    if "天神" in s or "大名" in s or "tenjin" in s_lower or "中洲" in s or "博多" in s:
        return "tenjin"
    if "すすきの" in s or "susukino" in s_lower or "札幌" in s:
        return "susukino"
    if "渋谷" in s or "shibuya" in s_lower:
        return "shibuya"
    if "新宿" in s or "shinjuku" in s_lower:
        return "shinjuku"

    # 2. store_master.json を参照した判定
    sm = load_store_master()
    stores = sm.get("stores", {})
    store_meta = stores.get(s)
    if not store_meta:
        # 接頭辞違い等の検索（例: 'OLG 渋谷本店' vs '渋谷本店'）
        for k, v in stores.items():
            if s in k or k in s:
                store_meta = v
                break

    if store_meta:
        display_name = store_meta.get("display_name", "")
        area_label = store_meta.get("area_label", "")
        map_anchor = store_meta.get("map_anchor", "")
        combined = f"{display_name} {area_label} {map_anchor}"

        if "恵比寿" in combined:
            return "ebisu"
        if "銀座" in combined or "有楽町" in combined:
            return "ginza"
        if "横浜" in combined:
            return "yokohama"
        if "難波" in combined or "なんば" in combined or "道頓堀" in combined:
            return "namba"
        if "心斎橋" in combined:
            return "shinsaibashi"
        if "梅田" in combined or "北新地" in combined:
            return "umeda"
        if "栄" in combined or "錦" in combined:
            return "sakae"
        if "天神" in combined or "中洲" in combined or "博多" in combined:
            return "tenjin"
        if "すすきの" in combined or "札幌" in combined:
            return "susukino"
        if "渋谷" in combined:
            return "shibuya"
        if "新宿" in combined:
            return "shinjuku"

    return None


def get_store_anchor(store_name: Optional[str]) -> str:
    """店舗名からマップ検索の基準地（anchor）文字列を生成する。"""
    s = (store_name or "").strip()
    if not s:
        return "相席ラウンジ"

    sm = load_store_master()
    stores = sm.get("stores", {})

    meta = stores.get(s)
    if not meta:
        for k, v in stores.items():
            if s in k or k in s:
                meta = v
                break

    if meta and meta.get("map_anchor"):
        return meta["map_anchor"]

    # ブランド名の正式展開（ブランドルール遵守）
    clean_name = s.replace("OLG", "").replace("ag", "").strip()
    if s.startswith("OLG") or "オリエンタルラウンジ" in s:
        brand = "オリエンタルラウンジ"
    elif s.startswith("ag"):
        brand = "ag"
    elif s.startswith("JIS"):
        brand = "JIS"
    else:
        brand = "相席ラウンジ"

    return f"{brand}{clean_name}"


def get_google_maps_intent_url(query: str) -> str:
    """Google公式無料WebインテントURLを生成する（GCP有料APIは一切不使用）。"""
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query.strip())}"


def get_line_share_url(text: str) -> str:
    """LINE共有用のURLスキーマを生成する。"""
    return f"https://line.me/R/msg/text/?{urllib.parse.quote(text.strip())}"


def build_fallback_spots(store_name: str, anchor: str) -> List[Dict[str, Any]]:
    """
    マスター未登録エリア向けのスマートフォールバック（動的クエリ生成）スポット一覧を作成する。
    """
    clean_name = store_name.replace("OLG", "").replace("ag", "").strip()
    return [
        {
            "id": f"fallback-cafe-{clean_name}",
            "name": f"{clean_name}周辺の夜カフェ・スイーツ",
            "category": "cafe",
            "category_label": "夜カフェ",
            "walk_minutes": 3,
            "budget": "¥1,000〜¥2,000",
            "features": ["落ち着いた照明", "甘いもの・スイーツ", "ゆったり会話"],
            "map_query": f"{anchor} 近く 夜カフェ スイーツ カフェバー",
            "comment": "退店後の口直しや落ち着いた会話に最適。Googleマップで現在営業中の近くの夜カフェをワンタップ検索。",
            "is_dynamic": True
        },
        {
            "id": f"fallback-bar-{clean_name}",
            "name": f"{clean_name}周辺の隠れ家バー・バル",
            "category": "bar",
            "category_label": "バー・バル",
            "walk_minutes": 4,
            "budget": "¥2,000〜¥4,000",
            "features": ["カウンター席あり", "落ち着いた大人の空間", "サクッと1杯"],
            "map_query": f"{anchor} 近く 隠れ家バー ワインバル ダイニングバー",
            "comment": "騒がしい店内から横並びのカウンターへ。距離を縮めたい時にすぐ見つかる周辺バー検索。",
            "is_dynamic": True
        },
        {
            "id": f"fallback-shisha-{clean_name}",
            "name": f"{clean_name}周辺のシーシャカフェ",
            "category": "shisha",
            "category_label": "シーシャ",
            "walk_minutes": 3,
            "budget": "¥2,000〜¥3,000",
            "features": ["ソファ席", "リラックス空間", "お酒が弱くても安心"],
            "map_query": f"{anchor} 近く シーシャ カフェ ラウンジ",
            "comment": "ゆったりソファで深く語り合えるシーシャ。深夜営業の店舗を優先してマップ検索。",
            "is_dynamic": True
        },
        {
            "id": f"fallback-yokocho-{clean_name}",
            "name": f"{clean_name}周辺の横丁・大衆バル",
            "category": "yokocho",
            "category_label": "横丁",
            "walk_minutes": 3,
            "budget": "¥2,000〜¥3,500",
            "features": ["はしご酒", "賑やかで活気あり", "カジュアル"],
            "map_query": f"{anchor} 近く 横丁 大衆バル 居酒屋",
            "comment": "もう1杯ワイワイ楽しく飲みたいときに。気軽に入れる周辺のバル・横丁をマップ検索。",
            "is_dynamic": True
        }
    ]


def resolve_store_slug(store_name: Optional[str]) -> Optional[str]:
    """店舗名、表示名、SEO名称から store_master の slug を解決する。"""
    norm = (store_name or "").strip()
    if not norm:
        return None
    # 1. 直接スラッグとして一致する場合
    if norm in store_master.STORE_SLUGS.values():
        return norm
    # 2. STORE_SLUGS のキーとして一致する場合
    if norm in store_master.STORE_SLUGS:
        return store_master.STORE_SLUGS[norm]
    # 3. 前後空白や表記ゆれの吸収
    norm_compact = re.sub(r"[\s　\-_]+", "", norm).lower()
    for k, slug in store_master.STORE_SLUGS.items():
        k_compact = re.sub(r"[\s　\-_]+", "", k).lower()
        slug_compact = re.sub(r"[\s　\-_]+", "", slug).lower()
        seo_k = store_master.seo_name(k) if hasattr(store_master, "seo_name") else k
        seo_compact = re.sub(r"[\s　\-_]+", "", seo_k).lower()
        if (norm_compact == k_compact or norm_compact == slug_compact or norm_compact == seo_compact
            or (len(norm_compact) >= 3 and norm_compact in k_compact)
            or (len(k_compact) >= 3 and k_compact in norm_compact)
            or (len(norm_compact) >= 3 and norm_compact in seo_compact)
            or (len(seo_compact) >= 3 and seo_compact in norm_compact)):
            return slug
    return None


def get_second_venues_for_store(store_name: Optional[str]) -> Dict[str, Any]:
    """
    店舗名から、2軒目のおすすめスポット、シチュエーションTips、タイミングルールを取得する。
    1. venues_by_store（全63店舗実在スポット）から優先取得
    2. 未登録時は venues_by_area を検索
    3. それでもなければスマートフォールバック（動的クエリ生成）を返す。
    """
    norm_name = (store_name or "").strip()
    master = load_second_venues_master()
    venues_by_store = master.get("venues_by_store", {})
    venues_by_area = master.get("venues_by_area", {})
    situation_tips = master.get("situation_tips", [])
    timing_rules = master.get("timing_rules", [])

    slug = resolve_store_slug(norm_name)
    area_key = get_area_key_for_store(norm_name)
    anchor = get_store_anchor(norm_name)

    seo_name = store_master.seo_name(norm_name) if hasattr(store_master, "seo_name") and norm_name else norm_name
    clean_name = norm_name.replace("OLG", "").replace("ag", "").strip() or "相席ラウンジ"

    spots: List[Dict[str, Any]] = []
    is_fallback = False
    is_store_curated = False
    area_name = ""

    # 1. venues_by_store から店舗固有の実在スポットを優先取得
    if slug and slug in venues_by_store and venues_by_store[slug]:
        spots = venues_by_store[slug]
        is_fallback = False
        is_store_curated = True
        area_name = AREA_DISPLAY_NAMES.get(area_key, f"{clean_name}周辺") if area_key else f"{clean_name}周辺"
    elif norm_name in venues_by_store and venues_by_store[norm_name]:
        spots = venues_by_store[norm_name]
        is_fallback = False
        is_store_curated = True
        area_name = AREA_DISPLAY_NAMES.get(area_key, f"{clean_name}周辺") if area_key else f"{clean_name}周辺"
    elif seo_name in venues_by_store and venues_by_store[seo_name]:
        spots = venues_by_store[seo_name]
        is_fallback = False
        is_store_curated = True
        area_name = AREA_DISPLAY_NAMES.get(area_key, f"{clean_name}周辺") if area_key else f"{clean_name}周辺"
    # 2. 既存のエリア別スポット
    elif area_key and area_key in venues_by_area and venues_by_area[area_key]:
        spots = venues_by_area[area_key]
        area_name = AREA_DISPLAY_NAMES.get(area_key, area_key)
        is_fallback = False
        is_store_curated = False
    # 3. 未登録店舗向けスマートフォールバック
    else:
        spots = build_fallback_spots(norm_name, anchor)
        area_name = f"{clean_name}周辺"
        is_fallback = True
        is_store_curated = False

    # 各スポットに Google マップインテント URL、LINE共有URL、予約URLを付与
    from_param = f"/store/{slug}" if slug else "/concierge"
    enriched_spots = []
    for sp in spots:
        sp_copy = dict(sp)
        map_q = sp_copy.get("map_query", f"{sp_copy.get('name', '')} {anchor}")
        sp_copy["map_url"] = get_google_maps_intent_url(map_q)
        w_min = sp_copy.get("walk_minutes", 3)
        sp_copy["walk_minutes_label"] = "同ビル内（徒歩0分）" if w_min == 0 else f"徒歩{w_min}分"
        sp_copy["reservation_url"] = f"/link/second_venue_reservation?from={from_param}"
        sp_copy["line_share_url"] = get_line_share_url(
            f"【2軒目候補】{sp_copy.get('name')}（{sp_copy.get('category_label', '')}・{sp_copy['walk_minutes_label']}）\n{sp_copy['map_url']}"
        )
        enriched_spots.append(sp_copy)

    return {
        "store_name": norm_name,
        "clean_name": clean_name,
        "store_slug": slug,
        "seo_name": seo_name,
        "area_key": area_key,
        "area_name": area_name,
        "anchor": anchor,
        "is_fallback": is_fallback,
        "is_store_curated": is_store_curated,
        "spots": enriched_spots,
        "situation_tips": situation_tips,
        "timing_rules": timing_rules,
    }
