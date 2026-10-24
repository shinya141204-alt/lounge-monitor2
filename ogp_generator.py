import io
import os
import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import x_poster

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "static", "fonts", "ogp-jp-bold.otf")
MAC_FONT_BOLD = "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc"

_FONT_CACHE = {}


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for path in (FONT_PATH, MAC_FONT_BOLD):
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[size] = f
                return f
            except Exception:
                pass
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


SLOT_THEMES = {
    "20:00": {
        "bg_file": "note-eyecatch-weekday.jpg",
        "tint_rgba": (18, 13, 6, 216),
        "glow_top": (245, 158, 11, 80),      # Warm Amber Gold
        "glow_bottom": (236, 72, 153, 55),   # Soft Pink
        "accent": (251, 191, 36),            # #fbbf24 Amber Gold
        "badge_bg": (62, 40, 10, 235),
        "badge_border": (251, 191, 36, 255),
        "slot_tag": "20:00 EARLY NIGHT LIVE",
        "title_line1": "早い時間から女性が",
        "title_line2": "集まっている上位3店舗",
        "sub_caption": "仕事帰り・1軒目スタートの狙い目ラウンジ速報",
    },
    "22:00": {
        "bg_file": "note-eyecatch.jpg",
        "tint_rgba": (18, 8, 18, 216),
        "glow_top": (236, 72, 153, 88),      # Vivid Rose Pink
        "glow_bottom": (229, 200, 120, 60),  # Champagne Gold
        "accent": (244, 114, 182),           # #f472b6 Pink
        "badge_bg": (68, 16, 44, 235),
        "badge_border": (244, 114, 182, 255),
        "slot_tag": "22:00 PEAK TIME ANABA",
        "title_line1": "いま女性比率が高めで",
        "title_line2": "入りやすい穴場3店舗",
        "sub_caption": "ピークタイムに待ち時間を回避できる狙い目速報",
    },
    "00:00": {
        "bg_file": "eyecatch-shinjuku.jpg",
        "tint_rgba": (10, 8, 22, 220),
        "glow_top": (168, 85, 247, 88),      # Cyber Purple
        "glow_bottom": (236, 72, 153, 65),   # Neon Pink
        "accent": (192, 132, 252),           # #c084fc Purple
        "badge_bg": (44, 16, 76, 235),
        "badge_border": (192, 132, 252, 255),
        "slot_tag": "00:00 MIDNIGHT FEVER",
        "title_line1": "終電後も女性が多く",
        "title_line2": "残っている上位3店舗",
        "sub_caption": "深夜0時からの「2回転目」リアルタイム人数速報",
    },
    "02:00": {
        "bg_file": "eyecatch-second-venue.jpg",
        "tint_rgba": (6, 14, 24, 222),
        "glow_top": (56, 189, 248, 84),      # Night Cyan
        "glow_bottom": (168, 85, 247, 65),   # Deep Violet
        "accent": (56, 189, 248),            # #38bdf8 Sky Cyan
        "badge_bg": (12, 42, 68, 235),
        "badge_border": (56, 189, 248, 255),
        "slot_tag": "02:00 ALL-NIGHT LIVE",
        "title_line1": "深夜2時でも賑わって",
        "title_line2": "いるオールナイト3店舗",
        "sub_caption": "朝まで営業ラウンジのリアルタイム男女人数速報",
    },
}


def resolve_slot_from_param(s_param: str = "", now_jst: datetime.datetime = None) -> str:
    """Determine which time slot theme ('20:00', '22:00', '00:00', '02:00') to use."""
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    s_clean = (s_param or "").strip()
    if "2200" in s_clean or "22:00" in s_clean or "2230" in s_clean:
        return "22:00"
    if "0000" in s_clean or "00:00" in s_clean:
        return "00:00"
    if "0200" in s_clean or "02:00" in s_clean:
        return "02:00"
    if "2000" in s_clean or "20:00" in s_clean:
        return "20:00"

    h = now_jst.hour
    if 1 <= h < 6:
        return "02:00"
    if h == 0 or h == 23:
        return "00:00"
    if 21 <= h <= 22:
        return "22:00"
    return "20:00"


def get_ogp_meta_for_slot(s_param: str = "", now_jst: datetime.datetime = None) -> dict:
    """Return dynamic og:title and og:description matching the slot."""
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    slot_label = resolve_slot_from_param(s_param, now_jst=now_jst)
    theme = SLOT_THEMES.get(slot_label, SLOT_THEMES["20:00"])
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    date_prefix = f"【{now_jst.month}/{now_jst.day}({wd_str}) {slot_label}速報】"
    headline = f"{theme['title_line1']}{theme['title_line2']}"
    return {
        "slot_label": slot_label,
        "title": f"{date_prefix}{headline}｜相席ナビ",
        "description": f"{theme['sub_caption']}。全国53店舗の男女人数・男女比をリアルタイム更新中！",
    }


def _select_top3_for_slot(stores: list, slot_label: str) -> list:
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    dom_stores = [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]
    if not dom_stores:
        return []

    if slot_label in ("22:00", "22:30"):
        candidates = [s for s in dom_stores if int(s.get("women") or 0) >= 10]
        candidates.sort(
            key=lambda x: (
                int(x.get("women") or 0) / max(1, int(x.get("women") or 0) + int(x.get("men") or 0)),
                int(x.get("women") or 0),
            ),
            reverse=True,
        )
        return candidates[:3] if len(candidates) >= 3 else dom_stores[:3]

    dom_stores_sorted = sorted(
        dom_stores,
        key=lambda x: (int(x.get("women") or 0), int(x.get("men") or 0)),
        reverse=True,
    )
    return dom_stores_sorted[:3]


def _draw_radial_glow(base_img: Image.Image, center: tuple, radius: int, rgba_color: tuple):
    glow = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow, "RGBA")
    cx, cy = center
    r, g, b, max_a = rgba_color
    steps = 24
    for i in range(steps, 0, -1):
        rad = int(radius * (i / steps))
        alpha = int(max_a * ((1 - (i / steps)) ** 1.6))
        g_draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=(r, g, b, alpha))
    base_img.alpha_composite(glow)


def generate_live_ogp_png(stores: list, s_param: str = "", now_jst: datetime.datetime = None) -> bytes:
    """
    Render a 1200x630 dynamic OGP card image (PNG bytes) with:
      - Time-slot specific background photo & neon color palette
      - Date/time badge & nationwide total headcount
      - Live TOP 3 stores with Japanese names, female/male counts, and ratio progress bars
    """
    W, H = 1200, 630
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    slot_label = resolve_slot_from_param(s_param, now_jst=now_jst)
    theme = SLOT_THEMES.get(slot_label, SLOT_THEMES["20:00"])

    # 1. Load & prepare background image
    bg_path = os.path.join(BASE_DIR, "static", theme["bg_file"])
    if os.path.exists(bg_path):
        try:
            bg = Image.open(bg_path).convert("RGBA")
            bg_ratio = bg.width / bg.height
            target_ratio = W / H
            if bg_ratio > target_ratio:
                new_h = H
                new_w = int(H * bg_ratio)
            else:
                new_w = W
                new_h = int(W / bg_ratio)
            bg = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = (new_w - W) // 2
            top = (new_h - H) // 2
            bg = bg.crop((left, top, left + W, top + H))
            bg = bg.filter(ImageFilter.GaussianBlur(radius=3))
        except Exception:
            bg = Image.new("RGBA", (W, H), (10, 12, 20, 255))
    else:
        bg = Image.new("RGBA", (W, H), (10, 12, 20, 255))

    # 2. Apply dark luxury overlay + ambient radial glows
    overlay = Image.new("RGBA", (W, H), theme["tint_rgba"])
    canvas = Image.alpha_composite(bg, overlay)
    _draw_radial_glow(canvas, (1050, 90), 460, theme["glow_top"])
    _draw_radial_glow(canvas, (320, 560), 420, theme["glow_bottom"])

    draw = ImageDraw.Draw(canvas, "RGBA")

    # Fonts
    f_badge = _get_font(17)
    f_date = _get_font(23)
    f_title = _get_font(44)
    f_sub = _get_font(18)
    f_total_lbl = _get_font(16)
    f_total_val = _get_font(26)

    f_panel_hdr = _get_font(18)
    f_rank_num = _get_font(24)
    f_store = _get_font(26)
    f_diff = _get_font(15)
    f_pct_pill = _get_font(16)
    f_count = _get_font(21)

    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    date_str = f"【{now_jst.month}/{now_jst.day}({wd_str}) {slot_label} 現在】リアルタイム集計"

    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    dom_stores = [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]
    total_w = sum(int(s.get("women") or 0) for s in dom_stores)
    total_m = sum(int(s.get("men") or 0) for s in dom_stores)
    top3 = _select_top3_for_slot(dom_stores, slot_label)

    # =========================================================================
    # LEFT COLUMN (x=52 to 548)
    # =========================================================================
    lx = 52

    # 1) Slot Pill Badge + Brand tag
    badge_text = f"● {theme['slot_tag']}"
    bw = int(draw.textlength(badge_text, font=f_badge)) + 34
    bh = 36
    by = 48
    draw.rounded_rectangle(
        (lx, by, lx + bw, by + bh),
        radius=18,
        fill=theme["badge_bg"],
        outline=theme["badge_border"],
        width=2,
    )
    draw.text((lx + 17, by + 8), badge_text, font=f_badge, fill=theme["accent"])
    draw.text((lx + bw + 14, by + 8), "AISEKI NAVI", font=f_badge, fill=(200, 205, 218, 200))

    # 2) Date & Time Header
    dy = by + bh + 22
    draw.text((lx, dy), date_str, font=f_date, fill=(229, 200, 120, 255))

    # 3) Main 2-Line Headline
    ty1 = dy + 42
    draw.text((lx, ty1), theme["title_line1"], font=f_title, fill=(255, 255, 255, 255))
    ty2 = ty1 + 60
    draw.text((lx, ty2), theme["title_line2"], font=f_title, fill=theme["accent"])

    # 4) Sub-caption bar (Dark glass box with left accent bar)
    sy = ty2 + 68
    draw.rounded_rectangle(
        (lx, sy, lx + 492, sy + 46),
        radius=10,
        fill=(22, 26, 38, 220),
        outline=(255, 255, 255, 35),
        width=1,
    )
    draw.rounded_rectangle(
        (lx, sy, lx + 6, sy + 46),
        radius=3,
        fill=theme["accent"],
    )
    draw.text((lx + 20, sy + 13), theme["sub_caption"], font=f_sub, fill=(238, 240, 246, 255))

    # 5) Nationwide Total Box
    tbox_y = sy + 64
    tbox_w = 492
    tbox_h = 104
    draw.rounded_rectangle(
        (lx, tbox_y, lx + tbox_w, tbox_y + tbox_h),
        radius=16,
        fill=(14, 18, 28, 232),
        outline=(theme["accent"][0], theme["accent"][1], theme["accent"][2], 95),
        width=1,
    )
    draw.text(
        (lx + 22, tbox_y + 16),
        f"◆ 全国{len(dom_stores) or 53}店舗 リアルタイム来店合計",
        font=f_total_lbl,
        fill=(205, 212, 226, 255),
    )
    tot_w_str = f"女性 {total_w:,}名"
    tot_m_str = f"男性 {total_m:,}名"
    draw.text((lx + 22, tbox_y + 48), tot_w_str, font=f_total_val, fill=(255, 110, 168, 255))
    w_len = int(draw.textlength(tot_w_str, font=f_total_val))
    draw.text((lx + 22 + w_len + 16, tbox_y + 48), "／", font=f_total_val, fill=(130, 140, 160, 255))
    draw.text((lx + 22 + w_len + 50, tbox_y + 48), tot_m_str, font=f_total_val, fill=(96, 185, 255, 255))

    # =========================================================================
    # RIGHT COLUMN: LIVE TOP 3 RANKING BOARD (x=576 to 1152)
    # =========================================================================
    rx = 576
    rw = 574
    ry = 44
    rh = 542

    draw.rounded_rectangle(
        (rx, ry, rx + rw, ry + rh),
        radius=24,
        fill=(13, 16, 25, 238),
        outline=(theme["accent"][0], theme["accent"][1], theme["accent"][2], 140),
        width=2,
    )

    # Panel Header
    draw.text((rx + 26, ry + 20), "★ リアルタイム上位3店舗", font=f_panel_hdr, fill=(229, 200, 120, 255))
    live_badge = "● LIVE RANKING"
    lb_w = int(draw.textlength(live_badge, font=f_panel_hdr))
    draw.text((rx + rw - lb_w - 26, ry + 20), live_badge, font=f_panel_hdr, fill=(74, 222, 128, 255))
    draw.line((rx + 24, ry + 54, rx + rw - 24, ry + 54), fill=(255, 255, 255, 35), width=1)

    rank_colors = [
        ((251, 191, 36), (68, 48, 10)),   # 1st Gold
        ((203, 213, 225), (45, 55, 72)),  # 2nd Silver
        ((217, 119, 6), (65, 32, 10)),    # 3rd Bronze
    ]

    card_start_y = ry + 70
    card_h = 138
    card_gap = 14

    for idx in range(3):
        cy = card_start_y + idx * (card_h + card_gap)
        cx = rx + 22
        cw = rw - 44

        s = top3[idx] if idx < len(top3) else {"name": "---", "women": 0, "men": 0, "women_diff": 0}
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        tot = w + m
        w_pct = round((w / tot) * 100) if tot > 0 else 50
        w_diff = int(s.get("women_diff") or 0)
        ja_name = x_poster.format_ja_store_name(s.get("name", ""))

        is_first = (idx == 0)
        card_bg = (28, 24, 38, 245) if is_first else (22, 26, 38, 238)
        card_border = rank_colors[idx][0] if is_first else (255, 255, 255, 42)

        draw.rounded_rectangle(
            (cx, cy, cx + cw, cy + card_h),
            radius=16,
            fill=card_bg,
            outline=card_border,
            width=2 if is_first else 1,
        )

        # Rank Badge (1, 2, 3)
        r_fg, r_bg = rank_colors[idx]
        draw.rounded_rectangle(
            (cx + 18, cy + 16, cx + 56, cy + 54),
            radius=10,
            fill=r_bg,
            outline=r_fg,
            width=2,
        )
        r_str = str(idx + 1)
        rw_txt = int(draw.textlength(r_str, font=f_rank_num))
        draw.text((cx + 37 - rw_txt // 2, cy + 21), r_str, font=f_rank_num, fill=r_fg)

        # Store Name
        draw.text((cx + 70, cy + 20), ja_name, font=f_store, fill=(255, 255, 255, 255))
        name_w = int(draw.textlength(ja_name, font=f_store))

        # Momentum Pill (▲ +diff) if positive
        if w_diff > 0:
            diff_txt = f"▲ +{w_diff}"
            dw_txt = int(draw.textlength(diff_txt, font=f_diff)) + 18
            dx = cx + 70 + name_w + 12
            if dx + dw_txt < cx + cw - 135:
                draw.rounded_rectangle(
                    (dx, cy + 22, dx + dw_txt, cy + 50),
                    radius=8,
                    fill=(20, 65, 42, 235),
                    outline=(74, 222, 128, 190),
                    width=1,
                )
                draw.text((dx + 9, cy + 27), diff_txt, font=f_diff, fill=(134, 239, 172, 255))

        # Female Ratio Pill (Top Right of Card)
        pct_txt = f"女性比 {w_pct}%"
        pw_txt = int(draw.textlength(pct_txt, font=f_pct_pill)) + 24
        px = cx + cw - pw_txt - 18
        pill_fill = (219, 39, 119, 240) if w_pct >= 50 else (51, 65, 85, 240)
        draw.rounded_rectangle(
            (px, cy + 18, px + pw_txt, cy + 52),
            radius=9,
            fill=pill_fill,
        )
        draw.text((px + 12, cy + 25), pct_txt, font=f_pct_pill, fill=(255, 255, 255, 255))

        # Counts Row (女性 XX名 / 男性 XX名)
        cnt_y = cy + 66
        w_cnt_str = f"女性 {w}名"
        m_cnt_str = f"男性 {m}名"
        draw.text((cx + 20, cnt_y), w_cnt_str, font=f_count, fill=(255, 115, 172, 255))
        m_str_w = int(draw.textlength(m_cnt_str, font=f_count))
        draw.text((cx + cw - m_str_w - 20, cnt_y), m_cnt_str, font=f_count, fill=(96, 185, 255, 255))

        # Horizontal Ratio Bar
        bar_x = cx + 20
        bar_y = cy + 104
        bar_w = cw - 40
        bar_h = 14
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=7,
            fill=(56, 152, 236, 225),
        )
        w_bar_w = max(14, min(bar_w, int(bar_w * (w_pct / 100.0))))
        if tot > 0:
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + w_bar_w, bar_y + bar_h),
                radius=7,
                fill=(236, 72, 153, 255),
            )

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def generate_weekly_ogp_png(report: dict = None) -> bytes:
    """
    Render a 1200x630 Weekly Summary Whitepaper OGP card image (PNG bytes) for /stats and Sunday weekly X posts.
    """
    W, H = 1200, 630
    report = report or {}
    bg_path = os.path.join(BASE_DIR, "static", "guide-brand-comparison.jpg")
    if os.path.exists(bg_path):
        try:
            bg = Image.open(bg_path).convert("RGBA")
            bg_ratio = bg.width / bg.height
            target_ratio = W / H
            if bg_ratio > target_ratio:
                new_h = H
                new_w = int(H * bg_ratio)
            else:
                new_w = W
                new_h = int(W / bg_ratio)
            bg = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = (new_w - W) // 2
            top = (new_h - H) // 2
            bg = bg.crop((left, top, left + W, top + H))
            bg = bg.filter(ImageFilter.GaussianBlur(radius=3))
        except Exception:
            bg = Image.new("RGBA", (W, H), (10, 12, 20, 255))
    else:
        bg = Image.new("RGBA", (W, H), (10, 12, 20, 255))

    overlay = Image.new("RGBA", (W, H), (10, 12, 22, 222))
    canvas = Image.alpha_composite(bg, overlay)
    _draw_radial_glow(canvas, (1040, 100), 480, (229, 200, 120, 85))
    _draw_radial_glow(canvas, (300, 560), 440, (56, 189, 248, 65))

    draw = ImageDraw.Draw(canvas, "RGBA")

    f_badge = _get_font(17)
    f_date = _get_font(20)
    f_title = _get_font(43)
    f_sub = _get_font(18)
    f_total_lbl = _get_font(16)
    f_total_val = _get_font(23)

    f_panel_hdr = _get_font(18)
    f_rank_num = _get_font(24)
    f_store = _get_font(26)
    f_pct_pill = _get_font(16)
    f_count = _get_font(20)

    period_str = report.get("period") or "直近7日間"
    stores = report.get("stores") or []
    anaba_stores = report.get("anaba_stores") or []

    lx = 52
    by = 48
    badge_text = "● WEEKLY DATA WHITEPAPER"
    bw = int(draw.textlength(badge_text, font=f_badge)) + 34
    bh = 36
    draw.rounded_rectangle(
        (lx, by, lx + bw, by + bh),
        radius=18,
        fill=(55, 42, 14, 235),
        outline=(229, 200, 120, 255),
        width=2,
    )
    draw.text((lx + 17, by + 8), badge_text, font=f_badge, fill=(251, 191, 36, 255))

    dy = by + bh + 22
    draw.text((lx, dy), f"【週間統計レポート】{period_str}", font=f_date, fill=(56, 189, 248, 255))

    ty1 = dy + 40
    draw.text((lx, ty1), "今週もっとも女性が", font=f_title, fill=(255, 255, 255, 255))
    ty2 = ty1 + 58
    draw.text((lx, ty2), "集まったラウンジTOP3", font=f_title, fill=(251, 191, 36, 255))

    sy = ty2 + 66
    draw.rounded_rectangle(
        (lx, sy, lx + 492, sy + 46),
        radius=10,
        fill=(22, 26, 38, 225),
        outline=(255, 255, 255, 35),
        width=1,
    )
    draw.rounded_rectangle((lx, sy, lx + 6, sy + 46), radius=3, fill=(251, 191, 36, 255))
    draw.text((lx + 20, sy + 13), "全国53店舗・7日間のピーク平均＆穴場データ白書", font=f_sub, fill=(238, 240, 246, 255))

    tbox_y = sy + 64
    tbox_w = 492
    tbox_h = 104
    draw.rounded_rectangle(
        (lx, tbox_y, lx + tbox_w, tbox_y + tbox_h),
        radius=16,
        fill=(14, 22, 34, 235),
        outline=(56, 189, 248, 110),
        width=1,
    )
    draw.text((lx + 22, tbox_y + 16), "◆ 今週の「高女子率・穴場」No.1店舗", font=f_total_lbl, fill=(148, 212, 255, 255))
    if anaba_stores:
        a1 = anaba_stores[0]
        a1_name = x_poster.format_ja_store_name(a1.get("name", ""))
        a1_pct = a1.get("avg_wpct", 58)
        a1_w = a1.get("avg_women", 30)
        draw.text((lx + 22, tbox_y + 48), f"{a1_name}（女性比 {a1_pct}% / 平均{a1_w}名）", font=f_total_val, fill=(74, 222, 128, 255))
    else:
        draw.text((lx + 22, tbox_y + 48), "水・木 20:00〜22:00（全国平均女性比 57%）", font=f_total_val, fill=(74, 222, 128, 255))

    rx = 576
    rw = 574
    ry = 44
    rh = 542
    draw.rounded_rectangle(
        (rx, ry, rx + rw, ry + rh),
        radius=24,
        fill=(13, 16, 25, 240),
        outline=(229, 200, 120, 150),
        width=2,
    )
    draw.text((rx + 26, ry + 20), "★ 週間ピーク平均 上位3店舗", font=f_panel_hdr, fill=(251, 191, 36, 255))
    live_badge = "● 7-DAY STATS"
    lb_w = int(draw.textlength(live_badge, font=f_panel_hdr))
    draw.text((rx + rw - lb_w - 26, ry + 20), live_badge, font=f_panel_hdr, fill=(56, 189, 248, 255))
    draw.line((rx + 24, ry + 54, rx + rw - 24, ry + 54), fill=(255, 255, 255, 35), width=1)

    rank_colors = [
        ((251, 191, 36), (68, 48, 10)),
        ((203, 213, 225), (45, 55, 72)),
        ((217, 119, 6), (65, 32, 10)),
    ]
    card_start_y = ry + 70
    card_h = 138
    card_gap = 14

    for idx in range(3):
        cy = card_start_y + idx * (card_h + card_gap)
        cx = rx + 22
        cw = rw - 44

        s = stores[idx] if idx < len(stores) else {"name": "---", "avg_women": 0, "avg_men": 0, "max_women": 0}
        avg_w = float(s.get("avg_women") or 0)
        avg_m = float(s.get("avg_men") or 0)
        max_w = int(s.get("max_women") or 0)
        tot = avg_w + avg_m
        w_pct = round((avg_w / tot) * 100) if tot > 0 else 52
        ja_name = x_poster.format_ja_store_name(s.get("name", ""))

        is_first = (idx == 0)
        card_bg = (28, 24, 38, 245) if is_first else (22, 26, 38, 238)
        card_border = rank_colors[idx][0] if is_first else (255, 255, 255, 42)
        draw.rounded_rectangle(
            (cx, cy, cx + cw, cy + card_h),
            radius=16,
            fill=card_bg,
            outline=card_border,
            width=2 if is_first else 1,
        )

        r_fg, r_bg = rank_colors[idx]
        draw.rounded_rectangle((cx + 18, cy + 16, cx + 56, cy + 54), radius=10, fill=r_bg, outline=r_fg, width=2)
        r_str = str(idx + 1)
        rw_txt = int(draw.textlength(r_str, font=f_rank_num))
        draw.text((cx + 37 - rw_txt // 2, cy + 21), r_str, font=f_rank_num, fill=r_fg)

        draw.text((cx + 70, cy + 20), ja_name, font=f_store, fill=(255, 255, 255, 255))

        pct_txt = f"週間最大 {max_w}名"
        pw_txt = int(draw.textlength(pct_txt, font=f_pct_pill)) + 24
        px = cx + cw - pw_txt - 18
        draw.rounded_rectangle((px, cy + 18, px + pw_txt, cy + 52), radius=9, fill=(219, 39, 119, 235))
        draw.text((px + 12, cy + 25), pct_txt, font=f_pct_pill, fill=(255, 255, 255, 255))

        cnt_y = cy + 66
        w_cnt_str = f"女性ピーク平均 {avg_w:.0f}名"
        m_cnt_str = f"男性平均 {avg_m:.0f}名"
        draw.text((cx + 20, cnt_y), w_cnt_str, font=f_count, fill=(255, 115, 172, 255))
        m_str_w = int(draw.textlength(m_cnt_str, font=f_count))
        draw.text((cx + cw - m_str_w - 20, cnt_y), m_cnt_str, font=f_count, fill=(96, 185, 255, 255))

        bar_x = cx + 20
        bar_y = cy + 104
        bar_w = cw - 40
        bar_h = 14
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=7, fill=(56, 152, 236, 225))
        w_bar_w = max(14, min(bar_w, int(bar_w * (w_pct / 100.0))))
        if tot > 0:
            draw.rounded_rectangle((bar_x, bar_y, bar_x + w_bar_w, bar_y + bar_h), radius=7, fill=(236, 72, 153, 255))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
