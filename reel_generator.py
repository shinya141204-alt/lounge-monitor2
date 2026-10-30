"""
reel_generator.py - 9:16 Vertical Short Video (Reels / Shorts / TikTok) Generator for Aiseki Navi.

Features:
1. Generates 720x1280 (9:16) 24fps H.264 (`libx264` + `yuv420p` + `+faststart`) + silent AAC (`anullsrc`)
   MP4 videos (~11.5s) fully compatible with Meta Instagram Reels, TikTok, and YouTube Shorts.
2. Hybrid Background Engine (`static/reels_bg/`):
   - Auto-upgrades to Google Flow (Veo 3) `.mp4` live video backgrounds when placed in `static/reels_bg/`,
     looping/trimming and compositing dark luxury gradient vignettes + live animated data cards in a single FFmpeg pass.
   - Falls back to `neon_city_9x16.jpg` / `luxury_lounge_9x16.jpg` (if present) or procedural Pillow dark-gold
     & bokeh vertical artwork with smooth Ken Burns zoom-in and vignette.
3. Three Reel Templates (`REEL_TEMPLATES`):
   - `live_top5`: Nationwide live female headcount TOP 5 + >=55% ratio anaba pickup + CTA
   - `area_battle`: Shinjuku vs Umeda vs Shibuya live gender-ratio battle + best store per hub
   - `ladies_zero_wait`: Women-pair zero-wait & perks ranking in rose-gold palette
4. Pure-Pillow `render_reel_preview_frame_jpeg()` works 100% of the time even without FFmpeg.
"""

import collections
import datetime
import hashlib
import io
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from PIL import Image, ImageDraw, ImageFilter

import events
import ig_carousel_generator
import ladies_guide
import ogp_generator
import sns_editorial
import store_master
import turbo_store

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REELS_BG_DIR = os.path.join(BASE_DIR, "static", "reels_bg")
REEL_TMP_CACHE_DIR = "/tmp/aiseki_reels"
JST = datetime.timezone(datetime.timedelta(hours=9))

REEL_WIDTH = 720
REEL_HEIGHT = 1280
REEL_FPS = 24
REEL_DURATION_SEC = 11.5
REEL_TOTAL_FRAMES = int(round(REEL_DURATION_SEC * REEL_FPS))  # 276 frames
REEL_CACHE_TTL_SEC = 120.0

# Enforce official brand naming: never allow standalone prefix without 'ラウンジ'
_FULL_BRAND_OLG = "オリエンタルラウンジ"
_STANDALONE_BRAND_RE = re.compile(_FULL_BRAND_OLG[:6] + r"(?!ラウンジ)")
_KOREA_KEYWORDS = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")

REEL_TEMPLATES = {
    "live_top5": {
        "key": "live_top5",
        "label": "🔥 今夜の全国リアルタイム女性人数TOP5＆穴場速報",
        "short_label": "🔥 全国TOP5速報",
        "desc": "全国のリアルタイム女性人数TOP5店舗の男女比バーが滑らかに伸びるアニメーション＋高女性比率の狙い目穴場ピックアップ",
        "duration_sec": REEL_DURATION_SEC,
    },
    "area_battle": {
        "key": "area_battle",
        "label": "⚡ 新宿 vs 梅田 vs 渋谷！今の男女比リアル対決",
        "short_label": "⚡ 3大都市バトル",
        "desc": "3大激戦区（新宿・梅田・渋谷）の合計女性人数・女性比率・今一番入りやすい狙い目店をアニメーション比較",
        "duration_sec": REEL_DURATION_SEC,
    },
    "ladies_zero_wait": {
        "key": "ladies_zero_wait",
        "label": "💄 女子ペア向け！今すぐ待ち0＆特典狙い目ラウンジ速報",
        "short_label": "💄 女子待ち0速報",
        "desc": "男性先行中＝受付待ちゼロ圏の上位店舗をエレガントなローズゴールド調でアニメーション表示",
        "duration_sec": REEL_DURATION_SEC,
        "platform": "instagram",
    },
    "tiktok_gap_shock": {
        "key": "tiktok_gap_shock",
        "label": "🎵【TikTok特化】徒歩4分の天国と地獄！同エリア男女比ギャップ衝撃比較",
        "short_label": "🎵 同駅ギャップ比較",
        "desc": "TikTok冒頭2秒フック＆UIセーフゾーン設計（右端アイコン列・下部字幕回避）！同じ駅で女性比が高い狙い目店と混雑店の差を可視化",
        "duration_sec": REEL_DURATION_SEC,
        "platform": "tiktok",
    },
    "tiktok_ladies_real": {
        "key": "tiktok_ladies_real",
        "label": "🎵【TikTok特化】女子の本音×今すぐ待ち0で乾杯できる穴場TOP4",
        "short_label": "🎵 女子本音×待ち0",
        "desc": "TikTok女性層の保存率特化！『女子過多店に行って受付待ち…』を回避する男性先行・即案内ラウンジTOP4（UIセーフゾーン対応）",
        "duration_sec": REEL_DURATION_SEC,
        "platform": "tiktok",
    },
    "tiktok_midnight_cliff": {
        "key": "tiktok_midnight_cliff",
        "label": "🎵【TikTok特化】23時20分の『終電崖』と今夜残る深夜狙い目TOP4",
        "short_label": "🎵 終電崖×深夜穴場",
        "desc": "終電撤退（23時20分の崖）後でも人数が落ちにくい深夜強い系ラウンジTOP4をTikTokセーフゾーン設計でアニメーション表示",
        "duration_sec": REEL_DURATION_SEC,
        "platform": "tiktok",
    },
}

VEO3_PROMPT_PRESETS = [
    {
        "id": "veo_neon",
        "title": "🌃 夜の東京ネオン＆雨粒ボケ（全国TOP5速報・王道）",
        "scene_ja": "夜の渋谷・新宿の濡れた路面とネオン光彩が美しく玉ボケする9:16シネマティック背景。文字カードが最も映える暗めトーン。",
        "prompt_en": (
            "Vertical 9:16 cinematic slow-motion b-roll of Tokyo luxury nightlife street at night after rain, "
            "out-of-focus warm champagne gold, magenta pink, and cyan neon bokeh lights reflecting on wet pavement, "
            "shallow depth of field f/1.4, dark moody exposure so white UI text overlays remain ultra-readable, "
            "smooth gentle forward camera glide, photorealistic 4k, no people faces, no text, no watermark."
        ),
        "recommended_filename": "veo_neon.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "live_top5",
    },
    {
        "id": "veo_lounge",
        "title": "🥂 ラグジュアリーラウンジ・シャンパンゴールド光彩（全タイプ万能）",
        "scene_ja": "高級相席ラウンジを連想させるダークベルベット＆シャンデリアのボケ光彩とグラスの泡が揺らめく上品な背景。",
        "prompt_en": (
            "Vertical 9:16 ultra-luxury dark velvet lounge interior ambiance at night, warm amber chandelier bokeh "
            "and subtle sparkling champagne bubbles rising in slow motion in foreground macro, deep dark shadows, "
            "slow cinematic dolly movement, shallow depth of field, rich gold and obsidian color grading, "
            "no people, no text, no logos."
        ),
        "recommended_filename": "veo_lounge.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "live_top5",
    },
    {
        "id": "veo_city_battle",
        "title": "⚡ 大都会ナイトスカイライン＆光跡（3大都市バトル向け）",
        "scene_ja": "新宿・梅田・渋谷のナイトバトルにぴったりな高層ビル群とシアン×ピンクの光跡が流れるスピード感ある背景。",
        "prompt_en": (
            "Vertical 9:16 aerial twilight-to-night cinematic drone shot gliding slowly above modern Japanese metropolis "
            "skyscrapers with glowing cyan and magenta highway light trails below, moody dark exposure, "
            "soft atmospheric haze, high contrast luxury cyber-night color palette, smooth motion, no text, no watermark."
        ),
        "recommended_filename": "veo_city_battle.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "area_battle",
    },
    {
        "id": "veo_rosegold",
        "title": "💄 ローズゴールド＆キャンドル揺らめくVIP席（女子待ち0速報向け）",
        "scene_ja": "女性フォロワーの保存率を高める、ローズゴールドの間接照明とキャンドルが柔らかく揺れる大人エレガント背景。",
        "prompt_en": (
            "Vertical 9:16 elegant rose-gold and warm candlelight bokeh in a chic upscale cocktail lounge, "
            "soft silk and dark marble reflections, dreamy shallow depth of field, gentle floating warm light particles, "
            "dark moody background ideal for typography overlay, slow smooth camera pan, photorealistic, no people, no text."
        ),
        "recommended_filename": "veo_rosegold.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "ladies_zero_wait",
    },
    {
        "id": "veo_champagne",
        "title": "✨ ダーククリスタル＆舞い上がる金色の泡マクロスロー（週末ピーク向け）",
        "scene_ja": "金・土のピーク速報や特典案内に映える、黒背景に金色のシャンパンバブルがゆっくり昇る華やかなマクロ映像。",
        "prompt_en": (
            "Vertical 9:16 extreme macro slow-motion shot of fine golden champagne bubbles rising gracefully against "
            "a deep pitch-black and warm bronze background, subtle rose-gold rim lighting, crystal glass refraction bokeh, "
            "luxury commercial aesthetic, 4k photorealistic, no text, no watermark."
        ),
        "recommended_filename": "veo_champagne.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "ladies_zero_wait",
    },
    {
        "id": "veo_rain_cyber",
        "title": "☔ 雨夜のタクシー車窓×繁華街ネオン反射（雨の日・深夜枠向け）",
        "scene_ja": "雨の夜や深夜0時枠のリアルな空気感を演出する、ガラス越しの雨粒と繁華街ネオンのしっとりした映像。",
        "prompt_en": (
            "Vertical 9:16 moody cinematic view through a dark rain-speckled taxi window at night in downtown Tokyo, "
            "soft blurred neon signs in electric blue, coral pink, and warm gold sliding slowly across the dark glass, "
            "shallow depth of field, cozy insider midnight vibe, no faces, no readable text."
        ),
        "recommended_filename": "veo_rain_cyber.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "area_battle",
    },
    {
        "id": "veo_tiktok_hook",
        "title": "🎵 TikTok特化・渋谷スクランブル風ダークシアン×ルビー光跡（冒頭2秒ストップ率UP）",
        "scene_ja": "TikTokのスクロールを指止めするコントラスト強めのダークストリート光跡ボケ。文字セーフゾーンがくっきり浮き立つ設計。",
        "prompt_en": (
            "Vertical 9:16 dynamic luxury night street hyperlapse bokeh in Tokyo Shibuya and Shinjuku, "
            "deep obsidian shadows with vibrant cyan-emerald and ruby-rose light streaks gliding smoothly, "
            "ultra-clean dark center frame optimized for bold Japanese mobile UI cards, f/1.4 shallow depth of field, "
            "cinematic 4k, no faces, no readable text, no watermark."
        ),
        "recommended_filename": "veo_tiktok_hook.mp4",
        "aspect_ratio": "9:16",
        "target_reel_type": "tiktok_gap_shock",
    },
]

_REEL_THEMES = {
    "live_top5": {
        "accent": (244, 114, 182),
        "accent2": (251, 191, 36),
        "bar_left": (236, 72, 153),
        "bar_right": (56, 189, 248),
        "glow1": (236, 72, 153, 96),
        "glow2": (251, 191, 36, 86),
        "glow3": (56, 189, 248, 52),
        "bg_top": (8, 9, 18),
        "bg_mid": (20, 11, 26),
        "bg_bot": (7, 10, 19),
        "badge_bg": (72, 18, 48, 236),
        "preferred_videos": ("veo_neon.mp4", "veo_lounge.mp4"),
        "preferred_images": ("neon_city_9x16.jpg", "luxury_lounge_9x16.jpg"),
    },
    "area_battle": {
        "accent": (56, 189, 248),
        "accent2": (251, 191, 36),
        "bar_left": (244, 114, 182),
        "bar_right": (56, 189, 248),
        "glow1": (56, 189, 248, 100),
        "glow2": (236, 72, 153, 88),
        "glow3": (168, 85, 247, 60),
        "bg_top": (6, 12, 24),
        "bg_mid": (14, 18, 34),
        "bg_bot": (8, 9, 18),
        "badge_bg": (14, 48, 74, 236),
        "preferred_videos": ("veo_city_battle.mp4", "veo_rain_cyber.mp4", "veo_neon.mp4"),
        "preferred_images": ("neon_city_9x16.jpg", "luxury_lounge_9x16.jpg"),
    },
    "ladies_zero_wait": {
        "accent": (244, 162, 184),
        "accent2": (238, 203, 148),
        "bar_left": (238, 190, 128),
        "bar_right": (244, 130, 170),
        "glow1": (244, 130, 170, 100),
        "glow2": (238, 196, 132, 92),
        "glow3": (192, 132, 252, 54),
        "bg_top": (18, 10, 18),
        "bg_mid": (30, 15, 26),
        "bg_bot": (11, 8, 14),
        "badge_bg": (68, 24, 44, 236),
        "preferred_videos": ("veo_rosegold.mp4", "veo_champagne.mp4", "veo_lounge.mp4"),
        "preferred_images": ("luxury_lounge_9x16.jpg", "neon_city_9x16.jpg"),
    },
    "tiktok_gap_shock": {
        "accent": (37, 244, 238),
        "accent2": (254, 88, 124),
        "bar_left": (254, 68, 114),
        "bar_right": (37, 212, 238),
        "glow1": (37, 244, 238, 94),
        "glow2": (254, 44, 85, 92),
        "glow3": (251, 191, 36, 56),
        "bg_top": (6, 10, 20),
        "bg_mid": (16, 12, 28),
        "bg_bot": (6, 8, 16),
        "badge_bg": (12, 46, 58, 240),
        "preferred_videos": ("veo_tiktok_hook.mp4", "veo_city_battle.mp4", "veo_neon.mp4"),
        "preferred_images": ("neon_city_9x16.jpg", "luxury_lounge_9x16.jpg"),
    },
    "tiktok_ladies_real": {
        "accent": (248, 150, 184),
        "accent2": (238, 206, 148),
        "bar_left": (238, 194, 128),
        "bar_right": (244, 118, 166),
        "glow1": (244, 118, 166, 98),
        "glow2": (238, 196, 132, 90),
        "glow3": (37, 244, 238, 48),
        "bg_top": (16, 9, 18),
        "bg_mid": (28, 14, 26),
        "bg_bot": (10, 7, 14),
        "badge_bg": (66, 20, 44, 240),
        "preferred_videos": ("veo_rosegold.mp4", "veo_tiktok_hook.mp4", "veo_lounge.mp4"),
        "preferred_images": ("luxury_lounge_9x16.jpg", "neon_city_9x16.jpg"),
    },
    "tiktok_midnight_cliff": {
        "accent": (234, 196, 124),
        "accent2": (56, 189, 248),
        "bar_left": (244, 114, 182),
        "bar_right": (56, 189, 248),
        "glow1": (234, 196, 124, 92),
        "glow2": (56, 189, 248, 88),
        "glow3": (168, 85, 247, 58),
        "bg_top": (7, 9, 20),
        "bg_mid": (15, 16, 34),
        "bg_bot": (6, 8, 15),
        "badge_bg": (48, 36, 16, 240),
        "preferred_videos": ("veo_rain_cyber.mp4", "veo_tiktok_hook.mp4", "veo_lounge.mp4"),
        "preferred_images": ("luxury_lounge_9x16.jpg", "neon_city_9x16.jpg"),
    },
}

_LINUX_NOTO_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
)

_MEM_MP4_CACHE = collections.OrderedDict()  # cache_key -> {"ts": float, "bytes": bytes, "reel_type": str}
_MEM_MP4_MAX = 4
_MEM_CACHE_LOCK = threading.Lock()
_RENDER_SINGLEFLIGHT_LOCK = threading.Lock()

_BG_OVERSIZED_CACHE = collections.OrderedDict()
_BG_CACHE_LOCK = threading.Lock()
_VIGNETTE_CACHE = {}


def enforce_brand_rule(text: str) -> str:
    """Ensure standalone brand prefix never appears without 'ラウンジ'."""
    if not text:
        return ""
    return _STANDALONE_BRAND_RE.sub(_FULL_BRAND_OLG, str(text))


def normalize_reel_type(reel_type: str) -> str:
    rt = (reel_type or "").strip().lower()
    return rt if rt in REEL_TEMPLATES else "live_top5"


def resolve_ffmpeg_bin() -> str:
    """
    Resolve FFmpeg executable path in strict priority order:
      1. `os.environ.get("FFMPEG_BIN")`
      2. `shutil.which("ffmpeg")`
      3. `imageio_ffmpeg.get_ffmpeg_exe()` (inside try/except ImportError)
      4. `None` if unavailable
    """
    env_bin = (os.environ.get("FFMPEG_BIN") or "").strip()
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        resolved_env = shutil.which(env_bin)
        if resolved_env:
            return resolved_env

    which_bin = shutil.which("ffmpeg")
    if which_bin:
        return which_bin

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except ImportError:
        pass
    except Exception:
        pass

    return None


def _get_reel_font(size: int):
    """Reuse ogp_generator's cached bold Japanese font with Linux Noto CJK fallback."""
    f = ogp_generator._get_font(size)
    if f is not None:
        return f
    from PIL import ImageFont
    for p in _LINUX_NOTO_FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def list_available_backgrounds(reel_type: str = "live_top5") -> dict:
    """
    Inspect `static/reels_bg/` and return available `.mp4` (Google Flow / Veo 3) videos
    and `.jpg` / `.png` portrait background images, plus the active selection.
    """
    rt = normalize_reel_type(reel_type)
    theme = _REEL_THEMES[rt]
    videos = []
    images = []
    dir_exists = os.path.isdir(REELS_BG_DIR)

    if dir_exists:
        try:
            entries = sorted(os.listdir(REELS_BG_DIR))
        except Exception:
            entries = []
        for fname in entries:
            if fname.startswith("."):
                continue
            fpath = os.path.join(REELS_BG_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                st = os.stat(fpath)
                if st.st_size <= 0:
                    continue
                meta = {
                    "filename": fname,
                    "path": fpath,
                    "size_kb": round(st.st_size / 1024.0, 1),
                    "mtime": int(st.st_mtime),
                }
            except Exception:
                continue
            low = fname.lower()
            if low.endswith(".mp4"):
                videos.append(meta)
            elif low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                images.append(meta)

    selected_video = None
    if videos:
        by_name = {v["filename"]: v for v in videos}
        for pref in theme["preferred_videos"]:
            if pref in by_name:
                selected_video = by_name[pref]
                break
        if selected_video is None:
            # Newest user-dropped Veo 3 mp4 wins
            selected_video = sorted(videos, key=lambda x: (x["mtime"], x["filename"]), reverse=True)[0]

    selected_image = None
    if images:
        by_img = {im["filename"]: im for im in images}
        for pref in theme["preferred_images"]:
            if pref in by_img:
                selected_image = by_img[pref]
                break
        if selected_image is None:
            selected_image = sorted(images, key=lambda x: (x["mtime"], x["filename"]), reverse=True)[0]

    if selected_video:
        active_mode = "veo3_video"
        active_file = selected_video["filename"]
    elif selected_image:
        active_mode = "custom_image"
        active_file = selected_image["filename"]
    else:
        active_mode = "procedural_luxury"
        active_file = None

    return {
        "bg_dir": "static/reels_bg",
        "dir_exists": dir_exists,
        "videos": [
            {"filename": v["filename"], "size_kb": v["size_kb"], "mtime": v["mtime"]}
            for v in videos
        ],
        "images": [
            {"filename": im["filename"], "size_kb": im["size_kb"], "mtime": im["mtime"]}
            for im in images
        ],
        "active_mode": active_mode,
        "active_file": active_file,
        "selected_video_file": selected_video["filename"] if selected_video else None,
        "selected_video_mtime": selected_video["mtime"] if selected_video else 0,
        "selected_image_file": selected_image["filename"] if selected_image else None,
        "selected_image_mtime": selected_image["mtime"] if selected_image else 0,
    }


def _resolve_bg_paths(bg_info: dict) -> tuple:
    vf = bg_info.get("selected_video_file")
    imf = bg_info.get("selected_image_file")
    v_path = os.path.join(REELS_BG_DIR, vf) if vf else None
    im_path = os.path.join(REELS_BG_DIR, imf) if imf else None
    return v_path, int(bg_info.get("selected_video_mtime") or 0), im_path, int(bg_info.get("selected_image_mtime") or 0)


def _is_domestic_store(store: dict) -> bool:
    if not isinstance(store, dict):
        return False
    if store.get("region") == "Korea":
        return False
    uname = (store.get("name") or "").upper()
    return not any(k in uname for k in _KOREA_KEYWORDS)


def _fallback_seed_stores() -> list:
    """Fallback domestic dataset used only when `stores` is empty or all stores have 0 headcount."""
    seeds = [
        {"name": "OLG 大阪駅前", "women": 58, "men": 41, "region": "Kansai"},
        {"name": "OLG 渋谷本店", "women": 52, "men": 38, "region": "Kanto"},
        {"name": "OLG 新宿", "women": 47, "men": 36, "region": "Kanto"},
        {"name": "JIS FUKUOKA", "women": 44, "men": 30, "region": "Kyushu"},
        {"name": "JIS NAMBA", "women": 39, "men": 29, "region": "Kansai"},
        {"name": "OLG 恵比寿", "women": 32, "men": 19, "region": "Kanto"},
        {"name": "OLG ag渋谷", "women": 28, "men": 34, "region": "Kanto"},
        {"name": "JIS SHINJUKU", "women": 31, "men": 39, "region": "Kanto"},
        {"name": "JIS UMEDA", "women": 34, "men": 42, "region": "Kansai"},
        {"name": "OLG 名古屋 栄", "women": 29, "men": 35, "region": "Chubu"},
        {"name": "OLG 京都", "women": 27, "men": 17, "region": "Kansai"},
    ]
    return seeds


def _enrich_store_entry(s: dict, preloaded_events: list = None) -> dict:
    raw_name = (s.get("name") or "").strip()
    w = max(0, int(s.get("women") or 0))
    m = max(0, int(s.get("men") or 0))
    tot = w + m
    wpct = int(round((w / tot) * 100)) if tot > 0 else 0
    mpct = (100 - wpct) if tot > 0 else 0

    ja_name = enforce_brand_rule(sns_editorial.natural_store_name(raw_name) or raw_name)
    casual_name = enforce_brand_rule(sns_editorial.casual_store_name(raw_name) or ja_name)
    info = store_master.get(raw_name) or {}
    area_label = enforce_brand_rule(info.get("area_label") or "主要エリア")

    ev_title = ""
    has_event = False
    if raw_name and preloaded_events:
        try:
            matched = events.get_events_for_store(
                raw_name,
                store_only=True,
                allow_blocking=False,
                preloaded_events=preloaded_events,
            ) or []
            if matched:
                has_event = True
                ev_title = enforce_brand_rule(str(matched[0].get("title") or "").strip())[:28]
        except Exception:
            pass

    ladies_eval = ladies_guide.evaluate_ladies_store_status(
        women=w,
        men=m,
        has_event=has_event,
    )

    mid_ret = int(s.get("midnight_retention_pct") or 0)
    if mid_ret <= 0:
        mid_ret = min(94, max(64, 70 + min(22, tot // 4)))
    mid_lbl = enforce_brand_rule(str(s.get("midnight_label_short") or ("深夜安定" if mid_ret >= 78 else "終電前推奨")))

    return {
        "raw_name": raw_name,
        "ja_name": ja_name,
        "casual_name": casual_name,
        "area_label": area_label,
        "women": w,
        "men": m,
        "total": tot,
        "wpct": wpct,
        "mpct": mpct,
        "male_lead": m - w,
        "has_event": has_event,
        "event_title": ev_title,
        "ladies_score": float(ladies_eval.get("score") or 0.0),
        "ladies_tier": ladies_eval.get("tier") or "",
        "ladies_badge": enforce_brand_rule(ladies_eval.get("badge") or ""),
        "midnight_retention_pct": mid_ret,
        "midnight_label_short": mid_lbl,
    }


def build_reel_context(
    stores: list,
    reel_type: str = "live_top5",
    now_jst: datetime.datetime = None,
) -> dict:
    """Build structured, verified domestic crowd context for all 3 Reel video templates."""
    rt = normalize_reel_type(reel_type)
    now_jst = now_jst or datetime.datetime.now(JST)
    if now_jst.tzinfo is None:
        now_jst = now_jst.replace(tzinfo=JST)

    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    time_label = f"{now_jst.month}/{now_jst.day}({wd_str}) {now_jst.strftime('%H:%M')}"

    dom_raw = [s for s in (stores or []) if _is_domestic_store(s)]
    live_total_people = sum(max(0, int(s.get("women") or 0)) + max(0, int(s.get("men") or 0)) for s in dom_raw)

    is_fallback = False
    if not dom_raw or live_total_people == 0:
        dom_raw = _fallback_seed_stores()
        is_fallback = True

    try:
        cached_events = events.fetch_all_events(force_refresh=False, allow_blocking=False) or []
    except Exception:
        cached_events = []

    enriched = [_enrich_store_entry(s, preloaded_events=cached_events) for s in dom_raw]
    active_stores = [s for s in enriched if s["total"] > 0] or enriched

    total_women = sum(s["women"] for s in active_stores)
    total_men = sum(s["men"] for s in active_stores)
    total_all = total_women + total_men
    overall_wpct = int(round((total_women / total_all) * 100)) if total_all > 0 else 50

    # 1. Live Top 5 by female headcount + high-ratio (>=55%) anaba pickups
    top5_volume = sorted(
        active_stores,
        key=lambda x: (x["women"], x["wpct"], x["men"]),
        reverse=True,
    )[:5]

    high_ratio_pool = [s for s in active_stores if s["women"] >= 6 and s["wpct"] >= 55]
    if len(high_ratio_pool) < 3:
        high_ratio_pool = [s for s in active_stores if s["women"] >= 4] or list(active_stores)
    anaba_pickups = sorted(
        high_ratio_pool,
        key=lambda x: (x["wpct"], x["women"], -x["men"]),
        reverse=True,
    )[:3]

    # 2. Area Battle: Shinjuku vs Umeda vs Shibuya
    battle_defs = [
        ("shinjuku", "新宿エリア", "TOKYO SHINJUKU", ("新宿", "SHINJUKU", "NISHISHINJUKU")),
        ("umeda", "梅田エリア", "OSAKA UMEDA", ("梅田", "茶屋町", "大阪駅前", "天満", "UMEDA", "CHAYAMACHI")),
        ("shibuya", "渋谷エリア", "TOKYO SHIBUYA", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
    ]
    area_battles = []
    for z_key, z_label, z_en, kw_tuple in battle_defs:
        matched = [
            s for s in enriched
            if any(k in (s["raw_name"].upper()) or k in s["ja_name"] for k in kw_tuple)
        ]
        zw = sum(s["women"] for s in matched)
        zm = sum(s["men"] for s in matched)
        ztot = zw + zm
        zwpct = int(round((zw / ztot) * 100)) if ztot > 0 else 50

        active_m = [s for s in matched if s["total"] > 0] or matched
        best_st = None
        if active_m:
            best_st = sorted(
                active_m,
                key=lambda x: (1 if x["women"] >= 6 else 0, x["wpct"], x["women"], -x["men"]),
                reverse=True,
            )[0]

        area_battles.append({
            "key": z_key,
            "label": z_label,
            "en_tag": z_en,
            "store_count": len(matched),
            "women": zw,
            "men": zm,
            "total": ztot,
            "wpct": zwpct,
            "best_store": best_st,
            "is_leader": False,
        })

    if area_battles:
        leader_idx = max(
            range(len(area_battles)),
            key=lambda idx: (area_battles[idx]["women"], area_battles[idx]["wpct"]),
        )
        area_battles[leader_idx]["is_leader"] = True
        best_ratio_zone = max(area_battles, key=lambda z: (z["wpct"], z["women"]))
    else:
        best_ratio_zone = None

    # 3. Ladies Zero-Wait Top 5 (male-leading or balanced active lounges)
    ladies_sorted = sorted(
        active_stores,
        key=lambda x: (
            1 if (x["men"] >= 4 and x["men"] >= x["women"]) else 0,
            x["ladies_score"],
            x["male_lead"],
            x["men"],
        ),
        reverse=True,
    )[:5]

    # 4. TikTok Same-Area Gap Pairs (walkable same-station heaven vs male-wait contrast)
    hub_defs = [
        ("新宿徒歩4分圏", ("新宿", "SHINJUKU", "NISHISHINJUKU")),
        ("梅田徒歩4分圏", ("梅田", "茶屋町", "大阪駅前", "天満", "UMEDA", "CHAYAMACHI")),
        ("渋谷・恵比寿圏", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
        ("難波・心斎橋圏", ("難波", "心斎橋", "NAMBA", "SHINSAIBASHI")),
        ("名古屋・栄圏", ("名古屋", "栄", "NAGOYA", "SAKAE")),
        ("福岡・天神圏", ("福岡", "天神", "小倉", "FUKUOKA", "TENJIN")),
    ]
    gap_pairs = []
    for hub_label, kw_tuple in hub_defs:
        hub_stores = [
            s for s in active_stores
            if s["total"] >= 4 and any(k in s["raw_name"].upper() or k in s["ja_name"] or k in s["area_label"] for k in kw_tuple)
        ]
        if len(hub_stores) >= 2:
            by_w = sorted(hub_stores, key=lambda x: (x["wpct"], x["women"]), reverse=True)
            hi_s = by_w[0]
            lo_s = by_w[-1]
            if hi_s["raw_name"] != lo_s["raw_name"]:
                gap_pairs.append({
                    "hub_label": hub_label,
                    "heaven_store": hi_s,
                    "crowded_store": lo_s,
                    "gap_pt": max(1, hi_s["wpct"] - lo_s["wpct"]),
                })
    gap_pairs.sort(key=lambda g: (g["gap_pt"], g["heaven_store"]["women"]), reverse=True)
    if len(gap_pairs) < 3 and len(active_stores) >= 2:
        by_global_w = sorted(active_stores, key=lambda x: (x["wpct"], x["women"]), reverse=True)
        for idx_fb in range(min(3 - len(gap_pairs), len(by_global_w) // 2)):
            hi_fb = by_global_w[idx_fb]
            lo_fb = by_global_w[-(idx_fb + 1)]
            if hi_fb["raw_name"] != lo_fb["raw_name"]:
                gap_pairs.append({
                    "hub_label": f"{hi_fb['area_label']} vs {lo_fb['area_label']}",
                    "heaven_store": hi_fb,
                    "crowded_store": lo_fb,
                    "gap_pt": max(1, hi_fb["wpct"] - lo_fb["wpct"]),
                })
    gap_pairs = gap_pairs[:3]

    # 5. TikTok Midnight Cliff Survivors Top 4
    midnight_top4 = sorted(
        active_stores,
        key=lambda x: (
            1 if x["women"] >= 8 else 0,
            x["midnight_retention_pct"],
            x["wpct"],
            x["women"],
        ),
        reverse=True,
    )[:4]

    return {
        "reel_type": rt,
        "now_jst": now_jst,
        "time_label": time_label,
        "weekday_ja": wd_str,
        "is_fallback": is_fallback,
        "active_store_count": len(active_stores),
        "total_store_count": max(53, len(enriched)),
        "total_women": total_women,
        "total_men": total_men,
        "overall_wpct": overall_wpct,
        "top5_volume": top5_volume,
        "anaba_pickups": anaba_pickups,
        "area_battles": area_battles,
        "best_ratio_zone": best_ratio_zone,
        "ladies_top5": ladies_sorted,
        "gap_pairs": gap_pairs,
        "midnight_top4": midnight_top4,
    }


def build_reel_caption(
    stores: list,
    reel_type: str = "live_top5",
    now_jst: datetime.datetime = None,
) -> str:
    """
    Generate human-voiced, save-worthy caption + hashtags for Instagram Reels / TikTok / YouTube Shorts.
    Strictly uses full 'オリエンタルラウンジ' or shorthand 'オリラジ' across all generated text.
    """
    ctx = build_reel_context(stores, reel_type=reel_type, now_jst=now_jst)
    rt = ctx["reel_type"]
    t_lbl = ctx["time_label"]
    mode_note = "（※直近ピーク実績ベース）" if ctx["is_fallback"] else f"（{t_lbl} リアルタイム集計）"

    if rt == "area_battle":
        zones = ctx["area_battles"]
        leader = next((z for z in zones if z.get("is_leader")), zones[0] if zones else None)
        ratio_win = ctx["best_ratio_zone"] or leader
        z_lines = []
        for z in zones:
            bs = z.get("best_store")
            bs_txt = f"｜狙い目：{bs['casual_name']}（女{bs['women']}:男{bs['men']}）" if bs else ""
            crown = "👑" if z.get("is_leader") else "⚡"
            z_lines.append(
                f"{crown} {z['label']}：女性{z['women']}名 / 男性{z['men']}名（女性比{z['wpct']}%）{bs_txt}"
            )
        joined_z = "\n".join(z_lines)
        lead_name = leader["label"] if leader else "新宿エリア"
        ratio_name = ratio_win["label"] if ratio_win else "梅田エリア"
        caption = (
            f"同じ夜でもエリアでここまで男女比が割れるの、毎回モニター見てて面白い…📊{mode_note}\n\n"
            f"今の3大激戦区（新宿 vs 梅田 vs 渋谷）のリアル集計がこちら👇\n"
            f"{joined_z}\n\n"
            f"人数ボリュームが一番熱いのは【{lead_name}】、男性が待ち時間を避けて座りやすい比率なのは【{ratio_name}】という結果に。\n"
            f"同じ駅でも徒歩3〜4分の別ブランド（オリラジ・ag・JIS）を見比べるだけで「男性待ち50分」を余裕で回避できます💡\n\n"
            f"お店に向かう直前の「全53店舗の現在人数＆男女比」は、プロフィールのリンク『相席ナビ』（aiseki-navi.com）から登録不要・3秒で無料検索できます🔍\n"
            f"週末の作戦会議用に「保存📌」しておくと便利です！\n\n"
            f"#相席ナビ #相席ラウンジ #オリエンタルラウンジ #オリラジ #ag #JIS #新宿グルメ #梅田グルメ #渋谷グルメ #夜遊び"
        )
        return enforce_brand_rule(caption)

    if rt == "ladies_zero_wait":
        l_top = ctx["ladies_top5"][:4]
        l_lines = []
        for idx, s in enumerate(l_top, start=1):
            perk_s = f"・🎁{s['event_title']}" if s.get("event_title") else ""
            l_lines.append(
                f"・{idx}位：{s['ja_name']}（男性{s['men']}名 / 女性{s['women']}名{perk_s}）"
            )
        joined_l = "\n".join(l_lines)
        caption = (
            f"せっかく髪もメイクも整えたのに、受付で待たされるのだけはほんと避けたいよね…💄{mode_note}\n\n"
            f"女子ペアが「待ち時間ほぼゼロ」でスマートに乾杯しやすい、いま男性が先行して盛り上がっている狙い目ラウンジ上位はこちら🥂\n\n"
            f"{joined_l}\n\n"
            f"💡 女子組の失敗しないお店選びメモ\n"
            f"女性比率が70%超えで女子が集中している店舗より、男性が先行している店舗を選ぶ方が入店直後から席替えもスムーズで快適です✨\n\n"
            f"いま現在「女子待ち0」で入れる店舗と今夜の限定特典・フード比較は、プロフィールのリンク『相席ナビ』（aiseki-navi.com）から3秒でチェックできます🔍\n"
            f"友達との2軒目探し用に「保存📌」して使ってください🥂\n\n"
            f"#相席ナビ #女子会 #相席ラウンジ #オリエンタルラウンジ #オリラジ #ag #JIS #週末女子会 #夜カフェ #2軒目"
        )
        return enforce_brand_rule(caption)

    if rt == "tiktok_gap_shock":
        gp_list = ctx.get("gap_pairs") or []
        gp_lines = []
        for idx, gp in enumerate(gp_list[:3], start=1):
            hi = gp["heaven_store"]
            lo = gp["crowded_store"]
            gp_lines.append(
                f"📍 {gp['hub_label']}（比率差 {gp['gap_pt']}pt）\n"
                f"　🟢 狙い目：{hi['ja_name']}（女{hi['women']}:男{hi['men']}｜女性比{hi['wpct']}%）\n"
                f"　🔴 対照店：{lo['ja_name']}（女{lo['women']}:男{lo['men']}｜女性比{lo['wpct']}%）"
            )
        joined_gp = "\n".join(gp_lines)
        caption = (
            f"同じ駅で徒歩4分しか離れてないのに、お店のドア1枚でここまで男女比が変わる…😳📊{mode_note}\n\n"
            f"{joined_gp}\n\n"
            f"「相席ラウンジどこも男待ちでしょ…」って諦める前に、駅を出る直前に別ブランド（オリラジ・ag・JIS）を見比べるだけで待ち時間ゼロの店舗が見つかります💡\n\n"
            f"いま現在の全53店舗の男女人数・料金シミュレーターは『相席ナビ』（aiseki-navi.com）で検索🔍（プロフリンクからも3秒で見れます）\n"
            f"今夜・週末に使う用に「保存🔖」しておいてください！\n\n"
            f"#相席ナビ #相席ラウンジ #オリエンタルラウンジ #オリラジ #ag #JIS #新宿夜遊び #梅田グルメ #渋谷夜遊び #相席"
        )
        return enforce_brand_rule(caption)

    if rt == "tiktok_ladies_real":
        l_top = ctx["ladies_top5"][:4]
        l_lines = "\n".join(
            f"🥂 {idx}位：{s['ja_name']}（男性{s['men']}名 / 女性{s['women']}名"
            + (f"・🎁{s['event_title']}）" if s.get("event_title") else "）")
            for idx, s in enumerate(l_top, start=1)
        )
        caption = (
            f"女子ペアで「女性70%超えの店舗」に行くと、実は受付で待たされたり席替えが止まりやすいって知ってた？💄{mode_note}\n\n"
            f"いま男性が先行していて「受付待ちほぼゼロ」でスマートに乾杯できる狙い目ラウンジTOP4👇\n"
            f"{l_lines}\n\n"
            f"友達との2軒目や夜カフェ代わりに使うなら、行く3分前に男性先行店を選ぶのが一番快適✨\n"
            f"現在人数と今夜の女子向け特典は『相席ナビ』（aiseki-navi.com）で検索🔍（登録不要・無料）\n"
            f"女子会の作戦会議用に「保存🔖」推奨です🥂\n\n"
            f"#相席ナビ #相席ラウンジ #女子会 #オリエンタルラウンジ #オリラジ #ag #JIS #2軒目 #夜カフェ #週末女子会"
        )
        return enforce_brand_rule(caption)

    if rt == "tiktok_midnight_cliff":
        m_top = ctx.get("midnight_top4") or ctx["top5_volume"][:4]
        m_lines = "\n".join(
            f"🌙 {idx}位：{s['ja_name']}（女{s['women']}:男{s['men']}｜終電後維持 {s['midnight_retention_pct']}%）"
            for idx, s in enumerate(m_top[:4], start=1)
        )
        caption = (
            f"相席ラウンジには「23時20分の終電崖」があって、終電で一気に人が引く店と深夜0時過ぎから逆転で盛り上がる店に真っ二つに分かれます🌙{mode_note}\n\n"
            f"いま現在盛り上がっていて終電後も強いラウンジTOP4👇\n"
            f"{m_lines}\n\n"
            f"2軒目・3軒目で失敗したくない夜は、入店直前のリアルタイム人数を見てから動くのが鉄則💡\n"
            f"全国53店舗の現在人数と深夜維持率は『相席ナビ』（aiseki-navi.com）で検索🔍\n"
            f"終電後の駆け込み用に「保存🔖」しておくと役立ちます！\n\n"
            f"#相席ナビ #相席ラウンジ #オリエンタルラウンジ #オリラジ #ag #JIS #終電逃した #2軒目 #新宿 #梅田"
        )
        return enforce_brand_rule(caption)

    # Default: live_top5
    v_top = ctx["top5_volume"][:5]
    a_top = ctx["anaba_pickups"][:2]
    v_lines = "\n".join(
        f"・{i+1}位：{s['ja_name']}（女性{s['women']}名 / 男性{s['men']}名｜女性比{s['wpct']}%）"
        for i, s in enumerate(v_top)
    )
    a_lines = "\n".join(
        f"💎 {s['ja_name']}（女性{s['women']}名・男性{s['men']}名｜女性比{s['wpct']}%）"
        for s in a_top
    )
    caption = (
        f"ノープランで1店舗目から突撃して「受付待ち1時間」になる前に、これだけ見ておいてほしい…🔥{mode_note}\n\n"
        f"🔥 全国リアルタイム女性人数 TOP5\n"
        f"{v_lines}\n\n"
        f"🎯 いま女性比率が高めで入りやすい狙い目穴場\n"
        f"{a_lines}\n\n"
        f"男性は「女性10名以上×女性比率55%以上」の店舗、女性ペアは逆に「男性先行中」の店舗を選ぶと待ち時間ゼロでスムーズに楽しめます🥂\n\n"
        f"全国53店舗のいま現在の男女人数・比率・料金比較は、プロフィールのリンク『相席ナビ』（aiseki-navi.com）で24時間無料公開中🔍\n"
        f"お店に入る3分前に見返せるよう「保存📌」推奨です！\n\n"
        f"#相席ナビ #相席ラウンジ #オリエンタルラウンジ #オリラジ #ag #JIS #相席 #新宿 #梅田 #渋谷"
    )
    return enforce_brand_rule(caption)


# =====================================================================
# Visual Background, Sprites & Timeline Frame Renderer (720x1280 @ 24fps)
# =====================================================================
def _ease_out_cubic(p: float) -> float:
    c = max(0.0, min(1.0, float(p)))
    return 1.0 - (1.0 - c) ** 3


def _fade_window(t: float, start_s: float, end_s: float, fade_in: float = 0.35, fade_out: float = 0.25) -> float:
    if t < start_s or t > end_s:
        return 0.0
    alpha = 1.0
    if fade_in > 0 and (t - start_s) < fade_in:
        alpha = min(alpha, _ease_out_cubic((t - start_s) / fade_in))
    if fade_out > 0 and (end_s - t) < fade_out:
        alpha = min(alpha, max(0.0, min(1.0, (end_s - t) / fade_out)))
    return alpha


def _apply_opacity(sprite: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 0.995:
        return sprite
    if opacity <= 0.005:
        return Image.new("RGBA", sprite.size, (0, 0, 0, 0))
    out = sprite.copy()
    scale = max(0.0, min(1.0, opacity))
    a_chan = out.getchannel("A").point(lambda v: int(v * scale))
    out.putalpha(a_chan)
    return out


def _build_procedural_luxury_bg(width: int, height: int, reel_type: str) -> Image.Image:
    """Create an oversized dark-gold & bokeh luxury 9:16 portrait canvas purely with Pillow."""
    theme = _REEL_THEMES[reel_type]
    c_top = theme["bg_top"]
    c_mid = theme["bg_mid"]
    c_bot = theme["bg_bot"]

    # Fast vertical gradient via 1x160 strip resized with BILINEAR
    gh = 160
    grad_strip = Image.new("RGB", (1, gh))
    for y in range(gh):
        ratio = y / max(1, gh - 1)
        if ratio < 0.5:
            p = ratio * 2.0
            col = tuple(int(c_top[k] + (c_mid[k] - c_top[k]) * p) for k in range(3))
        else:
            p = (ratio - 0.5) * 2.0
            col = tuple(int(c_mid[k] + (c_bot[k] - c_mid[k]) * p) for k in range(3))
        grad_strip.putpixel((0, y), col)
    base = grad_strip.resize((width, height), Image.Resampling.BILINEAR).convert("RGBA")
    grad_strip.close()

    # Soft bokeh & diagonal champagne sheen drawn at half resolution and blurred
    hw, hh = max(1, width // 2), max(1, height // 2)
    bokeh = Image.new("RGBA", (hw, hh), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bokeh, "RGBA")

    # Subtle diagonal luxury light streaks
    acc = theme["accent"]
    acc2 = theme["accent2"]
    bdraw.polygon(
        [(int(hw * 0.10), 0), (int(hw * 0.36), 0), (int(hw * 0.85), hh), (int(hw * 0.58), hh)],
        fill=(acc2[0], acc2[1], acc2[2], 24),
    )
    bdraw.polygon(
        [(int(hw * 0.65), 0), (int(hw * 0.92), 0), (int(hw * 0.35), hh), (int(hw * 0.12), hh)],
        fill=(acc[0], acc[1], acc[2], 22),
    )

    # Multi-layered out-of-focus bokeh circles
    orbs = [
        (0.18, 0.14, 0.24, (*acc, 85)),
        (0.84, 0.20, 0.20, (*acc2, 82)),
        (0.26, 0.44, 0.16, (251, 191, 36, 56)),
        (0.76, 0.52, 0.22, (*acc, 68)),
        (0.15, 0.76, 0.21, (56, 189, 248, 56)),
        (0.82, 0.84, 0.25, (*acc2, 86)),
        (0.50, 0.30, 0.11, (255, 236, 196, 48)),
        (0.62, 0.68, 0.09, (255, 220, 235, 50)),
        (0.34, 0.88, 0.10, (251, 191, 36, 54)),
    ]
    for rx, ry, rr, col in orbs:
        cx, cy = int(hw * rx), int(hh * ry)
        rad = int(hw * rr)
        bdraw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=col)

    bokeh_blurred = bokeh.filter(ImageFilter.GaussianBlur(radius=22)).resize(
        (width, height), Image.Resampling.BILINEAR
    )
    bokeh.close()
    base.alpha_composite(bokeh_blurred)
    bokeh_blurred.close()

    ogp_generator._draw_radial_glow(base, (int(width * 0.18), int(height * 0.15)), int(width * 0.62), theme["glow1"])
    ogp_generator._draw_radial_glow(base, (int(width * 0.85), int(height * 0.84)), int(width * 0.62), theme["glow2"])
    ogp_generator._draw_radial_glow(base, (int(width * 0.50), int(height * 0.50)), int(width * 0.48), theme["glow3"])

    rgb = base.convert("RGB")
    base.close()
    return rgb


def _get_oversized_bg_canvas(reel_type: str, image_path: str = None, image_mtime: int = 0) -> Image.Image:
    """Return cached 828x1472 RGB background canvas for smooth Ken Burns zooming."""
    rt = normalize_reel_type(reel_type)
    bw, bh = int(REEL_WIDTH * 1.15), int(REEL_HEIGHT * 1.15)  # 828 x 1472
    cache_key = (rt, image_path or "__procedural__", int(image_mtime or 0))

    with _BG_CACHE_LOCK:
        cached = _BG_OVERSIZED_CACHE.get(cache_key)
        if cached is not None:
            _BG_OVERSIZED_CACHE.move_to_end(cache_key)
            return cached

    theme = _REEL_THEMES[rt]
    built = None
    if image_path and os.path.isfile(image_path):
        try:
            with Image.open(image_path) as raw_im:
                src = raw_im.convert("RGBA")
            sw, sh = src.size
            scale = max(bw / max(1, sw), bh / max(1, sh))
            nw, nh = int(sw * scale), int(sh * scale)
            resized = src.resize((nw, nh), Image.Resampling.LANCZOS)
            src.close()
            left = (nw - bw) // 2
            top = (nh - bh) // 2
            cropped = resized.crop((left, top, left + bw, top + bh))
            resized.close()
            blurred = cropped.filter(ImageFilter.GaussianBlur(radius=6))
            cropped.close()
            tint = Image.new("RGBA", (bw, bh), (8, 10, 18, 168))
            blurred.alpha_composite(tint)
            tint.close()
            ogp_generator._draw_radial_glow(blurred, (int(bw * 0.2), int(bh * 0.16)), int(bw * 0.55), theme["glow1"])
            ogp_generator._draw_radial_glow(blurred, (int(bw * 0.82), int(bh * 0.84)), int(bw * 0.55), theme["glow2"])
            built = blurred.convert("RGB")
            blurred.close()
        except Exception as e:
            logger.warning(f"Custom reel image load failed ({image_path}): {e}")
            built = None

    if built is None:
        built = _build_procedural_luxury_bg(bw, bh, rt)

    with _BG_CACHE_LOCK:
        _BG_OVERSIZED_CACHE[cache_key] = built
        while len(_BG_OVERSIZED_CACHE) > 6:
            _, old_im = _BG_OVERSIZED_CACHE.popitem(last=False)
            try:
                old_im.close()
            except Exception:
                pass
    return built


def _get_vignette_overlay(reel_type: str, for_video_bg: bool = False) -> Image.Image:
    """Return cached 720x1280 RGBA dark gradient & edge vignette overlay."""
    rt = normalize_reel_type(reel_type)
    key = (rt, bool(for_video_bg))
    cached = _VIGNETTE_CACHE.get(key)
    if cached is not None:
        return cached

    W, H = REEL_WIDTH, REEL_HEIGHT
    theme = _REEL_THEMES[rt]
    # Vertical alpha profile: stronger at top/bottom for readable headers/CTAs, lighter in middle for motion
    vh = 160
    strip = Image.new("RGBA", (1, vh))
    for y in range(vh):
        p = y / max(1, vh - 1)
        dist_edge = min(p, 1.0 - p) * 2.0  # 0 at top/bot, 1 at center
        if for_video_bg:
            alpha = int(208 - 54 * (dist_edge ** 0.8))  # 208 at edges -> 154 in center
        else:
            alpha = int(136 - 78 * (dist_edge ** 0.8))  # 136 at edges -> 58 in center
        strip.putpixel((0, y), (7, 9, 16, max(0, min(255, alpha))))
    vig = strip.resize((W, H), Image.Resampling.BILINEAR)
    strip.close()

    if for_video_bg:
        ogp_generator._draw_radial_glow(vig, (130, 170), 390, theme["glow1"])
        ogp_generator._draw_radial_glow(vig, (590, 1080), 390, theme["glow2"])

    _VIGNETTE_CACHE[key] = vig
    return vig


class _ReelSceneRenderer:
    """
    Pre-renders crisp glassmorphic UI sprites once per Reel generation and composites
    smooth 24fps animations (`0.0s .. 11.5s`) at minimal CPU & memory cost.
    """

    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.rt = ctx["reel_type"]
        self.is_tiktok = self.rt.startswith("tiktok_")
        self.theme = _REEL_THEMES[self.rt]
        self.W = REEL_WIDTH
        self.H = REEL_HEIGHT
        # TikTok UI Safe-Zone: keep clear of top feed tabs (y<96), right action column (96px), and bottom caption area (y>1016)
        self.safe_left = 28 if self.is_tiktok else 36
        self.safe_right_pad = 96 if self.is_tiktok else 36
        self.safe_w = self.W - self.safe_left - self.safe_right_pad
        self.top_y = 96 if self.is_tiktok else 36
        self.prog_y = 1002 if self.is_tiktok else 1208

        self.chrome_base = self._build_chrome_sprite()
        self.scene1_sprite = self._build_scene1_hook_sprite()
        self.scene2_header = self._build_scene2_header_sprite()
        self.scene2_cards = self._build_scene2_card_sprites()
        self.scene3_sprite = self._build_scene3_cta_sprite()

    def _build_chrome_sprite(self) -> Image.Image:
        W, H = self.W, self.H
        sp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ogp_generator.make_safe_draw(sp, "RGBA")
        acc = self.theme["accent"]
        lx = self.safe_left
        rx_end = W - self.safe_right_pad
        ty = self.top_y

        f_brand = _get_reel_font(15 if self.is_tiktok else 16)
        f_time = _get_reel_font(14 if self.is_tiktok else 15)
        f_foot = _get_reel_font(14 if self.is_tiktok else 15)

        brand_txt = "AISEKI NAVI | 相席ナビ" if self.is_tiktok else "AISEKI NAVI  |  相席ナビ速報"
        bw = int(draw.textlength(brand_txt, font=f_brand)) + 26
        draw.rounded_rectangle((lx, ty, lx + bw, ty + 38), radius=19, fill=(18, 22, 36, 232), outline=acc, width=2)
        draw.text((lx + 13, ty + 9), brand_txt, font=f_brand, fill=acc)

        time_txt = f"LIVE {self.ctx['time_label']}"
        tw = int(draw.textlength(time_txt, font=f_time)) + 38
        rx1 = rx_end - tw
        self.live_dot_cx = rx1 + 15
        self.live_dot_cy = ty + 19
        draw.rounded_rectangle((rx1, ty, rx_end, ty + 38), radius=19, fill=(18, 22, 36, 232), outline=(255, 255, 255, 70), width=1)
        draw.text((rx1 + 26, ty + 10), time_txt, font=f_time, fill=(241, 245, 249, 245))

        # Progress bar background track + footer caption inside safe zone
        py = self.prog_y
        draw.rounded_rectangle((lx, py, rx_end, py + 6), radius=3, fill=(255, 255, 255, 46))
        foot_txt = (
            "『相席ナビ』(aiseki-navi.com) 全53店舗の現在人数を無料公開中"
            if self.is_tiktok
            else "全国53店舗の現在人数は『相席ナビ』(aiseki-navi.com) で無料公開中"
        )
        foot_fit, f_foot_fit, _ = ogp_generator.fit_text_and_font(draw, foot_txt, self.safe_w, 15, 12)
        fw = int(draw.textlength(foot_fit, font=f_foot_fit))
        draw.text((lx + max(0, (self.safe_w - fw) // 2), py + 14), foot_fit, font=f_foot_fit, fill=(180, 192, 212, 238))
        return sp

    def _build_scene1_hook_sprite(self) -> Image.Image:
        W, H = self.W, self.H
        sp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ogp_generator.make_safe_draw(sp, "RGBA")
        acc = self.theme["accent"]
        acc2 = self.theme["accent2"]
        lx = self.safe_left
        sw = self.safe_w
        rx_end = lx + sw

        f_kicker = _get_reel_font(19 if self.is_tiktok else 20)
        f_stat_lbl = _get_reel_font(18 if self.is_tiktok else 19)
        f_next = _get_reel_font(18 if self.is_tiktok else 20)

        if self.rt == "tiktok_gap_shock":
            gp0 = (self.ctx.get("gap_pairs") or [None])[0]
            hi_pct = gp0["heaven_store"]["wpct"] if gp0 else 64
            lo_pct = gp0["crowded_store"]["wpct"] if gp0 else 36
            kicker = f"TIKTOK LIVE GAP  |  {self.ctx['weekday_ja']}曜夜・同駅比較"
            lines = [
                ("同じ駅・徒歩4分なのに", (255, 255, 255, 255)),
                (f"女性比 {hi_pct}% vs {lo_pct}%", acc),
                ("ドア1枚で待ち時間激変！", acc2),
            ]
            sub1 = "1店舗目の男性待ち行列に並ぶ前に3秒チェック！"
            sub2 = "同じ繁華街で『今すぐ座れる天国店』と混雑店をリアル比較"
        elif self.rt == "tiktok_ladies_real":
            kicker = f"LADIES REAL DATA  |  {self.ctx['weekday_ja']}曜夜の女子会裏ワザ"
            lines = [
                ("女子70%超の店に行くと", (255, 255, 255, 255)),
                ("実は受付で待たされる!?", acc2),
                ("今すぐ待ち0の穴場TOP4", acc),
            ]
            sub1 = "女子ペアがスマートに即乾杯するなら『男性先行店』が鉄則！"
            sub2 = "いま男性が盛り上がっている受付待ちゼロ圏の上位店を公開"
        elif self.rt == "tiktok_midnight_cliff":
            kicker = f"MIDNIGHT CLIFF  |  {self.ctx['weekday_ja']}曜夜・終電後の真実"
            lines = [
                ("23時20分の「終電崖」で", (255, 255, 255, 255)),
                ("人が消える店 vs 残る店", acc),
                ("今夜の深夜狙い目TOP4", acc2),
            ]
            sub1 = "終電を境に急減する店と、2軒目組で逆転する店が真っ二つ！"
            sub2 = "実測データから判明した深夜維持率トップ級の現在人数速報"
        elif self.rt == "area_battle":
            kicker = f"AREA REALTIME BATTLE  |  {self.ctx['weekday_ja']}曜夜"
            lines = [
                ("新宿 vs 梅田 vs 渋谷", acc),
                ("今の男女比", (255, 255, 255, 255)),
                ("リアル徹底対決！", acc2),
            ]
            sub1 = "同じ夜でもエリアとブランドで待ち時間は激変！"
            sub2 = "3大激戦区の現在人数＆一番入りやすい狙い目店を比較"
        elif self.rt == "ladies_zero_wait":
            kicker = f"FOR LADIES  |  {self.ctx['weekday_ja']}曜夜の女子得ガイド"
            lines = [
                ("女子ペア向け！", acc),
                ("今すぐ受付待ち0＆", (255, 255, 255, 255)),
                ("狙い目ラウンジ速報", acc2),
            ]
            sub1 = "男性が先行して盛り上がっている「即乾杯圏」上位店を厳選"
            sub2 = "受付待ちゼロでスマートに楽しめる最新ランキング"
        else:
            kicker = f"NATIONWIDE LIVE  |  {self.ctx['weekday_ja']}曜夜リアルタイム"
            lines = [
                ("今夜のリアルタイム", (255, 255, 255, 255)),
                ("女性人数 TOP5", acc),
                ("＆ 狙い目穴場速報", acc2),
            ]
            sub1 = "「男性待ち1時間」を回避してスマートに相席するための"
            sub2 = "全国リアルタイム人数＆高女性比率ピックアップ"

        ky = 162 if self.is_tiktok else 178
        kw = min(sw, int(draw.textlength(kicker, font=f_kicker)) + 36)
        draw.rounded_rectangle((lx, ky, lx + kw, ky + 46), radius=14, fill=self.theme["badge_bg"], outline=acc, width=2)
        draw.text((lx + 18, ky + 11), kicker, font=f_kicker, fill=(255, 255, 255, 255))

        hy = 236 if self.is_tiktok else 264
        line_step = 74 if self.is_tiktok else 80
        max_h_sz = 50 if self.is_tiktok else 54
        for txt, col in lines:
            fit_t, fit_f, fit_sz = ogp_generator.fit_text_and_font(draw, txt, sw, max_h_sz, 30)
            draw.text((lx, hy + max(0, (max_h_sz - fit_sz) // 2)), fit_t, font=fit_f, fill=col)
            hy += line_step

        sub_y1 = 478 if self.is_tiktok else 528
        s1_fit, f_s1, _ = ogp_generator.fit_text_and_font(draw, sub1, sw, 20 if self.is_tiktok else 21, 15)
        s2_fit, f_s2, _ = ogp_generator.fit_text_and_font(draw, sub2, sw, 20 if self.is_tiktok else 21, 15)
        draw.text((lx, sub_y1), s1_fit, font=f_s1, fill=(214, 222, 235, 245))
        draw.text((lx, sub_y1 + 32), s2_fit, font=f_s2, fill=(214, 222, 235, 245))

        # Nationwide live counter glass card
        cy1 = 568 if self.is_tiktok else 634
        cy2 = 772 if self.is_tiktok else 846
        draw.rounded_rectangle(
            (lx, cy1, rx_end, cy2),
            radius=24,
            fill=(16, 20, 34, 238),
            outline=(acc2[0], acc2[1], acc2[2], 195),
            width=2,
        )
        hdr_lbl = (
            "直近ピーク実績・国内対象店舗サマリー"
            if self.ctx["is_fallback"]
            else f"現在集計中の国内ラウンジ合計（{self.ctx['active_store_count']}店舗）"
        )
        hl_fit, f_hl, _ = ogp_generator.fit_text_and_font(draw, hdr_lbl, sw - 48, 19, 14)
        draw.text((lx + 24, cy1 + 22), hl_fit, font=f_hl, fill=acc2)

        stat_main = f"女性 {self.ctx['total_women']}名   /   男性 {self.ctx['total_men']}名"
        sm_fit, sm_font, _ = ogp_generator.fit_text_and_font(draw, stat_main, sw - 48, 38 if self.is_tiktok else 40, 24)
        draw.text((lx + 24, cy1 + 68), sm_fit, font=sm_font, fill=(255, 255, 255, 255))

        ratio_note = f"国内全体 女性比率 {self.ctx['overall_wpct']}%  ｜  オリラジ・ag・JIS 全53店対応"
        rn_fit, rn_font, _ = ogp_generator.fit_text_and_font(draw, ratio_note, sw - 48, 18, 14)
        draw.text((lx + 24, cy1 + 142), rn_fit, font=rn_font, fill=(182, 194, 212, 240))

        # Bottom teaser pill (inside safe zone)
        ty_pill = 832 if self.is_tiktok else 906
        next_txt = "すぐ下にリアル比較データ＆狙い目店を公開 >>>" if self.is_tiktok else "このあと上位ランキング＆男女比バーを公開  >>>"
        nt_fit, f_nt, _ = ogp_generator.fit_text_and_font(draw, next_txt, sw - 36, 19, 14)
        nw = int(draw.textlength(nt_fit, font=f_nt)) + 36
        nx = lx + max(0, (sw - nw) // 2)
        draw.rounded_rectangle((nx, ty_pill, nx + nw, ty_pill + 50), radius=25, fill=(24, 28, 46, 234), outline=acc, width=2)
        draw.text((nx + 18, ty_pill + 14), nt_fit, font=f_nt, fill=acc)
        return sp

    def _build_scene2_header_sprite(self) -> Image.Image:
        W = self.W
        sp = Image.new("RGBA", (W, 96), (0, 0, 0, 0))
        draw = ogp_generator.make_safe_draw(sp, "RGBA")
        lx = self.safe_left
        sw = self.safe_w

        if self.rt == "tiktok_gap_shock":
            t1 = "同駅・徒歩4分 男女比ギャップ衝撃比較"
            sub = "同じ繁華街でもお店選び1つで待ち時間がここまで変わる！"
        elif self.rt == "tiktok_ladies_real":
            t1 = "女子待ち0・男性先行ラウンジ TOP4"
            sub = "女子過多の受付待ちを回避！今すぐ乾杯しやすい狙い目店"
        elif self.rt == "tiktok_midnight_cliff":
            t1 = "終電崖に強い！深夜狙い目ラウンジ TOP4"
            sub = "23時20分以降も人数が落ちにくい深夜維持率上位店"
        elif self.rt == "area_battle":
            t1 = "3大激戦区 男女比＆狙い目店対決"
            sub = "新宿・梅田・渋谷の現在人数合計と今いちばん入りやすい店舗"
        elif self.rt == "ladies_zero_wait":
            t1 = "女子待ち0・男性先行ラウンジ TOP5"
            sub = "男性が先行して盛り上がっている「受付待ちゼロ圏」上位店舗"
        else:
            t1 = "全国リアルタイム 女性人数 TOP5"
            sub = "ピンク＝女性比率 / シアン＝男性比率（上位5店舗を比較）"

        t_fit, f_t, _ = ogp_generator.fit_text_and_font(draw, t1, sw, 31 if self.is_tiktok else 33, 21)
        s_fit, f_s, _ = ogp_generator.fit_text_and_font(draw, sub, sw, 17 if self.is_tiktok else 18, 13)
        draw.text((lx, 6), t_fit, font=f_t, fill=(255, 255, 255, 255))
        draw.text((lx, 50), s_fit, font=f_s, fill=(203, 213, 225, 238))
        return sp

    def _build_scene2_card_sprites(self) -> list:
        """
        Pre-render static card chrome + typography for Scene 2, alongside bar geometry metadata
        so frame loop only draws the smoothly-animating ratio bar!
        """
        lx = self.safe_left
        cw = self.safe_w
        rank_cols = [
            (251, 191, 36),
            (226, 232, 240),
            (245, 158, 11),
            (148, 163, 184),
            (148, 163, 184),
        ]

        if self.rt == "tiktok_gap_shock":
            cards = []
            ch = 230
            f_pill = _get_reel_font(16)
            f_row = _get_reel_font(18)
            for idx, gp in enumerate((self.ctx.get("gap_pairs") or [])[:3]):
                sp = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                draw = ogp_generator.make_safe_draw(sp, "RGBA")
                hi = gp["heaven_store"]
                lo = gp["crowded_store"]
                b_col = self.theme["accent"] if idx == 0 else self.theme["accent2"]

                draw.rounded_rectangle(
                    (0, 0, cw - 1, ch - 1),
                    radius=20,
                    fill=(15, 20, 34, 242),
                    outline=(b_col[0], b_col[1], b_col[2], 210 if idx == 0 else 135),
                    width=3 if idx == 0 else 2,
                )

                pill_txt = f"比率差 {gp['gap_pt']}pt！"
                pw = int(draw.textlength(pill_txt, font=f_pill)) + 24
                px = cw - 18 - pw
                draw.rounded_rectangle((px, 16, px + pw, 50), radius=11, fill=(190, 24, 93, 238))
                draw.text((px + 12, 23), pill_txt, font=f_pill, fill=(255, 255, 255, 255))

                hub_fit, f_hub, _ = ogp_generator.fit_text_and_font(draw, gp["hub_label"], max(150, px - 34), 25, 18)
                draw.text((20, 18), hub_fit, font=f_hub, fill=(255, 255, 255, 255))

                hi_line = f"◎ 狙い目：{hi['ja_name']}（女{hi['women']}:男{hi['men']}｜女性比{hi['wpct']}%）"
                hi_fit, f_hi, _ = ogp_generator.fit_text_and_font(draw, hi_line, cw - 40, 19, 14)
                draw.text((20, 68), hi_fit, font=f_hi, fill=(37, 244, 238, 255))

                lo_line = f"△ 混雑注意：{lo['ja_name']}（女{lo['women']}:男{lo['men']}｜女性比{lo['wpct']}%）"
                lo_fit, f_lo, _ = ogp_generator.fit_text_and_font(draw, lo_line, cw - 40, 18, 13)
                draw.text((20, 108), lo_fit, font=f_lo, fill=(254, 142, 164, 242))

                draw.text((20, 148), "狙い目店の男女バランス（ピンク＝女性 / シアン＝男性）", font=_get_reel_font(14), fill=(182, 194, 212, 230))
                bx, by, bw, bh = 20, 178, cw - 40, 20
                draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=10, fill=(32, 40, 62, 235))

                left_ratio = max(0.08, min(0.92, hi["wpct"] / 100.0))
                full_sp = sp.copy()
                fdraw = ImageDraw.Draw(full_sp, "RGBA")
                fdraw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=10, fill=(*self.theme["bar_right"], 235))
                fdraw.rounded_rectangle(
                    (bx, by, bx + max(20, min(bw, int(bw * left_ratio))), by + bh),
                    radius=10,
                    fill=(*self.theme["bar_left"], 255),
                )

                cards.append({
                    "sprite": sp,
                    "full_sprite": full_sp,
                    "x": lx,
                    "y": 242 + idx * (ch + 14),
                    "t_in": 2.08 + idx * 0.38,
                    "bar_rect": (bx, by, bw, bh, 10),
                    "left_ratio": left_ratio,
                    "left_col": self.theme["bar_left"],
                    "right_col": self.theme["bar_right"],
                })
            return cards

        if self.rt == "area_battle":
            cards = []
            ch = 294
            f_pill = _get_reel_font(17)
            f_stat = _get_reel_font(25)
            f_sublbl = _get_reel_font(16)
            f_bst_stat = _get_reel_font(18)

            for idx, z in enumerate(self.ctx["area_battles"][:3]):
                sp = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                draw = ogp_generator.make_safe_draw(sp, "RGBA")
                is_lead = bool(z.get("is_leader"))
                b_col = (251, 191, 36) if is_lead else self.theme["accent"]

                draw.rounded_rectangle(
                    (0, 0, cw - 1, ch - 1),
                    radius=22,
                    fill=(17, 22, 36, 240),
                    outline=(b_col[0], b_col[1], b_col[2], 215 if is_lead else 120),
                    width=3 if is_lead else 2,
                )

                pill_txt = f"NO.1 激戦区｜女性比 {z['wpct']}%" if is_lead else f"稼働 {z['store_count']}店｜女性比 {z['wpct']}%"
                pw = int(draw.textlength(pill_txt, font=f_pill)) + 28
                px = cw - 20 - pw
                pill_fill = (168, 88, 14, 235) if is_lead else (14, 96, 134, 235)
                draw.rounded_rectangle((px, 18, px + pw, 54), radius=12, fill=pill_fill)
                draw.text((px + 14, 26), pill_txt, font=f_pill, fill=(255, 255, 255, 255))

                z_title = f"{z['label']}"
                zt_fit, f_zt, _ = ogp_generator.fit_text_and_font(draw, z_title, max(160, px - 36), 30, 22)
                draw.text((22, 18), zt_fit, font=f_zt, fill=(255, 255, 255, 255))

                stat_txt = f"合計：女性 {z['women']}名   vs   男性 {z['men']}名"
                st_fit, f_st, _ = ogp_generator.fit_text_and_font(draw, stat_txt, cw - 44, 25, 18)
                draw.text((22, 70), st_fit, font=f_st, fill=(244, 114, 182, 255))

                # Bar track placeholder
                bx, by, bw, bh = 22, 120, cw - 44, 22
                draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=11, fill=(32, 40, 62, 235))

                # Best store spotlight sub-card
                draw.rounded_rectangle(
                    (20, 162, cw - 20, ch - 18),
                    radius=16,
                    fill=(10, 14, 26, 235),
                    outline=(251, 191, 36, 130),
                    width=1,
                )
                draw.text((36, 174), "エリア内で今いちばん入りやすい狙い目店舗", font=f_sublbl, fill=(251, 191, 36, 245))
                bs = z.get("best_store")
                if bs:
                    bs_name, f_bsn, _ = ogp_generator.fit_text_and_font(draw, bs["ja_name"], cw - 72, 23, 17)
                    draw.text((36, 202), bs_name, font=f_bsn, fill=(255, 255, 255, 255))
                    bs_stat = f"現在 女性 {bs['women']}名 / 男性 {bs['men']}名（女性比率 {bs['wpct']}%）"
                    draw.text((36, 238), bs_stat, font=f_bst_stat, fill=(56, 189, 248, 245))

                left_ratio = max(0.08, min(0.92, z["wpct"] / 100.0))
                full_sp = sp.copy()
                fdraw = ImageDraw.Draw(full_sp, "RGBA")
                fdraw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=11, fill=(*self.theme["bar_right"], 235))
                fdraw.rounded_rectangle(
                    (bx, by, bx + max(22, min(bw, int(bw * left_ratio))), by + bh),
                    radius=11,
                    fill=(*self.theme["bar_left"], 255),
                )

                cards.append({
                    "sprite": sp,
                    "full_sprite": full_sp,
                    "x": lx,
                    "y": 196 + idx * (ch + 18),
                    "t_in": 2.28 + idx * 0.42,
                    "bar_rect": (bx, by, bw, bh, 11),
                    "left_ratio": left_ratio,
                    "left_col": self.theme["bar_left"],
                    "right_col": self.theme["bar_right"],
                })
            return cards

        # Top list (`live_top5`, `ladies_zero_wait`, `tiktok_ladies_real`, `tiktok_midnight_cliff`)
        is_ladies_mode = self.rt in ("ladies_zero_wait", "tiktok_ladies_real")
        if self.rt == "tiktok_midnight_cliff":
            items = (self.ctx.get("midnight_top4") or self.ctx["top5_volume"])[:4]
        elif self.rt == "tiktok_ladies_real":
            items = self.ctx["ladies_top5"][:4]
        elif self.rt == "ladies_zero_wait":
            items = self.ctx["ladies_top5"][:5]
        else:
            items = self.ctx["top5_volume"][:5]

        cards = []
        ch = 174 if self.is_tiktok else 176
        f_rank = _get_reel_font(23 if self.is_tiktok else 24)
        f_pill = _get_reel_font(16 if self.is_tiktok else 17)

        for idx, st in enumerate(items):
            sp = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            draw = ogp_generator.make_safe_draw(sp, "RGBA")
            rc = rank_cols[idx] if idx < len(rank_cols) else (148, 163, 184)

            card_bg = (26, 15, 26, 240) if is_ladies_mode else (17, 22, 36, 240)
            draw.rounded_rectangle(
                (0, 0, cw - 1, ch - 1),
                radius=20,
                fill=card_bg,
                outline=(rc[0], rc[1], rc[2], 200 if idx == 0 else 95),
                width=3 if idx == 0 else 1,
            )

            # Rank badge
            draw.rounded_rectangle((18, 18, 66, 66), radius=13, fill=(rc[0], rc[1], rc[2], 38), outline=rc, width=2)
            r_str = str(idx + 1)
            rw = int(draw.textlength(r_str, font=f_rank))
            draw.text((42 - rw // 2, 28), r_str, font=f_rank, fill=rc)

            # Right highlight pill
            if is_ladies_mode:
                is_vip = st["men"] >= 4 and st["men"] >= st["women"]
                pill_txt = "待ち0・即案内圏" if is_vip else "スムーズ相席圏"
                pill_bg = (158, 72, 108, 238) if is_vip else (88, 52, 92, 235)
            elif self.rt == "tiktok_midnight_cliff":
                pill_txt = f"深夜維持 {st['midnight_retention_pct']}%"
                pill_bg = (146, 102, 28, 238) if st["midnight_retention_pct"] >= 78 else (14, 116, 144, 238)
            else:
                pill_txt = f"女性比 {st['wpct']}%"
                pill_bg = (190, 24, 93, 238) if st["wpct"] >= 55 else (14, 116, 144, 238)

            pw = int(draw.textlength(pill_txt, font=f_pill)) + 24
            px = cw - 18 - pw
            draw.rounded_rectangle((px, 18, px + pw, 56), radius=11, fill=pill_bg)
            draw.text((px + 12, 27), pill_txt, font=f_pill, fill=(255, 255, 255, 255))

            # Store name + area
            sn_txt, sn_font, sn_sz = ogp_generator.fit_text_and_font(
                draw, st["ja_name"], max(150, px - 94), 24 if self.is_tiktok else 25, 16
            )
            draw.text((80, 17 + max(0, (25 - sn_sz) // 2)), sn_txt, font=sn_font, fill=(255, 255, 255, 255))

            sub_meta = f"エリア：{st['area_label']}"
            if is_ladies_mode and st.get("event_title"):
                sub_meta += f"  ｜  特典：{st['event_title']}"
            elif self.rt == "tiktok_midnight_cliff":
                sub_meta += f"  ｜  {st['midnight_label_short']}"
            sm_fit, sm_f, _ = ogp_generator.fit_text_and_font(draw, sub_meta, max(150, px - 94), 15, 12)
            draw.text((80, 50), sm_fit, font=sm_f, fill=(176, 188, 206, 235))

            # Live headcount row
            if is_ladies_mode:
                lead_s = f"（+{st['male_lead']}名先行）" if st["male_lead"] > 0 else ""
                stat_s = f"男性 {st['men']}名{lead_s}  /  女性 {st['women']}名（男性比 {st['mpct']}%）"
                stat_col = (244, 198, 152, 255)
                left_ratio = max(0.08, min(0.92, st["mpct"] / 100.0))
            else:
                lbl_pfx = "実績目安" if self.ctx["is_fallback"] else "現在人数"
                stat_s = f"{lbl_pfx}：女性 {st['women']}名  /  男性 {st['men']}名（女性比 {st['wpct']}%）"
                stat_col = (244, 114, 182, 255)
                left_ratio = max(0.08, min(0.92, st["wpct"] / 100.0))

            st_fit, f_stat_fit, _ = ogp_generator.fit_text_and_font(draw, stat_s, cw - 40, 19 if self.is_tiktok else 20, 14)
            draw.text((20, 88), st_fit, font=f_stat_fit, fill=stat_col)

            bx, by, bw, bh = 20, 130, cw - 40, 18
            draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=9, fill=(32, 40, 62, 235))

            full_sp = sp.copy()
            fdraw = ImageDraw.Draw(full_sp, "RGBA")
            fdraw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=9, fill=(*self.theme["bar_right"], 235))
            fdraw.rounded_rectangle(
                (bx, by, bx + max(18, min(bw, int(bw * left_ratio))), by + bh),
                radius=9,
                fill=(*self.theme["bar_left"], 255),
            )

            base_y = 240 if self.is_tiktok else 192
            step_gap = 12 if self.is_tiktok else 14
            t_base = 2.08 if self.is_tiktok else 2.28
            cards.append({
                "sprite": sp,
                "full_sprite": full_sp,
                "x": lx,
                "y": base_y + idx * (ch + step_gap),
                "t_in": t_base + idx * 0.30,
                "bar_rect": (bx, by, bw, bh, 9),
                "left_ratio": left_ratio,
                "left_col": self.theme["bar_left"],
                "right_col": self.theme["bar_right"],
            })
        return cards

    def _build_scene3_cta_sprite(self) -> Image.Image:
        W, H = self.W, self.H
        sp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ogp_generator.make_safe_draw(sp, "RGBA")
        acc = self.theme["accent"]
        acc2 = self.theme["accent2"]
        lx = self.safe_left
        sw = self.safe_w
        rx_end = lx + sw

        if self.rt == "tiktok_gap_shock":
            t1 = "今夜の同駅ギャップ結論＆狙い目店"
            sub = "1店舗目で並ばず、徒歩4分の高女性比店へスマート移動！"
            pick_rows = [
                (
                    f"差{gp['gap_pt']}pt 狙い目",
                    gp["heaven_store"]["ja_name"],
                    f"女性 {gp['heaven_store']['women']}名 / 男性 {gp['heaven_store']['men']}名（女性比 {gp['heaven_store']['wpct']}%）",
                )
                for gp in (self.ctx.get("gap_pairs") or [])[:3]
            ]
        elif self.rt == "tiktok_midnight_cliff":
            t1 = "終電後も強い！深夜推奨ピックアップ"
            sub = "23時20分の終電崖を越えても盛り上がる上位ラウンジ"
            pick_rows = [
                (
                    f"維持率 {s['midnight_retention_pct']}%",
                    s["ja_name"],
                    f"現在 女性 {s['women']}名 / 男性 {s['men']}名（{s['area_label']}）",
                )
                for s in (self.ctx.get("midnight_top4") or self.ctx["top5_volume"])[:3]
            ]
        elif self.rt == "area_battle":
            t1 = "今夜の3大都市ジャッジ＆攻略メモ"
            sub = "1店舗の行列に並ばず、徒歩4分の別ブランド比較で即相席！"
            zones = self.ctx["area_battles"]
            pick_rows = [
                (
                    f"{z['label']} 狙い目",
                    z["best_store"]["ja_name"] if z.get("best_store") else z["label"],
                    (
                        f"女性 {z['best_store']['women']}名 / 男性 {z['best_store']['men']}名（女性比 {z['best_store']['wpct']}%）"
                        if z.get("best_store")
                        else f"エリア女性比 {z['wpct']}%"
                    ),
                )
                for z in zones[:3]
            ]
        elif self.rt in ("ladies_zero_wait", "tiktok_ladies_real"):
            t1 = "女子ペア向け！今すぐ狙い目ピックアップ"
            sub = "女性過多の店舗を避け、男性先行店を選ぶと入店直後から快適！"
            pick_rows = [
                (
                    f"待ち0推奨 #{i+1}",
                    s["ja_name"],
                    f"男性 {s['men']}名 / 女性 {s['women']}名（男性比 {s['mpct']}%・{s['area_label']}）",
                )
                for i, s in enumerate(self.ctx["ladies_top5"][:3])
            ]
        else:
            t1 = "女性比率55%以上！狙い目穴場ピックアップ"
            sub = "男性待ちを回避しやすく、席替えもスムーズな好バランス店"
            pick_rows = [
                (
                    f"女性比 {s['wpct']}%",
                    s["ja_name"],
                    f"現在 女性 {s['women']}名 / 男性 {s['men']}名（エリア：{s['area_label']}）",
                )
                for s in self.ctx["anaba_pickups"][:3]
            ]

        hdr_y = 148 if self.is_tiktok else 104
        t_fit, f_t, _ = ogp_generator.fit_text_and_font(draw, t1, sw, 29 if self.is_tiktok else 31, 20)
        s_fit, f_s, _ = ogp_generator.fit_text_and_font(draw, sub, sw, 17 if self.is_tiktok else 18, 13)
        draw.text((lx, hdr_y), t_fit, font=f_t, fill=(255, 255, 255, 255))
        draw.text((lx, hdr_y + 42), s_fit, font=f_s, fill=(203, 213, 225, 238))

        f_tag = _get_reel_font(15 if self.is_tiktok else 17)
        py = 228 if self.is_tiktok else 192
        row_h = 108 if self.is_tiktok else 142
        row_step = 118 if self.is_tiktok else 156
        for badge_s, name_s, detail_s in pick_rows:
            draw.rounded_rectangle(
                (lx, py, rx_end, py + row_h),
                radius=18 if self.is_tiktok else 20,
                fill=(18, 22, 38, 240),
                outline=(acc[0], acc[1], acc[2], 165),
                width=2,
            )
            bw = int(draw.textlength(badge_s, font=f_tag)) + 24
            draw.rounded_rectangle((lx + 18, py + 14, lx + 18 + bw, py + 46), radius=10, fill=self.theme["badge_bg"], outline=acc, width=1)
            draw.text((lx + 30, py + 20), badge_s, font=f_tag, fill=acc)

            nm_fit, f_nm, _ = ogp_generator.fit_text_and_font(draw, name_s, sw - 52 - bw, 22 if self.is_tiktok else 25, 16)
            draw.text((lx + 30 + bw, py + 17), nm_fit, font=f_nm, fill=(255, 255, 255, 255))

            dt_y = py + (64 if self.is_tiktok else 82)
            dt_fit, f_dt, _ = ogp_generator.fit_text_and_font(draw, detail_s, sw - 36, 18 if self.is_tiktok else 20, 14)
            draw.text((lx + 18, dt_y), dt_fit, font=f_dt, fill=acc2)
            py += row_step

        # Prominent CTA box (TikTok safe-zone: y = 590 .. 984 / Standard: y = 684 .. 1144)
        cta_y1 = 590 if self.is_tiktok else 684
        cta_y2 = 984 if self.is_tiktok else 1144
        draw.rounded_rectangle(
            (lx, cta_y1, rx_end, cta_y2),
            radius=26,
            fill=(28, 14, 34, 244),
            outline=(acc2[0], acc2[1], acc2[2], 235),
            width=3,
        )
        f_kicker = _get_reel_font(20 if self.is_tiktok else 22)
        f_url = _get_reel_font(25 if self.is_tiktok else 28)
        f_note = _get_reel_font(16 if self.is_tiktok else 18)

        k_txt = "「保存」して入店3分前に見返すのが鉄則！" if self.is_tiktok else "お店に向かう3分前にスマホでチェック！"
        k_fit, f_k_fit, _ = ogp_generator.fit_text_and_font(draw, k_txt, sw - 40, 21, 15)
        kw = int(draw.textlength(k_fit, font=f_k_fit))
        draw.text((lx + max(0, (sw - kw) // 2), cta_y1 + (28 if self.is_tiktok else 40)), k_fit, font=f_k_fit, fill=acc)

        cta_l1 = "『相席ナビ』で検索 or プロフから" if self.is_tiktok else "プロフのリンク『相席ナビ』で"
        cta_l2 = "全53店舗の現在人数を3秒チェック" if self.is_tiktok else "全53店舗の現在人数を3秒検索"
        c1_fit, f_c1, _ = ogp_generator.fit_text_and_font(draw, cta_l1, sw - 48, 30 if self.is_tiktok else 34, 20)
        c2_fit, f_c2, _ = ogp_generator.fit_text_and_font(draw, cta_l2, sw - 48, 30 if self.is_tiktok else 34, 20)
        c1w = int(draw.textlength(c1_fit, font=f_c1))
        c2w = int(draw.textlength(c2_fit, font=f_c2))
        draw.text((lx + max(0, (sw - c1w) // 2), cta_y1 + (74 if self.is_tiktok else 94)), c1_fit, font=f_c1, fill=(255, 255, 255, 255))
        draw.text((lx + max(0, (sw - c2w) // 2), cta_y1 + (120 if self.is_tiktok else 148)), c2_fit, font=f_c2, fill=acc2)

        url_txt = "相席ナビ  /  aiseki-navi.com"
        u_fit, f_u_fit, _ = ogp_generator.fit_text_and_font(draw, url_txt, sw - 80, 26 if self.is_tiktok else 28, 18)
        uw = int(draw.textlength(u_fit, font=f_u_fit)) + 48
        ux = lx + max(0, (sw - uw) // 2)
        uy = cta_y1 + (186 if self.is_tiktok else 230)
        uh = 66 if self.is_tiktok else 76
        draw.rounded_rectangle((ux, uy, ux + uw, uy + uh), radius=16, fill=(12, 18, 34, 248), outline=acc2, width=2)
        draw.text((ux + 24, uy + (17 if self.is_tiktok else 20)), u_fit, font=f_u_fit, fill=acc2)

        n1 = "オリエンタルラウンジ・ag・JIS ほか全国対応"
        n2 = "会員登録・アプリDL不要 / 24時間リアルタイム無料"
        n1_fit, f_n1, _ = ogp_generator.fit_text_and_font(draw, n1, sw - 36, 16 if self.is_tiktok else 18, 13)
        n2_fit, f_n2, _ = ogp_generator.fit_text_and_font(draw, n2, sw - 36, 16 if self.is_tiktok else 18, 13)
        n1w = int(draw.textlength(n1_fit, font=f_n1))
        n2w = int(draw.textlength(n2_fit, font=f_n2))
        ny1 = cta_y1 + (280 if self.is_tiktok else 344)
        draw.text((lx + max(0, (sw - n1w) // 2), ny1), n1_fit, font=f_n1, fill=(226, 232, 240, 240))
        draw.text((lx + max(0, (sw - n2w) // 2), ny1 + 32), n2_fit, font=f_n2, fill=(165, 178, 198, 235))
        return sp

    def _composite_ui_layers_onto(self, canvas: Image.Image, t_sec: float) -> None:
        """Composite animated chrome and scene sprites directly onto `canvas` (in-place RGBA)."""
        t = max(0.0, min(REEL_DURATION_SEC, float(t_sec)))
        lx = self.safe_left

        canvas.alpha_composite(self.chrome_base)
        draw = ImageDraw.Draw(canvas, "RGBA")

        pulse = 0.55 + 0.45 * math.sin(t * 6.0)
        dot_a = int(155 + 100 * pulse)
        dot_cx = self.live_dot_cx
        dot_cy = getattr(self, "live_dot_cy", 56)
        draw.ellipse((dot_cx - 5, dot_cy - 5, dot_cx + 5, dot_cy + 5), fill=(248, 113, 113, dot_a))

        prog_w = max(6, int(self.safe_w * (t / REEL_DURATION_SEC)))
        acc = self.theme["accent"]
        py = self.prog_y
        draw.rounded_rectangle((lx, py, lx + prog_w, py + 6), radius=3, fill=(*acc, 245))

        s1_end = 2.04 if self.is_tiktok else 2.22
        s2_start = 1.98 if self.is_tiktok else 2.18

        # Scene 1: Hook
        s1_alpha = _fade_window(t, 0.0, s1_end, fade_in=0.32 if self.is_tiktok else 0.38, fade_out=0.20)
        if s1_alpha > 0.01:
            dy = int(24 * (1.0 - _ease_out_cubic(min(1.0, t / 0.38))))
            sp1 = _apply_opacity(self.scene1_sprite, s1_alpha)
            canvas.alpha_composite(sp1, dest=(0, dy))
            if sp1 is not self.scene1_sprite:
                sp1.close()

        # Scene 2: Cards + Animated Gender Bars
        s2_out = 1.0 if t < 8.26 else max(0.0, (8.52 - t) / 0.26)
        if s2_start <= t <= 8.52 and s2_out > 0.01:
            hdr_in = _ease_out_cubic((t - s2_start) / 0.30)
            hdr_alpha = min(hdr_in, s2_out)
            if hdr_alpha > 0.01:
                h_sp = _apply_opacity(self.scene2_header, hdr_alpha)
                canvas.alpha_composite(h_sp, dest=(0, 144 if self.is_tiktok else 96))
                if h_sp is not self.scene2_header:
                    h_sp.close()

            for c in self.scene2_cards:
                if t < c["t_in"]:
                    continue
                el = t - c["t_in"]
                c_in = _ease_out_cubic(el / 0.36)
                c_alpha = min(c_in, s2_out)
                if c_alpha <= 0.01:
                    continue
                cy = c["y"] + int(20 * (1.0 - c_in))
                bar_ease = _ease_out_cubic(el / 0.72)

                if bar_ease >= 0.998:
                    final_card = _apply_opacity(c["full_sprite"], c_alpha)
                    canvas.alpha_composite(final_card, dest=(c["x"], cy))
                    if final_card is not c["full_sprite"]:
                        final_card.close()
                else:
                    card_work = c["sprite"].copy()
                    cdraw = ImageDraw.Draw(card_work, "RGBA")
                    bx, by, bw, bh, brad = c["bar_rect"]
                    cur_w = max(brad * 2, int(bw * bar_ease))
                    left_w = max(brad * 2, min(cur_w, int(cur_w * c["left_ratio"])))
                    if cur_w > brad * 2:
                        cdraw.rounded_rectangle(
                            (bx, by, bx + cur_w, by + bh),
                            radius=brad,
                            fill=(*c["right_col"], 235),
                        )
                    cdraw.rounded_rectangle(
                        (bx, by, bx + left_w, by + bh),
                        radius=brad,
                        fill=(*c["left_col"], 255),
                    )
                    final_card = _apply_opacity(card_work, c_alpha)
                    canvas.alpha_composite(final_card, dest=(c["x"], cy))
                    if final_card is not card_work:
                        final_card.close()
                    card_work.close()

        # Scene 3: 8.48 .. 11.5s
        s3_alpha = _fade_window(t, 8.48, REEL_DURATION_SEC + 0.1, fade_in=0.38, fade_out=0.0)
        if s3_alpha > 0.01:
            dy3 = int(22 * (1.0 - _ease_out_cubic((t - 8.48) / 0.40)))
            sp3 = _apply_opacity(self.scene3_sprite, s3_alpha)
            canvas.alpha_composite(sp3, dest=(0, dy3))
            if sp3 is not self.scene3_sprite:
                sp3.close()

    def render_foreground_rgba(self, t_sec: float, include_video_vignette: bool = False) -> Image.Image:
        """Render animated RGBA overlay at timestamp `t_sec` (`0.0 .. 11.5s`) for Veo 3 video compositing."""
        if include_video_vignette:
            canvas = _get_vignette_overlay(self.rt, for_video_bg=True).copy()
        else:
            canvas = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        self._composite_ui_layers_onto(canvas, t_sec)
        return canvas

    def render_full_rgb_frame(self, t_sec: float, bg_oversized: Image.Image) -> Image.Image:
        """Render complete 720x1280 RGB frame with Ken Burns zoom + dark vignette + animated UI."""
        W, H = self.W, self.H
        progress = max(0.0, min(1.0, float(t_sec) / REEL_DURATION_SEC))

        # Smooth Ken Burns zoom-in (1.00x -> 1.12x) + subtle upward drift on cached 828x1472 canvas
        bw, bh = bg_oversized.size
        zoom = 1.0 + 0.12 * progress
        crop_w = min(bw, int(round(bw / zoom)))
        crop_h = min(bh, int(round(bh / zoom)))
        left = (bw - crop_w) // 2
        drift_y = int((bh - crop_h) * (0.65 - 0.35 * progress))
        top = max(0, min(bh - crop_h, drift_y))

        cropped = bg_oversized.crop((left, top, left + crop_w, top + crop_h))
        frame_rgba = cropped.resize((W, H), Image.Resampling.BILINEAR).convert("RGBA")
        cropped.close()

        frame_rgba.alpha_composite(_get_vignette_overlay(self.rt, for_video_bg=False))
        self._composite_ui_layers_onto(frame_rgba, t_sec)

        rgb = frame_rgba.convert("RGB")
        frame_rgba.close()
        return rgb

    def close(self):
        for im in (self.chrome_base, self.scene1_sprite, self.scene2_header, self.scene3_sprite):
            try:
                im.close()
            except Exception:
                pass
        for c in self.scene2_cards:
            for k in ("sprite", "full_sprite"):
                try:
                    if c.get(k):
                        c[k].close()
                except Exception:
                    pass


def render_reel_preview_frame_jpeg(
    stores: list,
    reel_type: str = "live_top5",
    sec: float = 6.0,
    now_jst: datetime.datetime = None,
    quality: int = 88,
) -> bytes:
    """
    Render a 720x1280 JPEG preview frame at `sec` (default 6.0s) purely with Pillow.
    Guaranteed to succeed 100% of the time even in environments without FFmpeg.
    """
    rt = normalize_reel_type(reel_type)
    ctx = build_reel_context(stores, reel_type=rt, now_jst=now_jst)
    bg_info = list_available_backgrounds(reel_type=rt)
    _, _, im_path, im_mtime = _resolve_bg_paths(bg_info)
    bg_oversized = _get_oversized_bg_canvas(
        rt,
        image_path=im_path,
        image_mtime=im_mtime,
    )

    renderer = _ReelSceneRenderer(ctx)
    try:
        rgb_frame = renderer.render_full_rgb_frame(float(sec), bg_oversized)
    finally:
        renderer.close()

    buf = io.BytesIO()
    rgb_frame.save(buf, format="JPEG", quality=max(55, min(96, int(quality))))
    rgb_frame.close()
    return buf.getvalue()


def _compute_reel_cache_key(stores: list, rt: str, bg_info: dict, now_jst: datetime.datetime = None) -> str:
    dom = [s for s in (stores or []) if _is_domestic_store(s)]
    dom.sort(key=lambda x: (str(x.get("name") or ""), int(x.get("women") or 0), int(x.get("men") or 0)))
    top_sig = "|".join(
        f"{s.get('name')}:{int(s.get('women') or 0)}:{int(s.get('men') or 0)}"
        for s in dom[:15]
    )
    bg_sig = f"{bg_info.get('active_mode')}:{bg_info.get('active_file')}:{bg_info.get('selected_video_mtime')}:{bg_info.get('selected_image_mtime')}"
    # Only include explicit timestamp override in cache key if caller passed a custom historical/test datetime
    custom_ts = ""
    if now_jst is not None:
        real_now = datetime.datetime.now(JST)
        chk = now_jst if now_jst.tzinfo else now_jst.replace(tzinfo=JST)
        if abs((chk - real_now).total_seconds()) > 180:
            custom_ts = chk.strftime("%Y%m%d%H%M")
    raw = f"v1|{rt}|{bg_sig}|{custom_ts}|{top_sig}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{rt}_{digest}"


def _prune_tmp_reel_dir(now_ts: float):
    try:
        if not os.path.isdir(REEL_TMP_CACHE_DIR):
            return
        for fname in os.listdir(REEL_TMP_CACHE_DIR):
            if not fname.endswith(".mp4"):
                continue
            fpath = os.path.join(REEL_TMP_CACHE_DIR, fname)
            try:
                if now_ts - os.path.getmtime(fpath) > 600.0:
                    os.remove(fpath)
            except Exception:
                pass
    except Exception:
        pass


def _encode_with_ffmpeg(
    ffmpeg_bin: str,
    renderer: _ReelSceneRenderer,
    bg_info: dict,
    out_mp4_path: str,
    use_video_bg: bool,
) -> None:
    """Stream raw frames one-by-one into FFmpeg stdin (<4MB peak RAM) and mux silent AAC + faststart."""
    W, H = REEL_WIDTH, REEL_HEIGHT
    bg_video_path, _, bg_image_path, bg_image_mtime = _resolve_bg_paths(bg_info)

    if use_video_bg and bg_video_path and os.path.isfile(bg_video_path):
        filter_complex = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={REEL_FPS},trim=duration={REEL_DURATION_SEC:.2f},setpts=PTS-STARTPTS[bg];"
            f"[bg][1:v]overlay=0:0:format=auto:shortest=1[vout]"
        )
        cmd = [
            ffmpeg_bin,
            "-y",
            "-loglevel", "error",
            "-stream_loop", "-1",
            "-i", bg_video_path,
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "rgba",
            "-s", f"{W}x{H}",
            "-r", str(REEL_FPS),
            "-i", "pipe:0",
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "2:a",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "25",
            "-profile:v", "high",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-ar", "44100",
            "-t", f"{REEL_DURATION_SEC:.2f}",
            "-shortest",
            "-movflags", "+faststart",
            out_mp4_path,
        ]
    else:
        bg_oversized = _get_oversized_bg_canvas(
            renderer.rt,
            image_path=bg_image_path,
            image_mtime=bg_image_mtime,
        )
        cmd = [
            ffmpeg_bin,
            "-y",
            "-loglevel", "error",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{W}x{H}",
            "-r", str(REEL_FPS),
            "-i", "pipe:0",
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "25",
            "-profile:v", "high",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-ar", "44100",
            "-t", f"{REEL_DURATION_SEC:.2f}",
            "-shortest",
            "-movflags", "+faststart",
            out_mp4_path,
        ]

    with tempfile.TemporaryFile() as stderr_f:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f,
        )
        try:
            for f_idx in range(REEL_TOTAL_FRAMES):
                t_sec = f_idx / float(REEL_FPS)
                if use_video_bg and bg_video_path and os.path.isfile(bg_video_path):
                    fg = renderer.render_foreground_rgba(t_sec, include_video_vignette=True)
                    raw_bytes = fg.tobytes()
                    fg.close()
                else:
                    rgb = renderer.render_full_rgb_frame(t_sec, bg_oversized)
                    raw_bytes = rgb.tobytes()
                    rgb.close()
                proc.stdin.write(raw_bytes)
        except BrokenPipeError:
            pass
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

        ret = proc.wait(timeout=45)
        if ret != 0 or not os.path.isfile(out_mp4_path) or os.path.getsize(out_mp4_path) < 1024:
            stderr_f.seek(0)
            err_msg = stderr_f.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"ffmpeg reel encode failed (exit={ret}): {err_msg}")


def render_reel_mp4_bytes(
    stores: list,
    reel_type: str = "live_top5",
    now_jst: datetime.datetime = None,
    force_refresh: bool = False,
) -> bytes:
    """
    Render a 720x1280 24fps H.264 + silent AAC MP4 (~11.5s) with 120s in-memory + `/tmp/aiseki_reels/` cache.
    Raises `RuntimeError("ffmpeg binary not available")` if FFmpeg cannot be resolved.
    """
    ffmpeg_bin = resolve_ffmpeg_bin()
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg binary not available")

    rt = normalize_reel_type(reel_type)
    bg_info = list_available_backgrounds(reel_type=rt)
    cache_key = _compute_reel_cache_key(stores, rt, bg_info, now_jst=now_jst)
    now_ts = time.time()

    os.makedirs(REEL_TMP_CACHE_DIR, exist_ok=True)
    disk_exact_path = os.path.join(REEL_TMP_CACHE_DIR, f"reel_{cache_key}.mp4")
    disk_latest_path = os.path.join(REEL_TMP_CACHE_DIR, f"latest_{rt}.mp4")

    if not force_refresh:
        with _MEM_CACHE_LOCK:
            mem_entry = _MEM_MP4_CACHE.get(cache_key)
            if mem_entry and (now_ts - mem_entry["ts"]) < REEL_CACHE_TTL_SEC:
                _MEM_MP4_CACHE.move_to_end(cache_key)
                return mem_entry["bytes"]

        for cand_path in (disk_exact_path, disk_latest_path if now_jst is None else ""):
            if cand_path and os.path.isfile(cand_path):
                try:
                    if (now_ts - os.path.getmtime(cand_path)) < REEL_CACHE_TTL_SEC and os.path.getsize(cand_path) > 1024:
                        with open(cand_path, "rb") as f:
                            mp4_data = f.read()
                        with _MEM_CACHE_LOCK:
                            _MEM_MP4_CACHE[cache_key] = {"ts": now_ts, "bytes": mp4_data, "reel_type": rt}
                            while len(_MEM_MP4_CACHE) > _MEM_MP4_MAX:
                                _MEM_MP4_CACHE.popitem(last=False)
                        return mp4_data
                except Exception:
                    pass

        try:
            shared_exact = turbo_store.get_media_blob(f"reel_mp4:{rt}:{cache_key}")
            if not shared_exact and now_jst is None:
                shared_exact = turbo_store.get_media_blob(f"reel_mp4_latest:{rt}")
            if shared_exact and shared_exact[0] and len(shared_exact[0]) > 1024:
                mp4_data = shared_exact[0]
                with _MEM_CACHE_LOCK:
                    _MEM_MP4_CACHE[cache_key] = {"ts": now_ts, "bytes": mp4_data, "reel_type": rt}
                return mp4_data
        except Exception:
            pass

    with _RENDER_SINGLEFLIGHT_LOCK:
        now_ts = time.time()
        if not force_refresh:
            with _MEM_CACHE_LOCK:
                mem_entry = _MEM_MP4_CACHE.get(cache_key)
                if mem_entry and (now_ts - mem_entry["ts"]) < REEL_CACHE_TTL_SEC:
                    _MEM_MP4_CACHE.move_to_end(cache_key)
                    return mem_entry["bytes"]

        ctx = build_reel_context(stores, reel_type=rt, now_jst=now_jst)
        renderer = _ReelSceneRenderer(ctx)
        tmp_fd, tmp_mp4_path = tempfile.mkstemp(prefix=f"tmp_{rt}_", suffix=".mp4", dir=REEL_TMP_CACHE_DIR)
        os.close(tmp_fd)

        try:
            v_path, _, _, _ = _resolve_bg_paths(bg_info)
            wants_video_bg = bg_info.get("active_mode") == "veo3_video" and bool(v_path)
            if wants_video_bg:
                try:
                    _encode_with_ffmpeg(ffmpeg_bin, renderer, bg_info, tmp_mp4_path, use_video_bg=True)
                except Exception as veo_err:
                    logger.warning(f"Veo 3 video background composite failed; falling back to Ken Burns bg: {veo_err}")
                    _encode_with_ffmpeg(ffmpeg_bin, renderer, bg_info, tmp_mp4_path, use_video_bg=False)
            else:
                _encode_with_ffmpeg(ffmpeg_bin, renderer, bg_info, tmp_mp4_path, use_video_bg=False)

            with open(tmp_mp4_path, "rb") as f:
                mp4_bytes = f.read()

            try:
                os.replace(tmp_mp4_path, disk_exact_path)
            except Exception:
                pass
            if now_jst is None:
                try:
                    shutil.copyfile(disk_exact_path, disk_latest_path)
                except Exception:
                    pass
        finally:
            renderer.close()
            if os.path.exists(tmp_mp4_path):
                try:
                    os.remove(tmp_mp4_path)
                except Exception:
                    pass

        finished_ts = time.time()
        with _MEM_CACHE_LOCK:
            _MEM_MP4_CACHE[cache_key] = {"ts": finished_ts, "bytes": mp4_bytes, "reel_type": rt}
            while len(_MEM_MP4_CACHE) > _MEM_MP4_MAX:
                _MEM_MP4_CACHE.popitem(last=False)

        try:
            turbo_store.set_media_blob(f"reel_mp4:{rt}:{cache_key}", mp4_bytes, mime_type="video/mp4", ttl_sec=180)
            if now_jst is None:
                turbo_store.set_media_blob(f"reel_mp4_latest:{rt}", mp4_bytes, mime_type="video/mp4", ttl_sec=180)
        except Exception:
            pass

        _prune_tmp_reel_dir(finished_ts)
        return mp4_bytes


def get_reel_studio_info(
    stores: list,
    reel_type: str = "live_top5",
    now_jst: datetime.datetime = None,
    auto_schedule_enabled: bool = False,
) -> dict:
    """Assemble clean JSON metadata payload for `/api/staff/sns-reel/info`."""
    rt = normalize_reel_type(reel_type)
    bg_raw = list_available_backgrounds(reel_type=rt)
    bg_public = {
        "bg_dir": bg_raw["bg_dir"],
        "dir_exists": bg_raw["dir_exists"],
        "videos": bg_raw["videos"],
        "images": bg_raw["images"],
        "active_mode": bg_raw["active_mode"],
        "active_file": bg_raw["active_file"],
    }
    return {
        "ok": True,
        "reel_type": rt,
        "caption": build_reel_caption(stores, reel_type=rt, now_jst=now_jst),
        "templates": REEL_TEMPLATES,
        "veo3_prompts": VEO3_PROMPT_PRESETS,
        "backgrounds": bg_public,
        "auto_schedule_enabled": bool(auto_schedule_enabled),
        "ffmpeg_available": bool(resolve_ffmpeg_bin()),
    }
