#!/usr/bin/env python3
"""
render_editorial_eyecatch.py - Clean-Minimal Brand Icon & Data Editorial Eyecatch Generator.

Renders:
  1. App Icons (Plan A: Interlocking Dual Ring on #111113 matte charcoal):
     - static/icon.png (512x512)
     - static/icon-512.png (512x512)
     - static/icon-192.png (192x192)
  2. All 24 Editorial Data Card Eyecatches (1280x720 .jpg & .webp) + 3 Thumbnails (640x360 .jpg & .webp)
  3. CLI mode (`--slug <slug> --cat ... --sample ... --title ... --sub ...`) for future `/article` runs.
"""

import argparse
import html as html_mod
import os
from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
STATIC_DIR = os.path.join(REPO_ROOT, "static")

ICON_SVG_512 = """
<svg viewBox="0 0 512 512" width="512" height="512" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bgGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#18181c"/>
      <stop offset="100%" stop-color="#101013"/>
    </linearGradient>
    <clipPath id="topCrossClip">
      <rect x="180" y="110" width="152" height="152"/>
    </clipPath>
    <clipPath id="bottomCrossClip">
      <rect x="180" y="262" width="152" height="152"/>
    </clipPath>
  </defs>
  <rect width="512" height="512" fill="url(#bgGrad)"/>
  <circle cx="214" cy="262" r="102" fill="none" stroke="#f4f4f5" stroke-width="29"/>
  <circle cx="298" cy="262" r="102" fill="none" stroke="#e29aa8" stroke-width="29"/>
  <g clip-path="url(#topCrossClip)">
    <circle cx="214" cy="262" r="102" fill="none" stroke="#141418" stroke-width="43"/>
    <circle cx="214" cy="262" r="102" fill="none" stroke="#f4f4f5" stroke-width="29"/>
  </g>
  <g clip-path="url(#bottomCrossClip)">
    <circle cx="298" cy="262" r="102" fill="none" stroke="#121216" stroke-width="43"/>
    <circle cx="298" cy="262" r="102" fill="none" stroke="#e29aa8" stroke-width="29"/>
  </g>
  <circle cx="394" cy="134" r="14.5" fill="#f4f4f5"/>
</svg>
"""

MINI_MARK_SVG = """
<svg class="brand-mark" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <circle cx="25" cy="33" r="13.5" fill="none" stroke="#f4f4f5" stroke-width="4.5"/>
  <circle cx="39" cy="33" r="13.5" fill="none" stroke="#e29aa8" stroke-width="4.5"/>
  <circle cx="51" cy="15" r="3.2" fill="#f4f4f5"/>
</svg>
"""

COMMON_CARD_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  width: 1280px;
  height: 720px;
  overflow: hidden;
  background: #121215;
  color: #f4f4f5;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Hiragino Sans", "Hiragino Kaku Gothic ProN", sans-serif;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "palt";
}
.canvas {
  position: relative;
  width: 1280px;
  height: 720px;
  padding: 52px 72px 50px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background:
    radial-gradient(circle at 82% 18%, rgba(255,255,255,0.032) 0%, rgba(18,18,21,0) 54%),
    linear-gradient(180deg, #151519 0%, #111114 100%);
}
.canvas::before {
  content: "";
  position: absolute;
  inset: 24px;
  border: 1px solid rgba(255,255,255,0.065);
  border-radius: 20px;
  pointer-events: none;
}
.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  z-index: 2;
}
.brand-pill {
  display: inline-flex;
  align-items: center;
  gap: 12px;
}
.brand-mark { width: 28px; height: 28px; }
.brand-kicker {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.14em;
  color: #a1a1aa;
}
.brand-sep { color: #3f3f46; margin: 0 2px; }
.brand-cat {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: #d4d4d8;
}
.meta-sample {
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0.06em;
  color: #8e8e98;
  font-variant-numeric: tabular-nums;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.08);
  background: rgba(255,255,255,0.02);
}
.viz-stage {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 44px;
  margin: 22px 0 18px;
  z-index: 2;
}
.bottom-bar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 28px;
  padding-top: 20px;
  border-top: 1px solid rgba(255,255,255,0.075);
  z-index: 2;
}
.title-main {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.015em;
  color: #f4f4f5;
  line-height: 1.25;
  white-space: nowrap;
}
.title-sub {
  margin-top: 7px;
  font-size: 17px;
  font-weight: 400;
  color: #9ca3af;
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.legend-group {
  display: flex;
  align-items: center;
  gap: 20px;
  font-size: 14px;
  color: #a1a1aa;
  white-space: nowrap;
}
.legend-item { display: inline-flex; align-items: center; gap: 8px; }
.dot-rose { width: 10px; height: 10px; border-radius: 50%; background: #e29aa8; display: inline-block; }
.dot-blue { width: 10px; height: 10px; border-radius: 50%; background: #8ab4f8; display: inline-block; }
.dot-stone { width: 10px; height: 10px; border-radius: 50%; background: #f4f4f5; display: inline-block; }
"""


# ---------- Reusable Stage Builders ----------

def stage_dual_curve(
    x_labels,
    rose_pts,
    blue_pts,
    callout_left,
    callout_right,
    side_cards,
    y_labels=("75%", "50%", "25%"),
):
    """Smooth two-curve crossover chart + 2 concise insight cards on the right."""
    def _poly_to_cubic(pts):
        if len(pts) < 2:
            return ""
        d = [f"M {pts[0][0]} {pts[0][1]}"]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            xm = (x0 + x1) / 2.0
            d.append(f"C {xm:.1f} {y0}, {xm:.1f} {y1}, {x1} {y1}")
        return " ".join(d)

    rose_d = _poly_to_cubic(rose_pts)
    blue_d = _poly_to_cubic(blue_pts)
    area_d = f"{rose_d} L {rose_pts[-1][0]} 274 L {rose_pts[0][0]} 274 Z"

    xs_svg = "".join(
        f'<text x="{x}" y="304" text-anchor="middle" fill="#a1a1aa" font-size="15" font-weight="600">{lbl}</text>'
        for x, lbl in x_labels
    )
    lx, ly, ltxt = callout_left[:3]
    lcol = callout_left[3] if len(callout_left) > 3 else "#e29aa8"
    lstroke = "rgba(226,154,168,0.38)" if lcol == "#e29aa8" else "rgba(138,180,248,0.38)"
    rx, ry, rtxt = callout_right[:3]
    rcol = callout_right[3] if len(callout_right) > 3 else "#8ab4f8"
    rstroke = "rgba(226,154,168,0.38)" if rcol == "#e29aa8" else "rgba(138,180,248,0.38)"

    cards_html = ""
    for kicker, color, heading, body in side_cards:
        cards_html += f"""
        <div style="padding: 21px 23px; border-radius: 16px; background: rgba(255,255,255,0.028); border: 1px solid rgba(255,255,255,0.08);">
          <div style="font-size: 13px; letter-spacing: 0.08em; color: {color}; font-weight: 600; margin-bottom: 6px;">{kicker}</div>
          <div style="font-size: 19.5px; font-weight: 700; color: #f4f4f5; margin-bottom: 6px; white-space: nowrap;">{heading}</div>
          <div style="font-size: 15px; color: #a1a1aa; line-height: 1.5;">{body}</div>
        </div>
        """

    return f"""
    <div style="flex: 1.3; height: 100%; display: flex; flex-direction: column; justify-content: center;">
      <svg viewBox="0 0 650 330" width="100%" height="330" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="roseArea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#e29aa8" stop-opacity="0.16"/>
            <stop offset="100%" stop-color="#e29aa8" stop-opacity="0.0"/>
          </linearGradient>
        </defs>
        <line x1="58" y1="54" x2="626" y2="54" stroke="rgba(255,255,255,0.07)" stroke-width="1"/>
        <line x1="58" y1="164" x2="626" y2="164" stroke="rgba(255,255,255,0.11)" stroke-width="1" stroke-dasharray="4 6"/>
        <line x1="58" y1="274" x2="626" y2="274" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>
        <text x="44" y="59" text-anchor="end" fill="#71717a" font-size="14">{y_labels[0]}</text>
        <text x="44" y="169" text-anchor="end" fill="#a1a1aa" font-size="14">{y_labels[1]}</text>
        <text x="44" y="279" text-anchor="end" fill="#71717a" font-size="14">{y_labels[2]}</text>
        {xs_svg}
        <path d="{area_d}" fill="url(#roseArea)"/>
        <path d="{rose_d}" fill="none" stroke="#e29aa8" stroke-width="4"/>
        <path d="{blue_d}" fill="none" stroke="#8ab4f8" stroke-width="3.5"/>
        <circle cx="{lx}" cy="{ly}" r="6.5" fill="#121215" stroke="{lcol}" stroke-width="3.5"/>
        <rect x="{max(64, lx - 24)}" y="{max(16, ly - 50)}" width="168" height="34" rx="8" fill="#1c1c21" stroke="{lstroke}"/>
        <text x="{max(64, lx - 24) + 84}" y="{max(16, ly - 50) + 22}" text-anchor="middle" fill="#f4f4f5" font-size="14" font-weight="600">{ltxt}</text>
        <circle cx="{rx}" cy="{ry}" r="6.5" fill="#121215" stroke="{rcol}" stroke-width="3.5"/>
        <rect x="{min(436, rx - 140)}" y="{max(18, ry - 52)}" width="180" height="34" rx="8" fill="#1c1c21" stroke="{rstroke}"/>
        <text x="{min(436, rx - 140) + 90}" y="{max(18, ry - 52) + 22}" text-anchor="middle" fill="#f4f4f5" font-size="14" font-weight="600">{rtxt}</text>
      </svg>
    </div>
    <div style="flex: 0.96; display: flex; flex-direction: column; gap: 16px;">
      {cards_html}
    </div>
    """


def stage_three_kpi_cards(cards):
    """3 clean comparison metric cards across the stage (middle or highlighted card gets rose accent)."""
    cols = ""
    for c in cards:
        is_hi = c.get("highlight", False)
        bg = "rgba(226,154,168,0.06)" if is_hi else "rgba(255,255,255,0.025)"
        bd = "1.5px solid rgba(226,154,168,0.42)" if is_hi else "1px solid rgba(255,255,255,0.08)"
        k_col = c.get("color", "#e29aa8" if is_hi else "#a1a1aa")
        badge_html = (
            f'<span style="padding: 3px 10px; border-radius: 999px; background: rgba(226,154,168,0.18); color: #f4f4f5; font-size: 12.5px; font-weight: 700; white-space: nowrap; flex-shrink: 0;">{c["badge"]}</span>'
            if c.get("badge")
            else ""
        )
        bar_w = max(8, min(100, c.get("bar_pct", 60)))
        bar_col = c.get("bar_color", "#e29aa8" if is_hi else "#f4f4f5")
        cols += f"""
        <div style="padding: 26px 25px; border-radius: 16px; background: {bg}; border: {bd}; display: flex; flex-direction: column; justify-content: space-between;">
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; min-height: 24px;">
              <div style="font-size: 13px; color: {k_col}; font-weight: 600; letter-spacing: 0.04em; white-space: nowrap;">{c["kicker"]}</div>
              {badge_html}
            </div>
            <div style="font-size: 20px; font-weight: 700; color: #f4f4f5; margin-top: 8px; white-space: nowrap;">{c["heading"]}</div>
          </div>
          <div style="margin: 22px 0;">
            <div style="font-size: 44px; font-weight: 700; color: {c.get("val_color", "#f4f4f5")}; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; white-space: nowrap;">{c["value"]}<span style="font-size: 20px; margin-left: 3px;">{c.get("unit", "")}</span></div>
            <div style="font-size: 14px; color: #a1a1aa; margin-top: 6px; line-height: 1.45;">{c["desc"]}</div>
          </div>
          <div style="height: 8px; border-radius: 999px; background: rgba(255,255,255,0.07); overflow: hidden;">
            <div style="width: {bar_w}%; height: 100%; background: {bar_col};"></div>
          </div>
        </div>
        """
    return f'<div style="width: 100%; display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 22px; align-items: stretch;">{cols}</div>'


def stage_spectrum_rows(header_cols, scale_ticks, rows):
    """Horizontal range / spectrum bar rows (3 rows) for brand, area, or time comparisons."""
    ticks_html = "".join(f"<span>{t}</span>" for t in scale_ticks)
    rows_html = ""
    for r in rows:
        is_hi = r.get("highlight", False)
        bg = "rgba(255,255,255,0.035)" if is_hi else "rgba(255,255,255,0.022)"
        bd = "rgba(226,154,168,0.34)" if is_hi else "rgba(255,255,255,0.075)"
        v_col = r.get("verdict_color", "#e29aa8" if is_hi else "#f4f4f5")
        rows_html += f"""
        <div style="display: grid; grid-template-columns: 316px 1fr 206px; align-items: center; gap: 24px; padding: 17px 20px; border-radius: 14px; background: {bg}; border: 1px solid {bd};">
          <div>
            <div style="font-size: 18px; font-weight: 700; color: #f4f4f5; white-space: nowrap;">{r["name"]}</div>
            <div style="font-size: 13px; color: #9ca3af; margin-top: 3px; white-space: nowrap;">{r["sub"]}</div>
          </div>
          <div style="position: relative; height: 14px; border-radius: 999px; background: rgba(255,255,255,0.06);">
            <div style="position: absolute; left: {r["left_pct"]}%; width: {r["width_pct"]}%; height: 100%; border-radius: 999px; background: {r["bar_color"]};"></div>
          </div>
          <div style="text-align: right; font-size: 15.5px; font-weight: 700; color: {v_col}; white-space: nowrap;">{r["verdict"]}</div>
        </div>
        """
    return f"""
    <div style="width: 100%; display: flex; flex-direction: column; justify-content: center; gap: 18px;">
      <div style="display: grid; grid-template-columns: 316px 1fr 206px; align-items: center; gap: 24px; padding: 0 8px 8px; border-bottom: 1px solid rgba(255,255,255,0.08); color: #8e8e98; font-size: 14px; font-weight: 600;">
        <div>{header_cols[0]}</div>
        <div style="display: flex; justify-content: space-between;">{ticks_html}</div>
        <div style="text-align: right;">{header_cols[1]}</div>
      </div>
      {rows_html}
    </div>
    """


def stage_four_step_flow(steps):
    """Clean 4-column horizontal step / timeline flow."""
    cols = ""
    for idx, st in enumerate(steps, 1):
        is_hi = st.get("highlight", False)
        bd = "1.5px solid rgba(226,154,168,0.42)" if is_hi else "1px solid rgba(255,255,255,0.08)"
        bg = "rgba(226,154,168,0.055)" if is_hi else "rgba(255,255,255,0.024)"
        acc = "#e29aa8" if is_hi else st.get("color", "#a1a1aa")
        cols += f"""
        <div style="padding: 24px 20px; border-radius: 16px; background: {bg}; border: {bd}; display: flex; flex-direction: column; justify-content: space-between;">
          <div>
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
              <span style="font-size: 13px; font-weight: 700; letter-spacing: 0.08em; color: {acc};">{st["step"]}</span>
              <span style="font-size: 13px; color: #71717a; font-variant-numeric: tabular-nums;">0{idx}</span>
            </div>
            <div style="font-size: 18px; font-weight: 700; color: #f4f4f5; line-height: 1.35; white-space: nowrap;">{st["title"]}</div>
          </div>
          <div style="margin-top: 20px; padding-top: 14px; border-top: 1px solid rgba(255,255,255,0.07);">
            <div style="font-size: 24px; font-weight: 700; color: {st.get("metric_color", "#f4f4f5")}; font-variant-numeric: tabular-nums; white-space: nowrap;">{st["metric"]}</div>
            <div style="font-size: 13.5px; color: #9ca3af; margin-top: 5px; line-height: 1.45;">{st["desc"]}</div>
          </div>
        </div>
        """
    return f'<div style="width: 100%; display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; align-items: stretch;">{cols}</div>'


# Special Architectural Stage for 70cm Dress Code
STAGE_DRESS_70CM = """
<div style="flex: 1.08; height: 100%; display: flex; align-items: center; justify-content: center;">
  <svg viewBox="0 0 540 330" width="100%" height="330" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="coneLight" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#f4f4f5" stop-opacity="0.16"/>
        <stop offset="100%" stop-color="#f4f4f5" stop-opacity="0.0"/>
      </linearGradient>
    </defs>
    <line x1="90" y1="36" x2="450" y2="36" stroke="rgba(255,255,255,0.14)" stroke-width="1.5"/>
    <rect x="246" y="28" width="48" height="10" rx="3" fill="#f4f4f5"/>
    <polygon points="254,38 286,38 408,248 132,248" fill="url(#coneLight)"/>
    <text x="270" y="72" text-anchor="middle" fill="#a1a1aa" font-size="14" letter-spacing="0.06em">暖色ピンスポット照射角 38°</text>
    <path d="M 78 182 L 106 182 L 106 248 L 168 248 L 168 274 L 78 274 Z" fill="none" stroke="#d4d4d8" stroke-width="2.5"/>
    <path d="M 462 182 L 434 182 L 434 248 L 372 248 L 372 274 L 462 274 Z" fill="none" stroke="#d4d4d8" stroke-width="2.5"/>
    <rect x="218" y="218" width="104" height="10" rx="2" fill="#e29aa8"/>
    <line x1="270" y1="228" x2="270" y2="274" stroke="#71717a" stroke-width="2.5"/>
    <line x1="238" y1="274" x2="302" y2="274" stroke="#71717a" stroke-width="2.5"/>
    <line x1="146" y1="148" x2="394" y2="148" stroke="#f4f4f5" stroke-width="2"/>
    <line x1="146" y1="138" x2="146" y2="158" stroke="#f4f4f5" stroke-width="2"/>
    <line x1="394" y1="138" x2="394" y2="158" stroke="#f4f4f5" stroke-width="2"/>
    <rect x="212" y="128" width="116" height="38" rx="8" fill="#121215" stroke="rgba(255,255,255,0.22)"/>
    <text x="270" y="153" text-anchor="middle" fill="#f4f4f5" font-size="21" font-weight="700">距離 70cm</text>
    <text x="270" y="308" text-anchor="middle" fill="#8e8e98" font-size="14">口元の青ヒゲ・Tゾーン皮脂・襟元のヨレが陰影で強調される距離</text>
  </svg>
</div>
<div style="flex: 1; display: flex; flex-direction: column; gap: 12px;">
  <div style="font-size: 14px; font-weight: 600; letter-spacing: 0.08em; color: #a1a1aa; margin-bottom: 4px;">BRAND DRESS CODE SPECTRUM</div>
  <div style="display: flex; align-items: center; justify-content: space-between; padding: 15px 20px; border-radius: 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);">
    <span style="font-size: 17px; font-weight: 600; color: #f4f4f5;">オリエンタルラウンジ</span>
    <span style="font-size: 14px; color: #e29aa8; font-weight: 600;">厳格 ｜ ジャケパン・襟付き推奨</span>
  </div>
  <div style="display: flex; align-items: center; justify-content: space-between; padding: 15px 20px; border-radius: 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);">
    <span style="font-size: 17px; font-weight: 600; color: #f4f4f5;">ag（アグ） / JIS</span>
    <span style="font-size: 14px; color: #d4d4d8;">標準 ｜ きれいめ私服（スウェットNG）</span>
  </div>
  <div style="display: flex; align-items: center; justify-content: space-between; padding: 15px 20px; border-radius: 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);">
    <span style="font-size: 17px; font-weight: 600; color: #f4f4f5;">ヤタコイ / ASOBIBAR</span>
    <span style="font-size: 14px; color: #8ab4f8;">寛容 ｜ カジュアル可（清潔感重視）</span>
  </div>
</div>
"""


def build_all_asset_specs():
    return [
        # 1. Lounge vs Screened 1-on-1 Date
        {
            "base": "eyecatch-lounge-vs-screened-date",
            "cat": "COMPARISON STUDY",
            "sample": "LOG n = 96,607",
            "stage": stage_dual_curve(
                x_labels=[(96, "20:00"), (264, "21:30"), (434, "23:00"), (588, "00:00")],
                rose_pts=[(96, 73), (264, 118), (434, 162), (588, 190)],
                blue_pts=[(96, 262), (264, 232), (434, 180), (588, 148)],
                callout_left=(96, 73, "20時 女性比 70.6%"),
                callout_right=(588, 148, "0時 男性先行 39.7%"),
                side_cards=[
                    ("MULTI-PERSON LIVE", "#e29aa8", "相席ラウンジが向く夜", "20〜21時台の黄金帯に、友人2人組で当日のライブ感を楽しむ夜"),
                    ("SCREENED 1-ON-1", "#8ab4f8", "審査制1対1が向く日", "事前メッセージを省き、週末カフェで1対1の対話に集中したい日"),
                ],
            ),
            "title": "相席ラウンジ vs 審査制1対1デート徹底比較",
            "sub": "複数人のライブ感か、メッセージ不要の1対1か ― 実測ログで見る使い分け境界線",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>平均女性比率</span><span class="legend-item"><span class="dot-blue"></span>男性先行（待機）発生率</span>',
        },
        # 2. Dress Code & 70cm Cleanliness
        {
            "base": "eyecatch-dress-code-cleanliness",
            "cat": "DRESS CODE & GROOMING",
            "sample": "5 BRANDS STANDARD",
            "stage": STAGE_DRESS_70CM,
            "title": "70cmソファ席の間接照明とブランド別服装基準",
            "sub": "入店NGドレスコード一覧と、近距離の陰影で損しない3分身だしなみチェック",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>対座70cm視線エリア</span><span class="legend-item"><span class="dot-stone"></span>5ブランド公式基準</span>',
        },
        # 3. Thirties Lounge Guide
        {
            "base": "eyecatch-thirties-lounge-guide",
            "cat": "DEMOGRAPHIC STUDY",
            "sample": "LOG n = 276,400",
            "stage": stage_spectrum_rows(
                header_cols=("ブランド", "30代の馴染みやすさ"),
                scale_ticks=("20歳", "24歳", "28歳", "32歳", "36歳+"),
                rows=[
                    {"name": "オリエンタルラウンジ", "sub": "水・木 20:00〜22:00 社会人帯", "left_pct": 32, "width_pct": 52, "bar_color": "#e29aa8", "verdict": "最適（社会人6割超）", "highlight": True},
                    {"name": "JIS（ジス）", "sub": "主要都市大型店・1人入店可", "left_pct": 22, "width_pct": 48, "bar_color": "#f4f4f5", "verdict": "良好（20代後半〜30代）"},
                    {"name": "ag（アグ）", "sub": "Z世代・週末ハイテンション帯", "left_pct": 4, "width_pct": 34, "bar_color": "#8ab4f8", "verdict": "20代前半中心（7割）", "verdict_color": "#9ca3af"},
                ],
            ),
            "title": "30代・アラサーが浮かない店舗と時間帯の境界線",
            "sub": "ブランド別の年齢層ボリュームゾーンと、平日20時の社会人ゴールデンタイム検証",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>社会人中心レンジ</span><span class="legend-item"><span class="dot-blue"></span>Z世代中心レンジ</span>',
        },
        # 4. Rain & Payday Cycle
        {
            "base": "eyecatch-rain-payday",
            "cat": "CALENDAR & WEATHER",
            "sample": "LOG n = 96,607",
            "stage": stage_three_kpi_cards([
                {"kicker": "毎月 25日〜28日", "heading": "給料日直後の週末", "value": "54.7", "unit": "%", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "平均女性比率（男性来店が急増）", "bar_pct": 55, "bar_color": "#8ab4f8"},
                {"kicker": "毎月 15日〜24日", "heading": "給料日前の穴場週", "value": "62.1", "unit": "%", "badge": "+7.4pt", "highlight": True, "desc": "ライバル減少で女性優位が安定", "bar_pct": 62, "bar_color": "#e29aa8"},
                {"kicker": "雨天夜 × 駅地下直結", "heading": "傘いらず導線の大型店", "value": "64.8", "unit": "%", "desc": "路面店から地下直結店へ女性集中", "bar_pct": 65, "bar_color": "#f4f4f5"},
            ]),
            "title": "雨の日・給料日前後に生まれる「隠れ穴場日」の法則",
            "sub": "25日直後の男性集中を避け、給料日前と駅地下直結店で勝率が上がるデータ根拠",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>給料日前（15〜24日）</span><span class="legend-item"><span class="dot-blue"></span>給料日直後（25〜28日）</span>',
        },
        # 5. Brand Waveform Asymmetry
        {
            "base": "eyecatch-brand-waveform-asymmetry",
            "cat": "TIME WAVEFORM STUDY",
            "sample": "5 BRANDS WAVEFORM",
            "stage": stage_dual_curve(
                x_labels=[(96, "20:00"), (264, "21:30"), (434, "23:00"), (588, "01:00")],
                rose_pts=[(96, 78), (264, 96), (434, 174), (588, 224)],
                blue_pts=[(96, 226), (264, 198), (434, 134), (588, 92)],
                callout_left=(96, 78, "早時間 ラウンジ 68%"),
                callout_right=(588, 92, "深夜 大型・回遊店逆転"),
                side_cards=[
                    ("20:00 - 22:00 PEAK", "#e29aa8", "オリエンタルラウンジ・ヤタコイ", "1軒目ディナー兼用の女性ペアが先行し、早時間帯の着席効率が最高潮"),
                    ("23:30 - 01:30 PEAK", "#8ab4f8", "JIS・ag・ASOBIBAR", "終電後も2軒目・回遊層の流入が続き、深夜帯の主役が入れ替わる"),
                ],
            ),
            "title": "20時と深夜0時で入れ替わる5大ブランドの時間波形",
            "sub": "同じ街でも時間帯で主役が逆転する理由と、男女で真逆になる「当たり店」の選び方",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>早時間ピーク型</span><span class="legend-item"><span class="dot-blue"></span>深夜持続・回遊型</span>',
        },
        # 6. Crowd-Rush Paradox
        {
            "base": "eyecatch-crowd-rush-paradox",
            "cat": "60-MIN FORECAST STUDY",
            "sample": "LOG n = 980,000",
            "stage": stage_three_kpi_cards([
                {"kicker": "金・土 19:00台発", "heading": "先行入店セーフティ帯", "value": "6.1", "unit": "%", "desc": "60分後の男余り転落リスク（極めて低い）", "bar_pct": 14, "bar_color": "#f4f4f5"},
                {"kicker": "金・土 22:00台発（女性60%店）", "heading": "群衆殺到パラドックス", "value": "29.8", "unit": "%", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "見て向かった男性が同時到着し3回に1回逆転", "bar_pct": 60, "bar_color": "#8ab4f8"},
                {"kicker": "女性比 70%以上の店舗", "heading": "70% 安全バッファ法則", "value": "97.7", "unit": "%", "badge": "推奨基準", "highlight": True, "desc": "60分後も女性優位を維持する安全圏", "bar_pct": 92, "bar_color": "#e29aa8"},
            ]),
            "title": "金曜22時の「群衆殺到パラドックス」と70%安全圏",
            "sub": "女性比60%を見て向かうと3割が逆転する構造と、60分後AI予測で到着負けを防ぐ基準",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>70%安全バッファ</span><span class="legend-item"><span class="dot-blue"></span>22時台の逆転リスク</span>',
        },
        # 7. Last-Train Cliff
        {
            "base": "eyecatch-last-train-cliff",
            "cat": "MIDNIGHT TRANSITION",
            "sample": "LOG n = 96,607",
            "stage": stage_dual_curve(
                x_labels=[(96, "22:30"), (258, "23:20"), (426, "00:00"), (588, "00:45")],
                rose_pts=[(96, 82), (258, 178), (426, 206), (588, 126)],
                blue_pts=[(96, 170), (258, 152), (426, 142), (588, 156)],
                callout_left=(258, 178, "23:20 終電退店の崖", "#e29aa8"),
                callout_right=(588, 126, "00:30 第2波V字回復", "#e29aa8"),
                side_cards=[
                    ("23:20 - 00:00 CLIFF", "#8ab4f8", "魔の40分間（終電退店ラッシュ）", "郊外組の女性が一斉にお会計へ向かい、一時的に男性比率が急上昇"),
                    ("00:30+ SECOND WAVE", "#e29aa8", "深夜第2波が入る繁華街大型店", "タクシー圏・2軒目組が再合流し、残った店舗で再び男女比が整う"),
                ],
            ),
            "title": "23時20分の「終電崖」と深夜0時半の第2波データ",
            "sub": "魔の40分間で空気が変わる瞬間と、終電後に再び女性が増える店舗の見極め方",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>店内女性数の推移</span><span class="legend-item"><span class="dot-blue"></span>店内男性数の推移</span>',
        },
        # 8. Table Change Protocol
        {
            "base": "eyecatch-table-change-protocol",
            "cat": "TABLE CHANGE STUDY",
            "sample": "LOG n = 276,400",
            "stage": stage_three_kpi_cards([
                {"kicker": "店内女性 10名未満", "heading": "席替え待機リスク圏", "value": "要注意", "unit": "", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "次の案内卓が不足し「一人待機課金」に陥りやすい", "bar_pct": 28, "bar_color": "#8ab4f8"},
                {"kicker": "着席から 30〜40分", "heading": "スマート離脱の黄金窓", "value": "35", "unit": "分", "badge": "最適目安", "highlight": True, "desc": "お手洗い導線やドリンク切替で角を立てず移動可能", "bar_pct": 68, "bar_color": "#e29aa8"},
                {"kicker": "店内女性 16名以上", "heading": "高回転・即再案内圏", "value": "5組+", "unit": "稼働", "desc": "複数テーブルが同時稼働しスムーズに次席へ接続", "bar_pct": 86, "bar_color": "#f4f4f5"},
            ]),
            "title": "気まずさを残さない席替え（チェンジ）の成功条件",
            "sub": "お互いの自尊心を守るスマートな伝え方と、待機課金を避ける「店内女性10名以上」の基準",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>推奨タイミング・人数</span><span class="legend-item"><span class="dot-blue"></span>待機発生リスク</span>',
        },
        # 9. Cost Break-Even
        {
            "base": "eyecatch-cost-break-even",
            "cat": "COST & TIME ROI",
            "sample": "PRICE SIMULATION",
            "stage": stage_three_kpi_cards([
                {"kicker": "相性を見極める基準時間", "heading": "40分見切りムーブ", "value": "3,600", "unit": "円前後", "badge": "高効率", "highlight": True, "desc": "延長課金が膨らむ前に判断し、次店舗へ余力を残せる", "bar_pct": 42, "bar_color": "#e29aa8"},
                {"kicker": "惰性で粘った1テーブル", "heading": "90分惰性滞在コスト", "value": "8,200", "unit": "円前後", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "週末加算込みで1万円に迫り、双方の疲労感も増大", "bar_pct": 88, "bar_color": "#8ab4f8"},
                {"kicker": "徒歩4分圏の別店舗へ移動", "heading": "40分×2店舗ハシゴ", "value": "2倍", "unit": "の機会", "desc": "同予算帯のまま空気の合う席に出会える確率を最大化", "bar_pct": 76, "bar_color": "#f4f4f5"},
            ]),
            "title": "90分粘る vs 40分で見切る料金コスパの損益分岐点",
            "sub": "10分単位の課金メーターで後悔しない滞在時間設計と、男女双方に優しい切り替え基準",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>40分見切り設計</span><span class="legend-item"><span class="dot-blue"></span>90分惰性延長</span>',
        },
        # 10. Osaka Kita vs Minami
        {
            "base": "eyecatch-osaka-kita-vs-minami",
            "cat": "OSAKA 9-STORE STUDY",
            "sample": "LOG n = 276,000",
            "stage": stage_spectrum_rows(
                header_cols=("大阪エリア（全9店舗）", "時間帯別の優位性"),
                scale_ticks=("19時", "21時", "23時", "01時", "03時"),
                rows=[
                    {"name": "梅田・茶屋町エリア", "sub": "オリエンタルラウンジ茶屋町・梅田 / ヤタコイ", "left_pct": 6, "width_pct": 46, "bar_color": "#e29aa8", "verdict": "20時台 女性比80%超", "highlight": True},
                    {"name": "天満・東通りエリア", "sub": "オリエンタルラウンジ天満 / ag梅田 / JIS梅田", "left_pct": 24, "width_pct": 54, "bar_color": "#f4f4f5", "verdict": "21時半〜深夜 安定高稼働"},
                    {"name": "難波・心斎橋（ミナミ）", "sub": "オリエンタルラウンジ難波・心斎橋 / JIS難波", "left_pct": 42, "width_pct": 54, "bar_color": "#8ab4f8", "verdict": "24時以降の深夜2強"},
                ],
            ),
            "title": "大阪キタ vs ミナミ全9店舗の「南北ハシゴ法則」",
            "sub": "20時の茶屋町、22時の裏天満、深夜0時以降のJIS梅田＆難波で主役が移る27万件ログ検証",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>早時間キタ優位</span><span class="legend-item"><span class="dot-blue"></span>深夜ミナミ・大型店優位</span>',
        },
        # 11. Local vs Urban
        {
            "base": "eyecatch-local-vs-urban",
            "cat": "REGIONAL COMPARISON",
            "sample": "51 STORES STUDY",
            "stage": stage_dual_curve(
                x_labels=[(96, "20:00"), (264, "21:30"), (434, "23:00"), (588, "00:30")],
                rose_pts=[(96, 178), (264, 146), (434, 118), (588, 72)],
                blue_pts=[(96, 86), (264, 76), (434, 152), (588, 208)],
                callout_left=(264, 76, "都心店 21時半ピーク", "#8ab4f8"),
                callout_right=(588, 72, "地方店 深夜121.1%急増", "#e29aa8"),
                side_cards=[
                    ("URBAN 33 STORES", "#8ab4f8", "都心大型クラスター（終電連動型）", "20〜22時に山場を迎え、23時以降は複数ブランドへ分散"),
                    ("REGIONAL 18 STORES", "#e29aa8", "地方1店舗都市（深夜一極集中型）", "居酒屋1軒目終わりの23時半〜深夜1時に街の女性が一極合流"),
                ],
            ),
            "title": "地方店と都心大型店で真逆に動く深夜0時の客足波形",
            "sub": "全国51店舗ログで判明した、地方1店舗エリア特有の「深夜121%逆転現象」と立ち回り",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>地方中核18店</span><span class="legend-item"><span class="dot-blue"></span>都心大型33店</span>',
        },
        # 12. Price & Event Comparison
        {
            "base": "eyecatch-price-comparison",
            "cat": "PRICE & EVENT GUIDE",
            "sample": "ALL STORES MATRIX",
            "stage": stage_three_kpi_cards([
                {"kicker": "相席前・待機中の課金", "heading": "待機料金 0円ルール", "value": "0", "unit": "円 / 10分", "desc": "案内待ち時間と相席成立後の時間単価を分けて比較", "bar_pct": 35, "bar_color": "#f4f4f5"},
                {"kicker": "早割・半額イベント判定", "heading": "4時間帯×イベント単価", "value": "-50", "unit": "%割引枠", "badge": "自動反映", "highlight": True, "desc": "オープン直後の早割や男性割引デーの実質単価を算出", "bar_pct": 74, "bar_color": "#e29aa8"},
                {"kicker": "成立しやすさの同時チェック", "heading": "女性10名以上×最安店", "value": "3秒", "unit": "横断比較", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "安さだけでなく「いま女性がいる店」を同エリアで抽出", "bar_pct": 88, "bar_color": "#8ab4f8"},
            ]),
            "title": "全国店舗の料金・イベント・現在人数の横断比較術",
            "sub": "待機料金・週末加算・ソロ入店可否・リアルタイム男女比をひと目で見比べる活用ガイド",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>実質単価・割引</span><span class="legend-item"><span class="dot-blue"></span>リアルタイム稼働条件</span>',
        },
        # 13. Male-Lead Mechanics
        {
            "base": "eyecatch-male-lead",
            "cat": "BEHAVIORAL DATA",
            "sample": "ZERO-WAIT MECHANICS",
            "stage": stage_four_step_flow([
                {"step": "PHASE 1", "title": "男性組が先行入店", "metric": "男 > 女", "metric_color": "#8ab4f8", "color": "#8ab4f8", "desc": "店内に男性の空きテーブルが生まれ、女性即案内枠が確保される"},
                {"step": "PHASE 2", "title": "女性組が人数を確認", "metric": "待ち 0分", "highlight": True, "desc": "受付待機を嫌う女性2人組が「すぐ座れる店舗」を選択して来店"},
                {"step": "PHASE 3", "title": "連続着席で女性急増", "metric": "+30〜45分", "desc": "案内スピードの速さが口コミとライブ数値を押し上げさらに流入"},
                {"step": "PHASE 4", "title": "男女バランス均衡へ", "metric": "55 : 45", "desc": "30〜60分後にテーブル稼働が一巡し、活気ある均衡状態へ到達"},
            ]),
            "title": "男性先行の店舗に女性ペアが集まりやすいデータ構造",
            "sub": "「女性は受付で待たない」行動原則から読み解く、男女比の自律回復メカニズム",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>女性待ちゼロ着席</span><span class="legend-item"><span class="dot-blue"></span>男性先行トリガー</span>',
        },
        # 14. Beauty Salon Area Stage
        {
            "base": "eyecatch-beauty-stage",
            "cat": "AREA & WOMEN'S GUIDE",
            "sample": "EARLY EVENING SURGE",
            "stage": stage_dual_curve(
                x_labels=[(96, "18:30"), (264, "19:45"), (434, "21:00"), (588, "22:30")],
                rose_pts=[(96, 146), (264, 68), (434, 84), (588, 138)],
                blue_pts=[(96, 248), (264, 212), (434, 152), (588, 118)],
                callout_left=(264, 68, "19:45 サロン帰り流入山"),
                callout_right=(588, 118, "22:30 仕事帰り男性合流"),
                side_cards=[
                    ("SALON DISTRICT EFFECT", "#e29aa8", "美容室・ネイル密集エリアの19時台", "銀座・恵比寿・梅田茶屋町・天神はサロン帰りの早時間来店が顕著"),
                    ("FOR WOMEN ZERO-WAIT", "#8ab4f8", "女性2人組が快適に過ごす店選び", "女子余り店での受付待機を避け、男性先行店をスマートに選ぶ基準"),
                ],
            ),
            "title": "美容サロン激戦区の早時間帯ピークと女性向け活用術",
            "sub": "ヘア・ネイル帰りの動線が作る19〜20時台の女性先行波と、待ちゼロで楽しむ店舗選び",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>サロン圏女性流入</span><span class="legend-item"><span class="dot-blue"></span>男性来店推移</span>',
        },
        # 15. Club Area Midnight
        {
            "base": "eyecatch-club-midnight",
            "cat": "MIDNIGHT 2-WAVE STUDY",
            "sample": "SHIBUYA / UMEDA / SHINJUKU",
            "stage": stage_dual_curve(
                x_labels=[(96, "20:30"), (258, "23:30"), (426, "01:00"), (588, "02:30")],
                rose_pts=[(96, 84), (258, 182), (426, 92), (588, 114)],
                blue_pts=[(96, 196), (258, 168), (426, 140), (588, 148)],
                callout_left=(96, 84, "第1波 クラブ前0次会", "#e29aa8"),
                callout_right=(426, 92, "第2波 深夜1時の再流入", "#e29aa8"),
                side_cards=[
                    ("WAVE 1 : 20:00 - 23:00", "#e29aa8", "イベント前の乾杯・0次会ニーズ", "メインタイム前にソファ席で食事と会話を楽しみながら過ごす層"),
                    ("WAVE 2 : 00:30 - 03:00", "#8ab4f8", "ダンスクラブ帰りの着席休憩ニーズ", "立ち疲れ・音量疲れから、落ち着いて座って話せるラウンジへ再流入"),
                ],
            ),
            "title": "梅田・渋谷・新宿で深夜0時以降も女性が続く2段階波",
            "sub": "ダンスクラブ隣接エリア特有の「0次会ピーク」と「深夜1時からの第2波」を実測検証",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>女性2段階ピーク波形</span><span class="legend-item"><span class="dot-blue"></span>深夜男性推移</span>',
        },
        # 16. Weekend Strategy
        {
            "base": "eyecatch-weekend",
            "cat": "FRIDAY & SATURDAY",
            "sample": "WEEKEND 3-SLOT MOVE",
            "stage": stage_three_kpi_cards([
                {"kicker": "SLOT 1 ｜ 19:30〜20:30", "heading": "ピーク前の先行着席", "value": "待ち 0", "unit": "分圏", "badge": "最推奨", "highlight": True, "desc": "1軒目ディナー帯の女性先行波に乗り、待機なしで着席", "bar_pct": 88, "bar_color": "#e29aa8"},
                {"kicker": "SLOT 2 ｜ 21:30〜23:00", "heading": "徒歩3分圏ブランド横断", "value": "70", "unit": "%基準", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "1時間待ちの旗艦店を避け、近隣の穴場ブランドへ即移動", "bar_pct": 70, "bar_color": "#8ab4f8"},
                {"kicker": "SLOT 3 ｜ 00:15以降", "heading": "終電後の第2波狙い", "value": "深夜", "unit": "再逆転", "desc": "終電退店が落ち着いた大型店で深夜組とゆったり合流", "bar_pct": 64, "bar_color": "#f4f4f5"},
            ]),
            "title": "金・土の男性1時間待ちを回避する時間帯別ムーブ",
            "sub": "週末ピークの待機列に並ばず着席するための「3つの時間帯戦略」と近隣ハシゴ判断",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>先行着席スイートスポット</span><span class="legend-item"><span class="dot-blue"></span>混雑回避ハシゴ基準</span>',
        },
        # 17. Weekday Strategy (note-eyecatch-weekday)
        {
            "base": "note-eyecatch-weekday",
            "cat": "WEEKDAY STRATEGY",
            "sample": "WED & THU SWEET SPOT",
            "stage": stage_spectrum_rows(
                header_cols=("曜日 × 時間帯（20:00〜22:00）", "男性の着席効率・女性比率"),
                scale_ticks=("低", "やや低", "標準", "良好", "最高"),
                rows=[
                    {"name": "水曜・木曜 20:00〜22:00", "sub": "仕事帰りの女子会・リフレッシュ需要が先行", "left_pct": 18, "width_pct": 76, "bar_color": "#e29aa8", "verdict": "女性比64%・待ち最小", "highlight": True},
                    {"name": "月曜・火曜 20:00〜22:00", "sub": "全体人数は控えめ・落ち着いた会話重視", "left_pct": 14, "width_pct": 54, "bar_color": "#f4f4f5", "verdict": "静かな対話向き"},
                    {"name": "金曜・土曜 21:30〜23:30", "sub": "母数最大だが2次会男性の集中で待機注意", "left_pct": 8, "width_pct": 44, "bar_color": "#8ab4f8", "verdict": "混雑・要70%選別", "verdict_color": "#9ca3af"},
                ],
            ),
            "title": "週末より「水曜・木曜20時〜22時」が勝ちやすい理由",
            "sub": "2次会男性の競合が少なく、仕事帰りの女性ペアと落ち着いて話せる平日ゴールデンタイム",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>水・木ゴールデン帯</span><span class="legend-item"><span class="dot-blue"></span>週末ピーク混雑帯</span>',
        },
        # 18. 3 Major Brand Comparison (guide-brand-comparison)
        {
            "base": "guide-brand-comparison",
            "cat": "BRAND COMPARISON",
            "sample": "ORIENTAL LOUNGE / JIS / ag",
            "stage": stage_three_kpi_cards([
                {"kicker": "全国26店舗 ｜ 王道ラグジュアリー", "heading": "オリエンタルラウンジ", "value": "24-33", "unit": "歳中心", "badge": "社会人主流", "highlight": True, "desc": "落ち着いたホテル調ソファ空間。2人組でのじっくり対話に最適", "bar_pct": 82, "bar_color": "#e29aa8"},
                {"kicker": "全国18店舗 ｜ 大型・高回転バー", "heading": "JIS（ジス）", "value": "1人〜", "unit": "入店可", "val_color": "#f4f4f5", "desc": "札幌・新宿・梅田・松山・福岡など主要都市で圧倒的集客と機動性", "bar_pct": 78, "bar_color": "#f4f4f5"},
                {"kicker": "全国9店舗 ｜ 若年層カジュアル", "heading": "ag（アグ）", "value": "20-25", "unit": "歳中心", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "Z世代・学生〜若手社会人が集まる明るくテンポの良い姉妹ブランド", "bar_pct": 68, "bar_color": "#8ab4f8"},
            ]),
            "title": "オリエンタルラウンジ・JIS・ag 3大ブランド徹底比較",
            "sub": "年齢層・料金システム・1人入店可否・空間の雰囲気で選ぶ失敗しないブランド診断",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>大人ラウンジ系</span><span class="legend-item"><span class="dot-blue"></span>Z世代・カジュアル系</span>',
        },
        # 19. Shinjuku 3 Stores
        {
            "base": "eyecatch-shinjuku",
            "cat": "SHINJUKU AREA GUIDE",
            "sample": "3 FLAGSHIP STORES",
            "stage": stage_spectrum_rows(
                header_cols=("新宿エリア主要3店舗", "客層・強みとなる使い方"),
                scale_ticks=("19時", "21時", "23時", "01時", "03時"),
                rows=[
                    {"name": "オリエンタルラウンジ 新宿", "sub": "歌舞伎町・靖国通り沿い ｜ 王道フラッグシップ", "left_pct": 8, "width_pct": 64, "bar_color": "#e29aa8", "verdict": "20〜24時 社会人2人組", "highlight": True},
                    {"name": "JIS 新宿東口", "sub": "歌舞伎町エリア ｜ 1人入店OK・深夜帯も高稼働", "left_pct": 22, "width_pct": 70, "bar_color": "#f4f4f5", "verdict": "ソロ可・深夜第2波に強い"},
                    {"name": "JIS 新宿西口", "sub": "西口オフィス・ヨドバシ裏 ｜ 落ち着いた穴場導線", "left_pct": 12, "width_pct": 52, "bar_color": "#8ab4f8", "verdict": "東口満席時の徒歩移動先"},
                ],
            ),
            "title": "新宿エリア主要3店舗の特徴と時間帯別の使い分け",
            "sub": "オリエンタルラウンジ新宿・JIS東口・JIS西口の客層差と、徒歩圏ハシゴの最短ルート",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>東口・歌舞伎町主軸</span><span class="legend-item"><span class="dot-blue"></span>西口オフィス穴場軸</span>',
        },
        # 20. Solo vs Pair
        {
            "base": "eyecatch-solo-pair",
            "cat": "SOLO VS PAIR STRATEGY",
            "sample": "PARTY SIZE COMPARISON",
            "stage": stage_three_kpi_cards([
                {"kicker": "1人入店（ソロ参戦）", "heading": "意思決定と移動が最速", "value": "JIS", "unit": "等で対応", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "思い立った瞬間に動け、40分見切りや店舗移動も自分のペースで完結", "bar_pct": 72, "bar_color": "#8ab4f8"},
                {"kicker": "友人2人組（ペア参戦）", "heading": "会話の安定感と全店対応", "value": "全63", "unit": "店舗対応", "badge": "王道スタイル", "highlight": True, "desc": "2対2で沈黙が生まれにくく、オリエンタルラウンジ含む全店に入店可能", "bar_pct": 92, "bar_color": "#e29aa8"},
                {"kicker": "3人以上のグループ", "heading": "大型ボックス席待ち注意", "value": "要確認", "unit": "", "desc": "混雑時は案内テーブルが限られるため、2人組に分かれる方が着席が早い", "bar_pct": 38, "bar_color": "#f4f4f5"},
            ]),
            "title": "「1人入店」と「友人ペア」の立ち回り・店舗選び比較",
            "sub": "ソロ入店対応ブランドの選び方と、1人・2人それぞれの会話の進め方とメリット整理",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>2人組（ペア）優位点</span><span class="legend-item"><span class="dot-blue"></span>1人（ソロ）機動力</span>',
        },
        # 21. Second Venue Invitation
        {
            "base": "eyecatch-second-venue",
            "cat": "SECOND VENUE GUIDE",
            "sample": "TIMING & VENUE FIT",
            "stage": stage_four_step_flow([
                {"step": "00 - 15 MIN", "title": "共通点と温度感の確認", "metric": "安心感", "desc": "まずは相手の好きな食事・お酒のペースを自然に把握する"},
                {"step": "25 - 40 MIN", "title": "盛り上がりのピークで提案", "metric": "22:30頃", "highlight": True, "desc": "話題が尽きる前・終電を気にする前に「甘いもの／静かなバー」を提案"},
                {"step": "WALK <= 4 MIN", "title": "徒歩4分以内の店を選ぶ", "metric": "移動負荷 0", "metric_color": "#8ab4f8", "color": "#8ab4f8", "desc": "夜風でテンションが冷めない駅近カフェ・バー・夜パフェが鉄則"},
                {"step": "23:15 LIMIT", "title": "終電時間を先に気遣う", "metric": "信頼獲得", "desc": "帰りの路線を確認した上で45分だけ軽く飲む提案が最も通る"},
            ]),
            "title": "相席から2軒目へ自然に誘うタイミングと店選びの基準",
            "sub": "終電前の22時半ルールと、断られにくい「徒歩4分圏・カフェ＆バー」へのスマートな導線",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>提案ゴールデンタイム</span><span class="legend-item"><span class="dot-blue"></span>徒歩4分圏ルール</span>',
        },
        # 22. First 10 Minutes
        {
            "base": "eyecatch-first-10min",
            "cat": "FIRST 10 MINUTES",
            "sample": "IMPRESSION BREAKDOWN",
            "stage": stage_three_kpi_cards([
                {"kicker": "0〜1分 ｜ 視覚と清潔感", "heading": "口元・肌・姿勢の第一印象", "value": "3", "unit": "秒で判定", "badge": "最優先", "highlight": True, "desc": "間接照明の70cm席では、会話内容より先に清潔感と安心感が見られる", "bar_pct": 88, "bar_color": "#e29aa8"},
                {"kicker": "1〜5分 ｜ 乾杯とアイスブレイク", "heading": "相手ペア同士の空気を尊重", "value": "7 : 3", "unit": "の傾聴比", "val_color": "#f4f4f5", "desc": "一問一答の面接トークを避け、相手2人が話しやすい共通テーマを振る", "bar_pct": 74, "bar_color": "#f4f4f5"},
                {"kicker": "5〜10分 ｜ 後半の方向性決定", "heading": "食・休日の自然な共感軸", "value": "+30", "unit": "分の熱量", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "最初の10分で「居心地が良い席」と感じてもらえれば2軒目率も急上昇", "bar_pct": 80, "bar_color": "#8ab4f8"},
            ]),
            "title": "着席して最初の10分で好印象をつくる会話と身だしなみ",
            "sub": "面接のような質問攻めを卒業し、「もっと話したい」と自然に思われる導入プロトコル",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>清潔感・第一印象</span><span class="legend-item"><span class="dot-blue"></span>会話テンポ・傾聴</span>',
        },
        # 23. Beginner Complete Guide (note-eyecatch)
        {
            "base": "note-eyecatch",
            "cat": "COMPLETE BEGINNER GUIDE",
            "sample": "63 STORES SUPPORTED",
            "stage": stage_four_step_flow([
                {"step": "STEP 1", "title": "身分証原本と清潔感", "metric": "必須準備", "desc": "顔写真付き公的身分証の原本と、襟付き・きれいめ私服を整える"},
                {"step": "STEP 2", "title": "ライブ男女比で店選び", "metric": "55%以上", "highlight": True, "desc": "来店前に相席ナビで現在人数と60分後予測を見て待機を回避"},
                {"step": "STEP 3", "title": "料金とシステム理解", "metric": "女性 0円", "metric_color": "#8ab4f8", "color": "#8ab4f8", "desc": "女性は完全無料、男性は10分単位の明朗会計（早割・イベントあり）"},
                {"step": "STEP 4", "title": "着席・席替え・2軒目", "metric": "35分基準", "desc": "最初の10分で空気をつくり、合わない場合はスマートに席替え相談"},
            ]),
            "title": "相席ラウンジ初心者向け完全ガイド｜料金・服装・店選び",
            "sub": "全国63店舗対応 ― 初めてでも迷わない入店準備からリアルタイム男女比の見方まで網羅",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>事前チェック項目</span><span class="legend-item"><span class="dot-blue"></span>料金・システム基本</span>',
        },
        # 24. Brand Page / Official X Header OGP (x-header)
        {
            "base": "x-header",
            "cat": "LIVE DATA PLATFORM",
            "sample": "63 STORES REAL-TIME",
            "stage": stage_three_kpi_cards([
                {"kicker": "REAL-TIME HEADCOUNT", "heading": "全国63店舗 毎分更新", "value": "5", "unit": "ブランド横断", "badge": "LIVE", "highlight": True, "desc": "オリエンタルラウンジ・JIS・ag・ヤタコイ・ASOBIBARの現在人数を一括表示", "bar_pct": 92, "bar_color": "#e29aa8"},
                {"kicker": "60-MIN AI FORECAST", "heading": "60分後の男女比推移予測", "value": "98", "unit": "万件学習", "val_color": "#f4f4f5", "desc": "到着時に男余りへ逆転するリスクを過去スナップショットから事前推論", "bar_pct": 84, "bar_color": "#f4f4f5"},
                {"kicker": "WAIT TIME & PRICE", "heading": "待ち時間＆実質料金比較", "value": "3", "unit": "秒で判断", "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": "同エリアの徒歩圏店舗を男女比・推定待ち時間・イベント割引で即比較", "bar_pct": 80, "bar_color": "#8ab4f8"},
            ]),
            "title": "全国63店舗のリアルタイム男女比・60分後AI予測メディア",
            "sub": "相席ナビ（AISEKI NAVI）― いま入るべき1店舗が3秒でわかるライブ人数ランキング＆統計白書",
            "legend": '<span class="legend-item"><span class="dot-rose"></span>リアルタイム男女人数</span><span class="legend-item"><span class="dot-blue"></span>AI推移予測・料金比較</span>',
        },
    ]


THUMBNAIL_DERIVATIVES = {
    "guide-brand-comparison": "thumb-guide-brand-comparison",
    "note-eyecatch": "thumb-manual",
    "note-eyecatch-weekday": "thumb-weekday",
}


def build_card_html(cat: str, sample: str, stage: str, title: str, sub: str, legend: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>{COMMON_CARD_CSS}</style>
</head>
<body>
  <div class="canvas">
    <div class="top-bar">
      <div class="brand-pill">
        {MINI_MARK_SVG}
        <span class="brand-kicker">AISEKI NAVI</span>
        <span class="brand-sep">/</span>
        <span class="brand-cat">{cat}</span>
      </div>
      <div class="meta-sample">{sample}</div>
    </div>
    <div class="viz-stage">{stage}</div>
    <div class="bottom-bar">
      <div>
        <div class="title-main">{title}</div>
        <div class="title-sub">{sub}</div>
      </div>
      <div class="legend-group">{legend}</div>
    </div>
  </div>
</body>
</html>"""


def save_eyecatch_pair(pil_rgb_1280: Image.Image, base_name: str, out_dir: str):
    jpg_path = os.path.join(out_dir, f"{base_name}.jpg")
    webp_path = os.path.join(out_dir, f"{base_name}.webp")
    pil_rgb_1280.save(jpg_path, format="JPEG", quality=88, optimize=True)
    pil_rgb_1280.save(webp_path, format="WEBP", quality=84, method=6)
    if base_name in THUMBNAIL_DERIVATIVES:
        t_base = THUMBNAIL_DERIVATIVES[base_name]
        im_thumb = pil_rgb_1280.resize((640, 360), Image.Resampling.LANCZOS)
        im_thumb.save(os.path.join(out_dir, f"{t_base}.jpg"), format="JPEG", quality=86, optimize=True)
        im_thumb.save(os.path.join(out_dir, f"{t_base}.webp"), format="WEBP", quality=82, method=6)


def render_icons(browser, out_dir: str):
    page = browser.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=2)
    page.set_content(
        f"<!DOCTYPE html><html><body style='margin:0;padding:0;width:512px;height:512px;overflow:hidden;'>{ICON_SVG_512}</body></html>"
    )
    png_bytes = page.screenshot(type="png")
    page.close()
    import io
    im_1024 = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im_512 = im_1024.resize((512, 512), Image.Resampling.LANCZOS)
    im_192 = im_1024.resize((192, 192), Image.Resampling.LANCZOS)
    im_512.save(os.path.join(out_dir, "icon.png"), format="PNG", optimize=True)
    im_512.save(os.path.join(out_dir, "icon-512.png"), format="PNG", optimize=True)
    im_192.save(os.path.join(out_dir, "icon-192.png"), format="PNG", optimize=True)
    print("Saved icons: icon.png (512), icon-512.png (512), icon-192.png (192)")


def render_all(out_dir: str, contact_sheet_path: str = None):
    import io
    specs = build_all_asset_specs()
    rendered_thumbs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        render_icons(browser, out_dir)
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=2)
        for sp in specs:
            html = build_card_html(
                cat=sp["cat"],
                sample=sp["sample"],
                stage=sp["stage"],
                title=sp["title"],
                sub=sp["sub"],
                legend=sp["legend"],
            )
            page.set_content(html)
            raw_bytes = page.screenshot(type="png")
            im_1280 = Image.open(io.BytesIO(raw_bytes)).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
            save_eyecatch_pair(im_1280, sp["base"], out_dir)
            rendered_thumbs.append(im_1280.resize((420, 236), Image.Resampling.LANCZOS))
            print(f"Saved: {sp['base']}.jpg / .webp")
        page.close()
        browser.close()

    if contact_sheet_path and rendered_thumbs:
        cols = 4
        rows = (len(rendered_thumbs) + cols - 1) // cols
        pad = 12
        sheet = Image.new("RGB", (cols * 420 + (cols + 1) * pad, rows * 236 + (rows + 1) * pad), (24, 24, 28))
        for idx, th in enumerate(rendered_thumbs):
            r, c = divmod(idx, cols)
            sheet.paste(th, (pad + c * (420 + pad), pad + r * (236 + pad)))
        os.makedirs(os.path.dirname(contact_sheet_path), exist_ok=True)
        sheet.save(contact_sheet_path, format="JPEG", quality=88)
        print("Saved contact sheet:", contact_sheet_path)


def render_single_cli(args):
    import io
    stage = stage_three_kpi_cards([
        {"kicker": args.k1_kicker, "heading": args.k1_head, "value": args.k1_val, "unit": args.k1_unit, "desc": args.k1_desc, "bar_pct": 62, "bar_color": "#f4f4f5"},
        {"kicker": args.k2_kicker, "heading": args.k2_head, "value": args.k2_val, "unit": args.k2_unit, "badge": "注目指標", "highlight": True, "desc": args.k2_desc, "bar_pct": 80, "bar_color": "#e29aa8"},
        {"kicker": args.k3_kicker, "heading": args.k3_head, "value": args.k3_val, "unit": args.k3_unit, "val_color": "#8ab4f8", "color": "#8ab4f8", "desc": args.k3_desc, "bar_pct": 70, "bar_color": "#8ab4f8"},
    ])
    html = build_card_html(
        cat=html_mod.escape(args.cat),
        sample=html_mod.escape(args.sample),
        stage=stage,
        title=html_mod.escape(args.title),
        sub=html_mod.escape(args.sub),
        legend='<span class="legend-item"><span class="dot-rose"></span>主要検証指標</span><span class="legend-item"><span class="dot-blue"></span>比較指標</span>',
    )
    base_name = args.base if args.base else f"eyecatch-{args.slug}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=2)
        page.set_content(html)
        raw_bytes = page.screenshot(type="png")
        page.close()
        browser.close()
    im_1280 = Image.open(io.BytesIO(raw_bytes)).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
    save_eyecatch_pair(im_1280, base_name, STATIC_DIR)
    print(f"Rendered single eyecatch: {base_name}.jpg / .webp")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render clean-minimal brand icons and editorial data eyecatches.")
    parser.add_argument("--all", action="store_true", help="Render all icons + all 24 editorial eyecatches")
    parser.add_argument("--contact-sheet", default="", help="Optional path to save a visual QA contact sheet")
    parser.add_argument("--slug", default="", help="Slug for single article eyecatch")
    parser.add_argument("--base", default="", help="Explicit base filename without extension")
    parser.add_argument("--cat", default="DATA EDITORIAL")
    parser.add_argument("--sample", default="REAL-TIME LOG STUDY")
    parser.add_argument("--title", default="")
    parser.add_argument("--sub", default="")
    parser.add_argument("--k1-kicker", default="POINT 01")
    parser.add_argument("--k1-head", default="早時間帯の傾向")
    parser.add_argument("--k1-val", default="20:00")
    parser.add_argument("--k1-unit", default="〜")
    parser.add_argument("--k1-desc", default="女性比率が高まりやすい先行時間帯")
    parser.add_argument("--k2-kicker", default="POINT 02")
    parser.add_argument("--k2-head", default="最適スイートスポット")
    parser.add_argument("--k2-val", default="65")
    parser.add_argument("--k2-unit", default="%+")
    parser.add_argument("--k2-desc", default="待機を避けて着席しやすい目安ライン")
    parser.add_argument("--k3-kicker", default="POINT 03")
    parser.add_argument("--k3-head", default="ピーク・深夜の推移")
    parser.add_argument("--k3-val", default="60")
    parser.add_argument("--k3-unit", default="分予測")
    parser.add_argument("--k3-desc", default="到着時の男女比変化を事前にチェック")
    args = parser.parse_args()
    if args.slug or args.base:
        render_single_cli(args)
    else:
        render_all(STATIC_DIR, contact_sheet_path=args.contact_sheet or None)
