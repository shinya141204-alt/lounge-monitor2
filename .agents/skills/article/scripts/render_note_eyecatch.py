#!/usr/bin/env python3
"""
render_note_eyecatch.py - Render note.com official size (1280x670) eyecatch images
for `docs/note_stock/01_last_train_human_drama.md` in Clean-Minimal editorial style.
"""

import io
import os
from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
OUT_DIR = os.path.join(REPO_ROOT, "docs", "note_stock", "images")

MINI_MARK_SVG = """
<svg class="brand-mark" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <circle cx="25" cy="33" r="13.5" fill="none" stroke="#f4f4f5" stroke-width="4.5"/>
  <circle cx="39" cy="33" r="13.5" fill="none" stroke="#e29aa8" stroke-width="4.5"/>
  <circle cx="51" cy="15" r="3.2" fill="#f4f4f5"/>
</svg>
"""

PLAN_A_HTML = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  width: 1280px;
  height: 670px;
  overflow: hidden;
  background: #111114;
  color: #f4f4f5;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Hiragino Sans", "Hiragino Kaku Gothic ProN", sans-serif;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "palt";
}}
.canvas {{
  position: relative;
  width: 1280px;
  height: 670px;
  padding: 50px 64px;
  display: grid;
  grid-template-columns: 548px 1fr;
  gap: 44px;
  align-items: stretch;
  background:
    radial-gradient(circle at 18% 20%, rgba(226,154,168,0.055) 0%, rgba(17,17,20,0) 52%),
    radial-gradient(circle at 86% 78%, rgba(138,180,248,0.045) 0%, rgba(17,17,20,0) 50%),
    linear-gradient(180deg, #151519 0%, #101013 100%);
}}
.canvas::before {{
  content: "";
  position: absolute;
  inset: 22px;
  border: 1px solid rgba(255,255,255,0.07);
  border-radius: 20px;
  pointer-events: none;
}}
.left-col {{
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 6px 0 4px 8px;
  z-index: 2;
}}
.brand-pill {{
  display: inline-flex;
  align-items: center;
  gap: 11px;
}}
.brand-mark {{ width: 26px; height: 26px; }}
.brand-kicker {{
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.14em;
  color: #a1a1aa;
}}
.brand-sep {{ color: #3f3f46; }}
.brand-cat {{
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.1em;
  color: #e29aa8;
}}
.hero-block {{
  margin: 8px 0 10px;
}}
.clock-row {{
  display: flex;
  align-items: baseline;
  gap: 16px;
  margin-bottom: 16px;
}}
.clock-num {{
  font-size: 86px;
  font-weight: 700;
  letter-spacing: -0.035em;
  line-height: 0.96;
  color: #f4f4f5;
  font-variant-numeric: tabular-nums;
}}
.clock-tag {{
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 13px;
  border-radius: 999px;
  background: rgba(226,154,168,0.12);
  border: 1px solid rgba(226,154,168,0.34);
  font-size: 14px;
  font-weight: 600;
  color: #f4f4f5;
  transform: translateY(-10px);
}}
.clock-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #e29aa8;
}}
.headline {{
  font-size: 40px;
  font-weight: 700;
  line-height: 1.34;
  letter-spacing: -0.015em;
  color: #f4f4f5;
}}
.headline .accent {{
  color: #e29aa8;
}}
.subcopy {{
  margin-top: 18px;
  font-size: 17.5px;
  line-height: 1.65;
  color: #a1a1aa;
  font-weight: 400;
}}
.author-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 18px;
  border-top: 1px solid rgba(255,255,255,0.08);
  font-size: 14px;
  color: #8e8e98;
}}
.author-name {{
  color: #d4d4d8;
  font-weight: 600;
}}
.right-card {{
  z-index: 2;
  border-radius: 18px;
  background: rgba(255,255,255,0.025);
  border: 1px solid rgba(255,255,255,0.085);
  padding: 26px 28px 24px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}}
.card-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
}}
.card-title {{
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: #d4d4d8;
}}
.sample-badge {{
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.05em;
  color: #a1a1aa;
  font-variant-numeric: tabular-nums;
  padding: 5px 12px;
  border-radius: 999px;
  background: rgba(255,255,255,0.035);
  border: 1px solid rgba(255,255,255,0.08);
}}
.metrics-strip {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-top: 6px;
}}
.m-box {{
  padding: 13px 14px;
  border-radius: 12px;
  background: rgba(17,17,20,0.65);
  border: 1px solid rgba(255,255,255,0.065);
}}
.m-box.hi {{
  background: rgba(226,154,168,0.075);
  border-color: rgba(226,154,168,0.36);
}}
.m-time {{
  font-size: 12.5px;
  font-weight: 600;
  color: #9ca3af;
  font-variant-numeric: tabular-nums;
}}
.m-box.hi .m-time {{ color: #e29aa8; }}
.m-val {{
  font-size: 25px;
  font-weight: 700;
  color: #f4f4f5;
  margin-top: 3px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}}
.m-val.blue {{ color: #8ab4f8; }}
.m-desc {{
  font-size: 12px;
  color: #8e8e98;
  margin-top: 2px;
  white-space: nowrap;
}}
</style>
</head>
<body>
  <div class="canvas">
    <div class="left-col">
      <div class="brand-pill">
        {MINI_MARK_SVG}
        <span class="brand-kicker">AISEKI NAVI</span>
        <span class="brand-sep">/</span>
        <span class="brand-cat">ESSAY &amp; DATA</span>
      </div>

      <div class="hero-block">
        <div class="clock-row">
          <div class="clock-num">23:20</div>
          <div class="clock-tag"><span class="clock-dot"></span>スマホが裏返る時刻</div>
        </div>
        <div class="headline">
          相席ラウンジの空気は、<br>毎晩<span class="accent">一斉に変わる。</span>
        </div>
        <div class="subcopy">
          9.6万件の人数データを追って見えた<br>「終電前の40分間」と、0時15分の第2波
        </div>
      </div>

      <div class="author-row">
        <span class="author-name">くわもん｜相席ナビ編集長</span>
        <span>全国63店舗 リアルタイム観測</span>
      </div>
    </div>

    <div class="right-card">
      <div class="card-head">
        <span class="card-title">店内女性人数の平均波形（22:30 — 00:45）</span>
        <span class="sample-badge">LOG n = 96,607</span>
      </div>

      <svg viewBox="0 0 540 295" width="100%" height="295" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="roseFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#e29aa8" stop-opacity="0.22"/>
            <stop offset="100%" stop-color="#e29aa8" stop-opacity="0.0"/>
          </linearGradient>
          <linearGradient id="cliffBand" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#e29aa8" stop-opacity="0.11"/>
            <stop offset="100%" stop-color="#e29aa8" stop-opacity="0.02"/>
          </linearGradient>
        </defs>

        <!-- Horizontal reference lines -->
        <line x1="42" y1="48" x2="512" y2="48" stroke="rgba(255,255,255,0.07)" stroke-width="1"/>
        <line x1="42" y1="145" x2="512" y2="145" stroke="rgba(255,255,255,0.09)" stroke-width="1" stroke-dasharray="4 6"/>
        <line x1="42" y1="246" x2="512" y2="246" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>

        <!-- 23:10 - 23:30 Cliff highlight band -->
        <rect x="148" y="34" width="108" height="212" rx="6" fill="url(#cliffBand)"/>
        <line x1="202" y1="34" x2="202" y2="246" stroke="rgba(226,154,168,0.48)" stroke-width="1.5" stroke-dasharray="3 4"/>

        <!-- Area & Main Curve (31.3 -> 26.5 -> 22.4 -> 25.6) -->
        <path d="M 56 68 C 102 68, 122 70, 148 74 C 180 80, 222 152, 256 162 C 296 175, 326 202, 368 202 C 414 202, 444 154, 496 152 L 496 246 L 56 246 Z" fill="url(#roseFill)"/>
        <path d="M 56 68 C 102 68, 122 70, 148 74 C 180 80, 222 152, 256 162 C 296 175, 326 202, 368 202 C 414 202, 444 154, 496 152" fill="none" stroke="#e29aa8" stroke-width="4.2" stroke-linecap="round"/>

        <!-- Male late arrival curve (crossing up at 23:30-23:50) -->
        <path d="M 56 186 C 118 180, 176 168, 256 156 C 312 148, 340 144, 368 146 C 418 150, 456 164, 496 170" fill="none" stroke="#8ab4f8" stroke-width="2.8" stroke-dasharray="6 5" opacity="0.88"/>

        <!-- Key Data Points -->
        <circle cx="148" cy="74" r="6" fill="#121215" stroke="#f4f4f5" stroke-width="3"/>
        <circle cx="256" cy="162" r="6.5" fill="#121215" stroke="#e29aa8" stroke-width="3.5"/>
        <circle cx="368" cy="202" r="6" fill="#121215" stroke="#8ab4f8" stroke-width="3"/>
        <circle cx="496" cy="152" r="6.5" fill="#121215" stroke="#e29aa8" stroke-width="3.5"/>

        <!-- Callout Pills -->
        <rect x="66" y="26" width="150" height="30" rx="7" fill="#1b1b20" stroke="rgba(255,255,255,0.16)"/>
        <text x="141" y="46" text-anchor="middle" fill="#f4f4f5" font-size="13" font-weight="600">23:10 平均 31.3名</text>

        <rect x="210" y="84" width="164" height="32" rx="8" fill="#23191d" stroke="rgba(226,154,168,0.5)"/>
        <text x="292" y="105" text-anchor="middle" fill="#f4f4f5" font-size="13.5" font-weight="700">20分で -16.0% 急減</text>

        <rect x="292" y="214" width="152" height="27" rx="7" fill="#181c24" stroke="rgba(138,180,248,0.38)"/>
        <text x="368" y="232" text-anchor="middle" fill="#cfe0fc" font-size="12.5" font-weight="600">23:50 女性比底 50.2%</text>

        <rect x="394" y="104" width="124" height="30" rx="7" fill="#1b1b20" stroke="rgba(226,154,168,0.4)"/>
        <text x="456" y="124" text-anchor="middle" fill="#e29aa8" font-size="13" font-weight="700">第2波 +14.3%</text>

        <!-- X-axis labels -->
        <text x="56" y="268" text-anchor="middle" fill="#8e8e98" font-size="13.5" font-weight="600">22:30</text>
        <text x="148" y="268" text-anchor="middle" fill="#d4d4d8" font-size="13.5" font-weight="600">23:10</text>
        <text x="256" y="268" text-anchor="middle" fill="#e29aa8" font-size="14" font-weight="700">23:30</text>
        <text x="368" y="268" text-anchor="middle" fill="#8ab4f8" font-size="13.5" font-weight="600">23:50</text>
        <text x="496" y="268" text-anchor="middle" fill="#d4d4d8" font-size="13.5" font-weight="600">00:30</text>

        <!-- Legend inside SVG bottom right -->
        <circle cx="72" cy="287" r="4.5" fill="#e29aa8"/>
        <text x="83" y="291" fill="#9ca3af" font-size="12">店内女性数（実測平均）</text>
        <line x1="226" y1="287" x2="246" y2="287" stroke="#8ab4f8" stroke-width="2.5" stroke-dasharray="4 3"/>
        <text x="253" y="291" fill="#9ca3af" font-size="12">1次会後の男性流入波</text>
      </svg>

      <div class="metrics-strip">
        <div class="m-box hi">
          <div class="m-time">23:10 → 23:30</div>
          <div class="m-val">-16.0%</div>
          <div class="m-desc">31.3名→26.5名の退店崖</div>
        </div>
        <div class="m-box">
          <div class="m-time">23時台 すれ違い</div>
          <div class="m-val blue">34.4%</div>
          <div class="m-desc">男性過多店の割合（20時3.5%）</div>
        </div>
        <div class="m-box">
          <div class="m-time">00:10 → 00:30</div>
          <div class="m-val">+14.3%</div>
          <div class="m-desc">終電後の「第2波」再浮上</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""

PLAN_B_HTML = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  width: 1280px;
  height: 670px;
  overflow: hidden;
  background: #121215;
  color: #f4f4f5;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Hiragino Sans", "Hiragino Kaku Gothic ProN", sans-serif;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "palt";
}}
.canvas {{
  position: relative;
  width: 1280px;
  height: 670px;
  padding: 46px 68px 44px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background:
    radial-gradient(circle at 82% 18%, rgba(255,255,255,0.032) 0%, rgba(18,18,21,0) 54%),
    linear-gradient(180deg, #151519 0%, #111114 100%);
}}
.canvas::before {{
  content: "";
  position: absolute;
  inset: 22px;
  border: 1px solid rgba(255,255,255,0.068);
  border-radius: 20px;
  pointer-events: none;
}}
.top-bar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  z-index: 2;
}}
.brand-pill {{
  display: inline-flex;
  align-items: center;
  gap: 12px;
}}
.brand-mark {{ width: 27px; height: 27px; }}
.brand-kicker {{
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: 0.14em;
  color: #a1a1aa;
}}
.brand-sep {{ color: #3f3f46; }}
.brand-cat {{
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: #d4d4d8;
}}
.meta-sample {{
  font-size: 14.5px;
  font-weight: 500;
  letter-spacing: 0.05em;
  color: #9ca3af;
  font-variant-numeric: tabular-nums;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.085);
  background: rgba(255,255,255,0.025);
}}
.cards-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 22px;
  margin: 22px 0;
  flex: 1;
  align-items: stretch;
  z-index: 2;
}}
.kpi-card {{
  padding: 26px 26px 24px;
  border-radius: 16px;
  background: rgba(255,255,255,0.025);
  border: 1px solid rgba(255,255,255,0.08);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}}
.kpi-card.hi {{
  background: rgba(226,154,168,0.065);
  border: 1.5px solid rgba(226,154,168,0.42);
}}
.kicker-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
}}
.kicker {{
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.05em;
  color: #a1a1aa;
  font-variant-numeric: tabular-nums;
}}
.kpi-card.hi .kicker {{ color: #e29aa8; }}
.badge {{
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(226,154,168,0.18);
  color: #f4f4f5;
  font-size: 12px;
  font-weight: 700;
}}
.head {{
  font-size: 20.5px;
  font-weight: 700;
  color: #f4f4f5;
  margin-top: 8px;
}}
.val-block {{ margin: 16px 0; }}
.val {{
  font-size: 48px;
  font-weight: 700;
  letter-spacing: -0.025em;
  font-variant-numeric: tabular-nums;
  color: #f4f4f5;
}}
.val.rose {{ color: #e29aa8; }}
.val.blue {{ color: #8ab4f8; }}
.unit {{ font-size: 21px; margin-left: 3px; font-weight: 600; }}
.desc {{
  font-size: 14.5px;
  color: #a1a1aa;
  line-height: 1.5;
  margin-top: 6px;
}}
.bar-bg {{
  height: 7px;
  border-radius: 999px;
  background: rgba(255,255,255,0.07);
  overflow: hidden;
}}
.bar-fg {{ height: 100%; border-radius: 999px; }}
.bottom-bar {{
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding-top: 18px;
  border-top: 1px solid rgba(255,255,255,0.075);
  z-index: 2;
}}
.title-main {{
  font-size: 31px;
  font-weight: 700;
  letter-spacing: -0.015em;
  color: #f4f4f5;
  line-height: 1.25;
}}
.title-sub {{
  margin-top: 7px;
  font-size: 16.5px;
  color: #9ca3af;
}}
.author-pill {{
  font-size: 14px;
  color: #a1a1aa;
  white-space: nowrap;
}}
</style>
</head>
<body>
  <div class="canvas">
    <div class="top-bar">
      <div class="brand-pill">
        {MINI_MARK_SVG}
        <span class="brand-kicker">AISEKI NAVI</span>
        <span class="brand-sep">/</span>
        <span class="brand-cat">MIDNIGHT HUMAN OBSERVATION</span>
      </div>
      <div class="meta-sample">63 STORES ｜ LOG n = 96,607</div>
    </div>

    <div class="cards-grid">
      <div class="kpi-card hi">
        <div>
          <div class="kicker-row">
            <span class="kicker">SCENE 01 ｜ 23:10 → 23:30</span>
            <span class="badge">終電前の20分間</span>
          </div>
          <div class="head">スマホが裏返る退店の崖</div>
        </div>
        <div class="val-block">
          <div class="val rose">-16.0<span class="unit">%</span></div>
          <div class="desc">全国平均 31.3名 → 26.5名へ一斉減少。<br>乗換案内の逆算線が引く現実の境界</div>
        </div>
        <div class="bar-bg"><div class="bar-fg" style="width: 78%; background: #e29aa8;"></div></div>
      </div>

      <div class="kpi-card">
        <div>
          <div class="kicker-row">
            <span class="kicker" style="color:#8ab4f8;">SCENE 02 ｜ 23:50 BOTTOM</span>
          </div>
          <div class="head">エレベーター前のすれ違い</div>
        </div>
        <div class="val-block">
          <div class="val blue">34.4<span class="unit">%</span></div>
          <div class="desc">男性過多店の割合（20時台は 3.5%）。<br>23:50に女性比率 50.2% で1日の底へ</div>
        </div>
        <div class="bar-bg"><div class="bar-fg" style="width: 56%; background: #8ab4f8;"></div></div>
      </div>

      <div class="kpi-card">
        <div>
          <div class="kicker-row">
            <span class="kicker">SCENE 03 ｜ 00:10 → 00:30</span>
          </div>
          <div class="head">終電が去った後の「第2波」</div>
        </div>
        <div class="val-block">
          <div class="val">+14.3<span class="unit">%</span></div>
          <div class="desc">平均 22.4名 → 25.6名へ再び上向く。<br>肩の力が抜けた深夜組が始まる時間</div>
        </div>
        <div class="bar-bg"><div class="bar-fg" style="width: 66%; background: #f4f4f5;"></div></div>
      </div>
    </div>

    <div class="bottom-bar">
      <div>
        <div class="title-main">23時20分、相席ラウンジの空気は一斉に変わる。</div>
        <div class="title-sub">9.6万件の人数データを追って見えた「終電前の40分間」と深夜の人間模様</div>
      </div>
      <div class="author-pill">くわもん｜相席ナビ編集長</div>
    </div>
  </div>
</body>
</html>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    targets = [
        ("note_01_eyecatch_plan_a", PLAN_A_HTML),
        ("note_01_eyecatch_plan_b", PLAN_B_HTML),
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 670}, device_scale_factor=2)
        for name, html in targets:
            page.set_content(html)
            raw = page.screenshot(type="png")
            im = Image.open(io.BytesIO(raw)).convert("RGB").resize((1280, 670), Image.Resampling.LANCZOS)
            png_path = os.path.join(OUT_DIR, f"{name}.png")
            jpg_path = os.path.join(OUT_DIR, f"{name}.jpg")
            im.save(png_path, format="PNG", optimize=True)
            im.save(jpg_path, format="JPEG", quality=92, optimize=True)
            print(f"Saved: {png_path} & {jpg_path}")
        page.close()
        browser.close()


if __name__ == "__main__":
    main()
