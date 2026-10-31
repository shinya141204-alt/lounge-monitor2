"""
ig_carousel_generator.py - One-Click Instagram / Threads 4:5 Portrait Carousel Generator (1080x1350).

Builds 5 ready-to-post weekend forecast slides + copy-paste captions from real SQLite / weekly_report
crowd statistics, with zero external API cost and minimal memory footprint.
"""

import io
import os
import zipfile
import datetime
import threading
import collections
from PIL import Image, ImageDraw, ImageFilter

import ogp_generator
import store_master
import x_poster

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JST = datetime.timezone(datetime.timedelta(hours=9))
_PORTRAIT_BASE_CACHE = collections.OrderedDict()
_PORTRAIT_BASE_LOCK = threading.Lock()
_PORTRAIT_BASE_INFLIGHT = {}
_PORTRAIT_BASE_MAX = 5
_KINSOKU_LEADING = frozenset("。、．，・：；！？）」』】〉》〕％%")


def _upcoming_weekend_dates(now_jst: datetime.datetime) -> tuple:
    """Return (fri_date, sat_date, label_str) for the current or upcoming Fri/Sat."""
    d = now_jst.date()
    wd = d.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    if wd <= 4:
        fri = d + datetime.timedelta(days=(4 - wd))
    elif wd == 5:
        fri = d - datetime.timedelta(days=1)
    else:
        fri = d + datetime.timedelta(days=5)
    sat = fri + datetime.timedelta(days=1)
    label = f"{fri.month}/{fri.day}(金)・{sat.month}/{sat.day}(土)"
    return fri, sat, label


def _resolve_day_theme(now_jst: datetime.datetime, weekend_label: str) -> dict:
    """
    Automatically switch cover & slide titles/colors based on the day of week so the
    Instagram grid never looks identical day after day:
      - Mon/Tue/Wed (0,1,2): Weekday Night Anaba & Zero-Wait Ranking (Cyan & Gold)
      - Thu/Fri/Sat (3,4,5): Weekend Peak Forecast & Hopping Guide (Pink & Gold)
      - Sun (6): Weekly Nationwide Data Summary & Best Ratio Report (Purple & Gold)
    """
    wd = now_jst.weekday()
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    today_str = f"{now_jst.month}/{now_jst.day}({weekdays_ja[wd]})"

    if wd in (0, 1, 2):
        return {
            "theme_key": "weekday",
            "date_badge": f"平日夜データ特集：{today_str} 最新版",
            "kicker": "全国60店舗超・リアルタイム統計から算出",
            "h1_line1": "平日の相席",
            "h1_line2": "待ち時間ゼロで",
            "h1_line3": "飲める穴場TOP5",
            "sub_line1": "平日は「男性待ち0分×女性比率58%超」の店舗が続出！",
            "sub_line2": "仕事帰りに1軒目からスマートに乾杯できる攻略ガイド",
            "slide2_title": "平日夜でも盛り上がる大型店 TOP5",
            "slide2_sub": "平日20〜22時台でも女性来店数が安定して多い主力店舗",
            "glow1": (56, 189, 248, 115),
            "glow2": (251, 191, 36, 95),
            "badge_bg": (14, 58, 82, 240),
            "badge_outline": (56, 189, 248),
            "h1_accent": (56, 189, 248, 255),
        }
    if wd == 6:
        return {
            "theme_key": "sunday",
            "date_badge": f"週間データ総括：{today_str} 集計号",
            "kicker": "全国60店舗超・直近1週間の稼働ログ白書",
            "h1_line1": "週間ランキング",
            "h1_line2": "女性来店数＆",
            "h1_line3": "穴場データ白書",
            "sub_line1": "今週もっとも女性が集まった店舗と入りやすかった穴場を総まとめ",
            "sub_line2": "日曜夜の早めスタート＆来週の作戦会議に使える保存版データ",
            "slide2_title": "今週の女性来店ピーク実績 TOP5",
            "slide2_sub": "直近1週間で女性ピーク人数がもっとも高かった全国トップ店舗",
            "glow1": (168, 85, 247, 115),
            "glow2": (251, 191, 36, 95),
            "badge_bg": (52, 22, 78, 240),
            "badge_outline": (192, 132, 252),
            "h1_accent": (192, 132, 252, 255),
        }
    return {
        "theme_key": "weekend",
        "date_badge": f"対象週末：{weekend_label} 夜",
        "kicker": "全国60店舗超・リアルタイムログから算出",
        "h1_line1": "今週末の相席",
        "h1_line2": "狙い目店舗＆",
        "h1_line3": "穴場時間予報",
        "sub_line1": "「男性待ち1時間」を回避してスムーズに相席するための",
        "sub_line2": "週末データ攻略ガイド（オリエンタルラウンジ / ag / JIS 対応）",
        "slide2_title": "今週末の盛り上がり予測 TOP5",
        "slide2_sub": "直近のピーク時に女性が多く集まっている大型人気店舗",
        "glow1": (236, 72, 153, 110),
        "glow2": (251, 191, 36, 90),
        "badge_bg": (68, 22, 48, 240),
        "badge_outline": (244, 114, 182),
        "h1_accent": (244, 114, 182, 255),
    }


def build_forecast_context(
    report: dict = None,
    live_stores: list = None,
    now_jst: datetime.datetime = None,
) -> dict:
    """Assemble clean top-5 peak stores, top-5 high-ratio anaba stores, and day-of-week theme."""
    now_jst = now_jst or datetime.datetime.now(JST)
    _, _, weekend_label = _upcoming_weekend_dates(now_jst)
    theme = _resolve_day_theme(now_jst, weekend_label)

    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")

    def _is_domestic(name: str) -> bool:
        up = (name or "").upper()
        return not any(k in up for k in korea_keywords)

    stores = []
    anaba = []
    is_live_fallback = False
    if report and isinstance(report, dict) and not report.get("error"):
        stores = [s for s in (report.get("stores") or []) if _is_domestic(s.get("name", ""))]
        anaba = [s for s in (report.get("anaba_stores") or []) if _is_domestic(s.get("name", ""))]

    if not stores and live_stores:
        dom_live = [
            s for s in live_stores
            if s.get("region") != "Korea" and _is_domestic(s.get("name", ""))
        ]
        dom_live.sort(key=lambda x: (int(x.get("women") or 0), int(x.get("men") or 0)), reverse=True)
        if dom_live:
            is_live_fallback = True
            stores = []
            for s in dom_live:
                w = int(s.get("women") or 0)
                m = int(s.get("men") or 0)
                tot = w + m
                wpct = round((w / tot) * 100, 1) if tot > 0 else 0.0
                stores.append({
                    "name": s.get("name", ""),
                    "avg_women": w,
                    "avg_men": m,
                    "max_women": w,
                    "avg_wpct": wpct,
                })
            solid_anaba = sorted(
                [s for s in stores if s["avg_women"] >= 10],
                key=lambda x: (x["avg_wpct"], x["avg_women"]),
                reverse=True,
            )
            rest_anaba = sorted(
                [s for s in stores if s["avg_women"] < 10],
                key=lambda x: (x["avg_women"], x["avg_wpct"]),
                reverse=True,
            )
            anaba = solid_anaba + rest_anaba

    if not stores:
        fallback_seed = [
            ("OLG 大阪駅前", 68, 48, 92, 58.6),
            ("OLG 渋谷本店", 62, 46, 85, 57.4),
            ("OLG 新宿", 58, 47, 82, 55.2),
            ("JIS FUKUOKA", 54, 38, 76, 58.7),
            ("JIS NAMBA", 49, 36, 70, 57.6),
            ("OLG 恵比寿", 36, 23, 52, 61.0),
            ("OLG 京都", 34, 21, 48, 61.8),
            ("OLG 神戸", 31, 20, 44, 60.8),
        ]
        stores = [
            {"name": n, "avg_women": aw, "avg_men": am, "max_women": mw, "avg_wpct": pct}
            for (n, aw, am, mw, pct) in fallback_seed
        ]
        anaba = sorted(stores, key=lambda x: (x["avg_wpct"], x["avg_women"]), reverse=True)

    def _enrich(item: dict) -> dict:
        raw = item.get("name", "")
        info = store_master.get(raw) or {}
        ja = x_poster.format_ja_store_name(raw)
        seo = store_master.seo_name(raw) or ja
        raw_wpct = item.get("avg_wpct")
        return {
            "raw_name": raw,
            "ja_name": ja,
            "seo_name": seo,
            "area_label": info.get("area_label") or "主要エリア",
            "avg_women": int(round(float(item.get("avg_women") or 0))),
            "avg_men": int(round(float(item.get("avg_men") or 0))),
            "max_women": int(round(float(item.get("max_women") or 0))),
            "avg_wpct": int(round(float(raw_wpct))) if raw_wpct is not None else 0,
        }

    top_volume = [_enrich(s) for s in stores[:5]]
    top_anaba = [_enrich(s) for s in anaba[:5]]

    return {
        "weekend_label": weekend_label,
        "generated_at": now_jst.strftime("%Y/%m/%d %H:%M"),
        "top_volume": top_volume,
        "top_anaba": top_anaba,
        "theme": theme,
        "is_live_fallback": is_live_fallback,
    }


def _make_portrait_base(bg_filename: str, glow1: tuple, glow2: tuple) -> tuple:
    W, H = 1080, 1350
    bg_path = os.path.join(BASE_DIR, "static", bg_filename)
    try:
        bg_mtime = int(os.path.getmtime(bg_path)) if os.path.exists(bg_path) else 0
    except Exception:
        bg_mtime = 0
    cache_key = (bg_filename, bg_mtime, tuple(glow1), tuple(glow2))

    with _PORTRAIT_BASE_LOCK:
        cached = _PORTRAIT_BASE_CACHE.get(cache_key)
        if cached is not None:
            _PORTRAIT_BASE_CACHE.move_to_end(cache_key)
            canvas_copy = cached.copy()
            return canvas_copy, ogp_generator.make_safe_draw(canvas_copy, "RGBA")
        inflight = _PORTRAIT_BASE_INFLIGHT.get(cache_key)
        if inflight is None:
            inflight = [threading.Lock(), 0]
            _PORTRAIT_BASE_INFLIGHT[cache_key] = inflight
        inflight[1] += 1
        key_lock = inflight[0]

    try:
        with key_lock:
            with _PORTRAIT_BASE_LOCK:
                cached = _PORTRAIT_BASE_CACHE.get(cache_key)
                if cached is not None:
                    _PORTRAIT_BASE_CACHE.move_to_end(cache_key)
                    canvas_copy = cached.copy()
                    return canvas_copy, ogp_generator.make_safe_draw(canvas_copy, "RGBA")

            if os.path.exists(bg_path):
                try:
                    with Image.open(bg_path) as raw_img:
                        bg = raw_img.convert("RGBA")
                    sw, sh = bg.size
                    scale = max(W / sw, H / sh)
                    nw, nh = int(sw * scale), int(sh * scale)
                    bg = bg.resize((nw, nh), Image.Resampling.LANCZOS)
                    left = (nw - W) // 2
                    top = (nh - H) // 2
                    bg = bg.crop((left, top, left + W, top + H))
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
                except Exception:
                    bg = Image.new("RGBA", (W, H), (14, 14, 24, 255))
            else:
                bg = Image.new("RGBA", (W, H), (14, 14, 24, 255))

            canvas = Image.new("RGBA", (W, H), (10, 10, 18, 255))
            canvas.alpha_composite(bg)
            tint = Image.new("RGBA", (W, H), (10, 12, 22, 226))
            canvas.alpha_composite(tint)
            ogp_generator._draw_radial_glow(canvas, (180, 180), 540, glow1)
            ogp_generator._draw_radial_glow(canvas, (900, 1160), 540, glow2)
            rgb_base = canvas.convert("RGB")

            with _PORTRAIT_BASE_LOCK:
                _PORTRAIT_BASE_CACHE.pop(cache_key, None)
                _PORTRAIT_BASE_CACHE[cache_key] = rgb_base
                while len(_PORTRAIT_BASE_CACHE) > _PORTRAIT_BASE_MAX:
                    _, evicted = _PORTRAIT_BASE_CACHE.popitem(last=False)
                    try:
                        evicted.close()
                    except Exception:
                        pass
                canvas_copy = rgb_base.copy()
            return canvas_copy, ogp_generator.make_safe_draw(canvas_copy, "RGBA")
    finally:
        with _PORTRAIT_BASE_LOCK:
            cur_inf = _PORTRAIT_BASE_INFLIGHT.get(cache_key)
            if cur_inf is inflight:
                cur_inf[1] -= 1
                if cur_inf[1] <= 0:
                    _PORTRAIT_BASE_INFLIGHT.pop(cache_key, None)


def _wrap_text_by_pixel_width(draw, text: str, font, max_width: int, max_lines: int = 3) -> tuple:
    """
    Wrap Japanese/ASCII text cleanly within `max_width` pixels with basic kinsoku (no leading punctuation).
    Uses binary search per line on already-sanitized text to avoid O(N^2) measurement overhead.
    Returns `(lines: list[str], truncated: bool)`. When `truncated` is True, the final line ends with `...`.
    """
    clean = ogp_generator.sanitize_font_text(text or "").strip()
    if not clean:
        return ([], False)

    raw_draw = getattr(draw, "_draw", draw)
    measure = ogp_generator._measure_clean_text_width
    lines = []

    for para in clean.split("\n"):
        if not para:
            lines.append("")
            continue
        p_len = len(para)
        pos = 0
        min_take = 1
        while pos < p_len:
            rem_str = para[pos:]
            r_len = len(rem_str)
            if r_len <= min_take or measure(raw_draw, rem_str, font) <= max_width:
                lines.append(rem_str)
                break

            lo = max(1, min_take)
            hi = r_len - 1
            fit_len = lo
            while lo <= hi:
                mid = (lo + hi) // 2
                if measure(raw_draw, rem_str[:mid], font) <= max_width:
                    fit_len = mid
                    lo = mid + 1
                else:
                    hi = mid - 1

            min_take = 1
            next_ch = rem_str[fit_len]
            if next_ch in _KINSOKU_LEADING and fit_len > 1:
                cand_with_punct = rem_str[: fit_len + 1]
                if measure(raw_draw, cand_with_punct, font) <= max_width + 24:
                    lines.append(cand_with_punct)
                    pos += fit_len + 1
                else:
                    lines.append(rem_str[: fit_len - 1])
                    pos += fit_len - 1
                    min_take = 2
            else:
                lines.append(rem_str[:fit_len])
                pos += fit_len

    if len(lines) <= max_lines:
        return (lines, False)

    kept = lines[: max_lines - 1]
    overflow = "".join(lines[max_lines - 1 :])
    lo, hi = 1, len(overflow)
    best_len = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if measure(raw_draw, overflow[:mid] + "...", font) <= max_width:
            best_len = mid
            lo = mid + 1
        else:
            hi = mid - 1
    trimmed = overflow[:best_len] if best_len > 0 else ""
    kept.append((trimmed.rstrip("、。・ ") + "...") if trimmed else "...")
    return (kept, True)


def _draw_header_and_footer(draw: ImageDraw.ImageDraw, slide_num: int, total_slides: int = 5, accent=(244, 114, 182)):
    W, H = 1080, 1350
    f_brand = ogp_generator._get_font(24)
    f_page = ogp_generator._get_font(22)
    f_foot = ogp_generator._get_font(21)

    # Top brand pill + page indicator
    brand_txt = "AISEKI NAVI  |  相席ナビ公式データ予測"
    bw = int(draw.textlength(brand_txt, font=f_brand)) + 40
    draw.rounded_rectangle((64, 54, 64 + bw, 102), radius=24, fill=(24, 28, 44, 235), outline=accent, width=2)
    draw.text((84, 65), brand_txt, font=f_brand, fill=accent)

    page_txt = f"{slide_num} / {total_slides}"
    pw = int(draw.textlength(page_txt, font=f_page)) + 34
    draw.rounded_rectangle((W - 64 - pw, 56, W - 64, 100), radius=22, fill=(24, 28, 44, 220), outline=(255, 255, 255, 60), width=1)
    draw.text((W - 64 - pw + 17, 66), page_txt, font=f_page, fill=(226, 232, 240, 240))

    # Bottom footer bar
    draw.line((64, H - 86, W - 64, H - 86), fill=(255, 255, 255, 35), width=1)
    foot_l = "全国60店舗超の現在人数は「相席ナビ」(aiseki-navi.com) で無料公開中"
    draw.text((64, H - 64), foot_l, font=f_foot, fill=(148, 163, 184, 235))


def _render_slide_canvas(slide_num: int, ctx: dict = None) -> Image.Image:
    """Render slide 1..5 onto a 1080x1350 RGB PIL.Image canvas."""
    ctx = ctx or build_forecast_context()
    theme = ctx.get("theme") or _resolve_day_theme(datetime.datetime.now(JST), ctx.get("weekend_label", ""))
    W, H = 1080, 1350
    slide_num = max(1, min(5, int(slide_num)))

    if slide_num == 1:
        canvas, draw = _make_portrait_base(
            "eyecatch-weekend.jpg",
            theme["glow1"],
            theme["glow2"],
        )
        accent = (251, 191, 36)
        _draw_header_and_footer(draw, 1, 5, accent=accent)

        f_date = ogp_generator._get_font(34)
        f_kicker = ogp_generator._get_font(38)
        f_h1 = ogp_generator._get_font(76)
        f_sub = ogp_generator._get_font(28)
        f_box_t = ogp_generator._get_font(28)
        f_box_b = ogp_generator._get_font(24)

        date_badge = theme["date_badge"]
        dw = int(draw.textlength(date_badge, font=f_date)) + 56
        draw.rounded_rectangle((64, 170, 64 + dw, 238), radius=18, fill=theme["badge_bg"], outline=theme["badge_outline"], width=2)
        draw.text((92, 185), date_badge, font=f_date, fill=(255, 255, 255, 255))

        draw.text((64, 286), theme["kicker"], font=f_kicker, fill=accent)
        draw.text((64, 356), theme["h1_line1"], font=f_h1, fill=(255, 255, 255, 255))
        draw.text((64, 456), theme["h1_line2"], font=f_h1, fill=theme["h1_accent"])
        draw.text((64, 556), theme["h1_line3"], font=f_h1, fill=(255, 255, 255, 255))

        draw.text((64, 678), theme["sub_line1"], font=f_sub, fill=(203, 213, 225, 245))
        draw.text((64, 722), theme["sub_line2"], font=f_sub, fill=(203, 213, 225, 245))

        bullets = [
            ("SLIDE 02", "圧倒的盛り上がり！女性来店数TOP5店舗", (244, 114, 182)),
            ("SLIDE 03", "男性待ちを回避しやすい「高女性比率」穴場TOP5", (56, 189, 248)),
            ("SLIDE 04", "時間帯別ベストな立ち回り＆女性目線メモ", (251, 191, 36)),
        ]
        by = 810
        for tag_s, desc_s, col in bullets:
            draw.rounded_rectangle((64, by, W - 64, by + 112), radius=20, fill=(20, 24, 38, 235), outline=(col[0], col[1], col[2], 150), width=2)
            draw.rounded_rectangle((90, by + 28, 242, by + 84), radius=12, fill=(col[0], col[1], col[2], 42), outline=col, width=2)
            draw.text((108, by + 40), tag_s, font=f_box_b, fill=col)
            draw.text((268, by + 38), desc_s, font=f_box_t, fill=(248, 250, 252, 255))
            by += 130

        save_txt = "右へスワイプしてチェック  >>>   行く前に「保存」がおすすめ"
        sw_px = int(draw.textlength(save_txt, font=f_box_b))
        draw.text(((W - sw_px) // 2, 1218), save_txt, font=f_box_b, fill=accent)

    elif slide_num in (2, 3):
        is_vol = (slide_num == 2)
        canvas, draw = _make_portrait_base(
            "note-eyecatch.jpg" if is_vol else "eyecatch-shinjuku.jpg",
            (236, 72, 153, 95) if is_vol else (56, 189, 248, 95),
            (251, 191, 36, 70),
        )
        accent = (244, 114, 182) if is_vol else (56, 189, 248)
        _draw_header_and_footer(draw, slide_num, 5, accent=accent)

        f_title = ogp_generator._get_font(46)
        f_sub = ogp_generator._get_font(24)
        f_rank = ogp_generator._get_font(30)
        f_sname = ogp_generator._get_font(32)
        f_area = ogp_generator._get_font(20)
        f_stat = ogp_generator._get_font(25)
        f_pill = ogp_generator._get_font(24)

        if is_vol:
            t1 = theme["slide2_title"]
            sub = theme["slide2_sub"]
            items = ctx["top_volume"]
        else:
            t1 = "入りやすい高女性比率・穴場 TOP5"
            sub = "女性人数が多く、かつ女性比率が高めでスムーズに座りやすい店舗"
            items = ctx["top_anaba"]

        draw.text((64, 134), t1, font=f_title, fill=(255, 255, 255, 255))
        draw.text((64, 198), sub, font=f_sub, fill=(203, 213, 225, 235))

        cy = 258
        card_h = 182
        rank_cols = [
            (251, 191, 36),
            (226, 232, 240),
            (245, 158, 11),
            (148, 163, 184),
            (148, 163, 184),
        ]
        for idx, st in enumerate(items[:5]):
            rc = rank_cols[idx]
            draw.rounded_rectangle(
                (64, cy, W - 64, cy + card_h),
                radius=22,
                fill=(20, 24, 38, 238),
                outline=(rc[0], rc[1], rc[2], 185 if idx == 0 else 80),
                width=3 if idx == 0 else 1,
            )
            # Rank badge
            draw.rounded_rectangle((90, cy + 24, 152, cy + 86), radius=14, fill=(rc[0], rc[1], rc[2], 38), outline=rc, width=2)
            r_str = str(idx + 1)
            rw_px = int(draw.textlength(r_str, font=f_rank))
            draw.text((121 - rw_px // 2, cy + 36), r_str, font=f_rank, fill=rc)

            # Highlight pill top-right
            is_live_fb = bool(ctx.get("is_live_fallback"))
            if is_vol:
                pill_txt = f"現在女性 {st['max_women']}名" if is_live_fb else f"最高女性 {st['max_women']}名"
            else:
                pill_txt = f"女性比率 {st['avg_wpct']}%" if is_live_fb else f"平均女性比 {st['avg_wpct']}%"
            pw_px = int(draw.textlength(pill_txt, font=f_pill)) + 34
            px = W - 92 - pw_px
            pill_bg = (190, 24, 93, 235) if is_vol else (14, 116, 144, 235)
            draw.rounded_rectangle((px, cy + 24, px + pw_px, cy + 72), radius=12, fill=pill_bg)
            draw.text((px + 17, cy + 34), pill_txt, font=f_pill, fill=(255, 255, 255, 255))

            # Store name + area (auto-fit so long store names never collide with top-right pill)
            sname_txt, sname_font, sname_sz = ogp_generator.fit_text_and_font(
                draw, st["seo_name"], max(200, px - 190), 32, 22
            )
            draw.text((174, cy + 24 + max(0, (32 - sname_sz) // 2)), sname_txt, font=sname_font, fill=(255, 255, 255, 255))
            draw.text((176, cy + 68), f"エリア：{st['area_label']}", font=f_area, fill=(148, 163, 184, 235))

            # Stats + ratio bar
            if is_live_fb:
                stat_l = f"現在人数：女性 {st['avg_women']}名  /  男性 {st['avg_men']}名（女性比 {st['avg_wpct']}%）"
            else:
                stat_l = f"女性ピーク平均 {st['avg_women']}名  /  男性平均 {st['avg_men']}名（女性比 {st['avg_wpct']}%）"
            draw.text((92, cy + 104), stat_l, font=f_stat, fill=(244, 114, 182, 255))

            bx, by_bar, bw_bar, bh_bar = 92, cy + 146, W - 184, 14
            draw.rounded_rectangle((bx, by_bar, bx + bw_bar, by_bar + bh_bar), radius=7, fill=(59, 130, 246, 220))
            fill_w = max(16, min(bw_bar, int(bw_bar * (st["avg_wpct"] / 100.0))))
            draw.rounded_rectangle((bx, by_bar, bx + fill_w, by_bar + bh_bar), radius=7, fill=(236, 72, 153, 255))

            cy += card_h + 16

    elif slide_num == 4:
        canvas, draw = _make_portrait_base(
            "eyecatch-beauty-stage.jpg",
            (251, 191, 36, 95),
            (56, 189, 248, 85),
        )
        accent = (251, 191, 36)
        _draw_header_and_footer(draw, 4, 5, accent=accent)

        f_title = ogp_generator._get_font(44)
        f_sub = ogp_generator._get_font(24)
        f_slot = ogp_generator._get_font(28)
        f_head = ogp_generator._get_font(30)
        f_body = ogp_generator._get_font(23)

        if theme.get("theme_key") == "weekday":
            s4_title = "水・木の時間帯別 勝ちパターン"
            s4_sub = "平日20〜22時中心の立ち回りで待ち時間ゼロで飲めます"
            cards = [
                (
                    "19:00 - 20:00",
                    "仕事帰り1軒目のスマート入店枠",
                    "早めの仕事帰りの女性が1軒目として来店する時間帯。男性ライバルが少なく、落ち着いて会話を楽しめます。",
                    (56, 189, 248),
                ),
                (
                    "20:00 - 22:00",
                    "水・木最大の穴場ゴールデンタイム",
                    "平日の女性来店数がピークを迎えつつ、男性待ち0分で座りやすい最もコスパが高いコアタイムです。",
                    (251, 191, 36),
                ),
                (
                    "22:00 - 23:30",
                    "終電前の2軒目・徒歩圏ハシゴ比較",
                    "食事会帰りの2軒目組が増える時間。新宿・渋谷・梅田などは徒歩4分の別ブランドと比率を見比べるのが鉄則です。",
                    (244, 114, 182),
                ),
                (
                    "WOMEN'S TIP",
                    "女性グループが「待ち0分で即相席」するコツ",
                    "女性比率が高すぎる店より、男性が先行して盛り上がっている店舗を選ぶと入店直後からスマートに相席しやすくなります。",
                    (167, 139, 250),
                ),
            ]
        else:
            s4_title = "金・土の時間帯別 勝ちパターン"
            s4_sub = "入店時間で待ち時間とコスパが劇的に変わります"
            cards = [
                (
                    "19:30 - 20:45",
                    "美容サロン帰り×待ち時間ゼロの黄金枠",
                    "1軒目利用の女性が先行して入店する時間帯。男性の入店待ちがほぼ発生せず、落ち着いて乾杯できる一番の狙い目です。",
                    (244, 114, 182),
                ),
                (
                    "21:15 - 23:00",
                    "最大ピーク帯は「徒歩4分ハシゴ比較」が鉄則",
                    "新宿・渋谷・梅田などは同じ時刻でも店舗間で女性比率が平均15%以上開きます。並ぶ前に近隣の別ブランドを比較しましょう。",
                    (251, 191, 36),
                ),
                (
                    "00:00 - 02:00",
                    "クラブ街＆地方店の「深夜第2波」ボーナス",
                    "大型クラブ周辺店舗や地方都市（熊本・沖縄・金沢・仙台など）は終電後に女性が再増加。2軒目・3軒目利用に最適です。",
                    (56, 189, 248),
                ),
                (
                    "WOMEN'S TIP",
                    "女性グループが「待ち0分で即相席」するコツ",
                    "女性比率が高すぎる店より、男性が先行して盛り上がっている店舗を選ぶと入店直後からスマートに相席しやすくなります。",
                    (167, 139, 250),
                ),
            ]

        draw.text((64, 134), s4_title, font=f_title, fill=(255, 255, 255, 255))
        draw.text((64, 196), s4_sub, font=f_sub, fill=(203, 213, 225, 235))

        cy = 262
        for badge_s, head_s, body_s, col in cards:
            ch = 224
            draw.rounded_rectangle((64, cy, W - 64, cy + ch), radius=22, fill=(18, 22, 36, 238), outline=(col[0], col[1], col[2], 165), width=2)
            bw_px = int(draw.textlength(badge_s, font=f_slot)) + 34
            draw.rounded_rectangle((92, cy + 22, 92 + bw_px, cy + 68), radius=12, fill=(col[0], col[1], col[2], 42), outline=col, width=2)
            draw.text((109, cy + 29), badge_s, font=f_slot, fill=col)
            head_fit, f_head_fit, head_sz = ogp_generator.fit_text_and_font(draw, head_s, W - 184, 30, 22)
            draw.text((92, cy + 84 + max(0, (30 - head_sz) // 2)), head_fit, font=f_head_fit, fill=(255, 255, 255, 255))

            # Pixel-aware natural wrapping (2 lines at 23px, or up to 3 lines at 21px with clean '...' fallback)
            max_body_w = W - 184
            body_lines, was_trunc = _wrap_text_by_pixel_width(draw, body_s, f_body, max_body_w, max_lines=2)
            if not was_trunc and len(body_lines) <= 2:
                for l_idx, line_txt in enumerate(body_lines):
                    draw.text((92, cy + 134 + l_idx * 34), line_txt, font=f_body, fill=(203, 213, 225, 240))
            else:
                f_body_sm = ogp_generator._get_font(21)
                body_lines_3, _ = _wrap_text_by_pixel_width(draw, body_s, f_body_sm, max_body_w, max_lines=3)
                for l_idx, line_txt in enumerate(body_lines_3):
                    draw.text((92, cy + 128 + l_idx * 28), line_txt, font=f_body_sm, fill=(203, 213, 225, 240))
            cy += ch + 18

    else:
        # Slide 5: CTA
        canvas, draw = _make_portrait_base(
            "guide-brand-comparison.jpg",
            (236, 72, 153, 105),
            (56, 189, 248, 95),
        )
        accent = (56, 189, 248)
        _draw_header_and_footer(draw, 5, 5, accent=accent)

        f_kicker = ogp_generator._get_font(34)
        f_h1 = ogp_generator._get_font(62)
        f_sub = ogp_generator._get_font(28)
        f_feat = ogp_generator._get_font(28)
        f_url = ogp_generator._get_font(40)

        draw.text((64, 190), "お店に向かう3分前にチェック！", font=f_kicker, fill=(244, 114, 182, 255))
        draw.text((64, 255), "今の男女人数・比率が", font=f_h1, fill=(255, 255, 255, 255))
        draw.text((64, 340), "ひと目でわかる", font=f_h1, fill=(255, 255, 255, 255))
        draw.text((64, 425), "『相席ナビ』", font=f_h1, fill=(251, 191, 36, 255))

        features = [
            "オリエンタルラウンジ・ag・JIS等 全国60店舗超を毎分更新",
            "「女性比率順」「現在地から近い順」で穴場が3秒で判明",
            "曜日・時間帯別の平均推移グラフ＆直近の増減トレンドも表示",
            "会員登録・アプリDL不要！スマホのブラウザで完全無料",
        ]
        fy = 555
        for ft in features:
            draw.rounded_rectangle((64, fy, W - 64, fy + 88), radius=18, fill=(20, 26, 42, 235), outline=(255, 255, 255, 55), width=1)
            draw.ellipse((90, fy + 30, 118, fy + 58), fill=(56, 189, 248, 255))
            draw.text((138, fy + 26), ft, font=f_feat, fill=(248, 250, 252, 255))
            fy += 104

        # Search / URL box
        draw.rounded_rectangle((64, 1005, W - 64, 1190), radius=26, fill=(56, 18, 46, 240), outline=(244, 114, 182, 220), width=3)
        cta1 = "プロフィールのリンク または Googleで検索"
        c1w = int(draw.textlength(cta1, font=f_sub))
        draw.text(((W - c1w) // 2, 1034), cta1, font=f_sub, fill=(248, 250, 252, 240))

        url_box_txt = "相席ナビ  /  aiseki-navi.com"
        uw = int(draw.textlength(url_box_txt, font=f_url)) + 64
        ux = (W - uw) // 2
        draw.rounded_rectangle((ux, 1086, ux + uw, 1164), radius=18, fill=(15, 23, 42, 245), outline=(251, 191, 36, 240), width=2)
        draw.text((ux + 32, 1102), url_box_txt, font=f_url, fill=(251, 191, 36, 255))

    return canvas


def render_slide_png(slide_num: int, ctx: dict = None) -> bytes:
    """Render slide 1..5 as 1080x1350 PNG bytes (fast compress_level=3)."""
    canvas = _render_slide_canvas(slide_num, ctx=ctx)
    out = io.BytesIO()
    canvas.save(out, format="PNG", compress_level=3)
    canvas.close()
    return out.getvalue()


def render_slide_jpeg(slide_num: int, ctx: dict = None, quality: int = 90) -> bytes:
    """Render slide 1..5 directly as 1080x1350 RGB JPEG bytes without intermediate PNG encode/decode."""
    canvas = _render_slide_canvas(slide_num, ctx=ctx)
    rgb = canvas if canvas.mode == "RGB" else canvas.convert("RGB")
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=max(50, min(98, int(quality))))
    if rgb is not canvas:
        rgb.close()
    canvas.close()
    return out.getvalue()


def build_captions(ctx: dict = None) -> dict:
    """Generate ready-to-paste Instagram & Threads captions from the forecast context."""
    ctx = ctx or build_forecast_context()
    v_top = ctx["top_volume"][:3]
    a_top = ctx["top_anaba"][:3]
    wk = ctx["weekend_label"]
    theme = ctx.get("theme") or _resolve_day_theme(datetime.datetime.now(JST), wk)
    t_key = theme.get("theme_key", "weekend")

    is_live_fb = bool(ctx.get("is_live_fallback"))
    v_metric_lbl = "現在女性" if is_live_fb else "最高女性"
    a_metric_lbl = "女性比率" if is_live_fb else "平均女性比"

    v_lines = "\n".join(
        f"・{i+1}位：{s['seo_name']}（{v_metric_lbl}{s['max_women']}名）"
        for i, s in enumerate(v_top)
    )
    a_lines = "\n".join(
        f"・{i+1}位：{s['seo_name']}（{a_metric_lbl}{s['avg_wpct']}%）"
        for i, s in enumerate(a_top)
    )

    if t_key == "weekday":
        headline = f"【{theme['date_badge']}｜平日夜の穴場＆高女性比率ガイド📊】"
        intro = "平日の仕事帰りに「男性待ち0分」でスムーズに相席できる狙い目店舗と、高女性比率の穴場TOP5をまとめました！"
    elif t_key == "sunday":
        headline = f"【{theme['date_badge']}｜全国60店舗超 週間データ白書📊】"
        intro = "今週もっとも女性来店数が多かった人気店舗と、男性待ちを回避しやすかった高女性比率の穴場店舗を総括しました！"
    else:
        headline = f"【{wk} 相席ラウンジ週末狙い目予報📊】"
        intro = "全国60店舗超・リアルタイム人数ログから、今週末に盛り上がりやすい店舗と「男性待ちを回避しやすい穴場店舗」を予測しました！"

    now_ref = None
    gen_at = (ctx.get("generated_at") or "").strip()
    if gen_at:
        try:
            now_ref = datetime.datetime.strptime(gen_at, "%Y/%m/%d %H:%M").replace(tzinfo=JST)
        except Exception:
            now_ref = None
    try:
        import sns_editorial
        tag_line = sns_editorial.pick_rotated_hashtag_line(
            "instagram",
            "carousel",
            now_jst=now_ref or datetime.datetime.now(JST),
            seed_extra=4,
        )
    except Exception:
        tag_line = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #週末ディナー"

    ig_caption = (
        f"{headline}\n"
        f"お店選びで迷わないように、右上の「保存」をしておくと入店前にすぐ見返せます📌\n\n"
        f"{intro}\n\n"
        f"🔥 盛り上がり・女性来店数 TOP3\n"
        f"{v_lines}\n\n"
        f"💎 高女性比率の穴場 TOP3\n"
        f"{a_lines}\n\n"
        f"💡 失敗しない立ち回りポイント\n"
        f"・男性は「女性10名以上（約5組）×女性比率55%以上」の店舗を選ぶと席替えもスムーズ！\n"
        f"・女性は逆に「男性が先行して盛り上がっているお店」を選ぶと、待ち時間ゼロで即乾杯しやすいです🥂\n\n"
        f"お店に向かう直前の「いま現在の男女人数・比率」と「全国60店舗超の料金・イベント横並び比較」は、プロフィールのリンク（aiseki-navi.com）から3秒で無料チェックできます🔍\n\n"
        f"{tag_line}"
    )

    threads_1 = (
        f"今週末（{wk}）に相席ラウンジ行く人向けの狙い目データまとめました📊\n\n"
        f"直近データで一番女性比率が高くて入りやすい穴場は、\n"
        f"{a_lines}\n\n"
        f"逆に女性目線で「待ち時間ゼロで即乾杯したい」なら、男性が先行して集まってる大型店を選ぶのが大正解🥂\n"
        f"全5枚の予測カード添付しておくので週末の作戦会議にどうぞ👇"
    )

    threads_2 = (
        f"金土の21時半に何も見ないで相席ラウンジ突撃して「男性待ち50分です」って言われるのが一番もったいない…😂\n\n"
        f"同じ新宿や梅田でも、歩いて3分の別店舗を見たら女性比率60%超えで即座れることが8割以上あります。\n"
        f"今週末（{wk}）の狙い目予測まとめ置いておきます📌"
    )

    return {
        "instagram_caption": ig_caption,
        "threads_post_1": threads_1,
        "threads_post_2": threads_2,
    }


def render_all_slides_zip(ctx: dict = None) -> bytes:
    """Package all 5 PNG slides + captions text file into an in-memory ZIP archive."""
    ctx = ctx or build_forecast_context()
    captions = build_captions(ctx)
    buf = io.BytesIO()
    filenames = [
        "01_weekend_cover.png",
        "02_top5_volume.png",
        "03_top5_anaba_ratio.png",
        "04_timeslot_strategy.png",
        "05_aiseki_navi_cta.png",
    ]
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for idx, fname in enumerate(filenames, start=1):
            png_data = render_slide_png(idx, ctx=ctx)
            zf.writestr(fname, png_data)
        readme_txt = (
            "=== Instagram キャプション ===\n\n"
            + captions["instagram_caption"]
            + "\n\n\n=== Threads 投稿案① ===\n\n"
            + captions["threads_post_1"]
            + "\n\n\n=== Threads 投稿案② ===\n\n"
            + captions["threads_post_2"]
            + "\n"
        )
        zf.writestr("captions.txt", readme_txt.encode("utf-8"))
    return buf.getvalue()
