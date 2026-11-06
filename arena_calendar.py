# -*- coding: utf-8 -*-
"""
arena_calendar.py - カレンダー特異日分類・10大ドーム/アリーナ公式公開予定取得・主要路線遅延コンテキスト

【役割】
1. カレンダー特異日タグ付け (`classify_day_context`):
   - 祝日・祝前日・連休位置（初日/中日/最終日）・給料日窓・賞与窓・季節イベント（忘年会ピーク/クリスマス/仕事納め/年末年始/歓送迎会等）を外部API通信ゼロで決定論的に算出。
2. 全国主要ドーム・アリーナの公式公開スケジュール取得 (`get_arena_events_for_date` / `refresh_arena_schedules`):
   - 公式サイトの公開カレンダーページのみを `robots.txt` 準拠・1日1回以下の低負荷で取得し、
     `moat_intelligence.detect_arena_event_surges` を「事後検知」から「事前予告＋事後実測検証」へ昇格させる。
3. JR公式公開運行情報からのターミナル駅遅延検知 (`get_city_transit_status`):
   - 15分TTLキャッシュ・非ブロッキングで主要ターミナル路線の遅れ・運転見合わせを検知し、
     終電前後の足止め需要と深夜滞在率の相関データを蓄積する。
"""

import datetime
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    import store_hours
    _STATIC_HOLIDAYS = set(getattr(store_hours, "JP_HOLIDAYS", set()) or set())
except Exception:
    _STATIC_HOLIDAYS = set()

logger = logging.getLogger(__name__)
JST = datetime.timezone(datetime.timedelta(hours=9))

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ARENA_CACHE_FILE = os.path.join(CACHE_DIR, "arena_schedules_cache.json")

_BOT_UA = "Mozilla/5.0 (compatible; AisekiNaviCalendarBot/1.0; +https://aiseki-navi.com)"

_robots_lock = threading.Lock()
_robots_cache = {}  # netloc -> (checked_mono, RobotFileParser | None)
_ROBOTS_TTL_SEC = 86400.0

_arena_lock = threading.Lock()
_arena_state = {
    "ts": 0.0,
    "fail_ts": -999.0,
    "fetching": False,
    "loaded_disk": False,
    "events": [],  # list of dicts sorted by event_date
}
_ARENA_TTL_SEC = 43200.0  # 12 hours (max 1-2 polite fetches per day)

_transit_lock = threading.Lock()
_transit_state = {
    "ts": 0.0,
    "fail_ts": -999.0,
    "fetching": False,
    "cities": {},
}
_TRANSIT_TTL_SEC = 900.0  # 15 minutes

_WEEKDAY_JA_TO_INT = {
    "月": 0,
    "火": 1,
    "水": 2,
    "木": 3,
    "金": 4,
    "土": 5,
    "日": 6,
}

# Supplementary 2026-2027 national holidays (ensures multi-year coverage alongside store_hours.JP_HOLIDAYS)
_EXTRA_JP_HOLIDAYS = {
    "2026-01-01", "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20",
    "2026-04-29", "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",
    "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
    "2026-10-12", "2026-11-03", "2026-11-23",
    "2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-21",
    "2027-03-22", "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23", "2027-10-11",
    "2027-11-03", "2027-11-23",
}
ALL_JP_HOLIDAYS = _STATIC_HOLIDAYS | _EXTRA_JP_HOLIDAYS


def _is_off_day(d: datetime.date) -> bool:
    ds = d.strftime("%Y-%m-%d")
    if d.weekday() >= 5 or ds in ALL_JP_HOLIDAYS:
        return True
    if (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3):
        return True
    return False


def _second_monday_of_january(year: int) -> datetime.date:
    jan1 = datetime.date(year, 1, 1)
    offset = (0 - jan1.weekday()) % 7
    first_mon = jan1 + datetime.timedelta(days=offset)
    return first_mon + datetime.timedelta(days=7)


def classify_day_context(target_date=None) -> dict:
    """
    指定日（`datetime.date` / `datetime.datetime` / `'YYYY-MM-DD'` / `None`）の
    カレンダー文脈タグを決定論的に返す（外部通信ゼロ）。
    """
    if target_date is None:
        d = datetime.datetime.now(JST).date()
    elif isinstance(target_date, datetime.datetime):
        d = target_date.astimezone(JST).date() if target_date.tzinfo else target_date.date()
    elif isinstance(target_date, datetime.date):
        d = target_date
    else:
        s = str(target_date).strip()[:10]
        try:
            d = datetime.date.fromisoformat(s)
        except Exception:
            d = datetime.datetime.now(JST).date()

    ds = d.strftime("%Y-%m-%d")
    dow = d.weekday()  # 0=Mon .. 6=Sun
    is_nat_holiday = 1 if ds in ALL_JP_HOLIDAYS else 0
    tomorrow = d + datetime.timedelta(days=1)
    yesterday = d - datetime.timedelta(days=1)

    today_off = _is_off_day(d)
    tomorrow_off = _is_off_day(tomorrow)
    yesterday_off = _is_off_day(yesterday)

    is_pre_holiday = 1 if (dow in (4, 5) or (tomorrow.strftime("%Y-%m-%d") in ALL_JP_HOLIDAYS) or (tomorrow.month == 12 and tomorrow.day >= 29)) else 0

    if not today_off:
        holiday_streak_pos = "weekday"
    elif not yesterday_off and not tomorrow_off:
        holiday_streak_pos = "single"
    elif not yesterday_off and tomorrow_off:
        holiday_streak_pos = "start"
    elif yesterday_off and tomorrow_off:
        holiday_streak_pos = "middle"
    else:
        holiday_streak_pos = "end"

    # Payday window: 24th-27th (plus 23rd if 25th falls on weekend/holiday) or last 2 days of month
    next_month_probe = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    last_day_of_month = (next_month_probe - datetime.timedelta(days=1)).day
    d25 = d.replace(day=25)
    friday_before_25 = (d.day in (23, 24) and dow == 4 and _is_off_day(d25))
    is_payday_window = 1 if (24 <= d.day <= 27 or friday_before_25 or d.day >= (last_day_of_month - 1)) else 0

    is_bonus_window = 1 if (
        (d.month == 6 and d.day >= 10)
        or (d.month == 7 and d.day <= 15)
        or (d.month == 12 and 5 <= d.day <= 28)
    ) else 0

    seijin_mon = _second_monday_of_january(d.year)
    seijin_sat = seijin_mon - datetime.timedelta(days=2)
    seijin_sun = seijin_mon - datetime.timedelta(days=1)

    if d.month == 12 and d.day == 24:
        season_tag = "christmas_eve"
    elif d.month == 12 and d.day == 25:
        season_tag = "christmas"
    elif d.month == 12 and 26 <= d.day <= 30:
        season_tag = "work_year_end"
    elif d.month == 12 and d.day == 31:
        season_tag = "new_year_eve"
    elif d.month == 1 and 1 <= d.day <= 3:
        season_tag = "shogatsu"
    elif d in (seijin_sat, seijin_sun, seijin_mon):
        season_tag = "seijin_weekend"
    elif d.month == 12 and 1 <= d.day <= 23:
        season_tag = "bounenkai_peak" if is_pre_holiday else "bounenkai_early"
    elif d.month == 11 and d.day >= 20:
        season_tag = "bounenkai_early"
    elif d.month == 10 and d.day >= 25:
        season_tag = "halloween"
    elif (d.month == 3 and d.day >= 20) or (d.month == 4 and d.day <= 12):
        season_tag = "shinkan_welcome"
    elif (d.month == 4 and d.day >= 28) or (d.month == 5 and d.day <= 6):
        season_tag = "gw"
    elif d.month == 8 and 11 <= d.day <= 16:
        season_tag = "obon"
    elif d.month == 2 and d.day in (13, 14):
        season_tag = "valentines"
    else:
        season_tag = "regular"

    tags = []
    if is_nat_holiday:
        tags.append("holiday")
    if is_pre_holiday:
        tags.append("pre_holiday")
    if holiday_streak_pos in ("start", "middle", "end"):
        tags.append(f"streak_{holiday_streak_pos}")
    if is_payday_window:
        tags.append("payday")
    if is_bonus_window:
        tags.append("bonus")
    if season_tag != "regular":
        tags.append(season_tag)

    return {
        "date_jst": ds,
        "dow": dow,
        "is_holiday": is_nat_holiday,
        "is_pre_holiday": is_pre_holiday,
        "holiday_streak_pos": holiday_streak_pos,
        "is_payday_window": is_payday_window,
        "is_bonus_window": is_bonus_window,
        "season_tag": season_tag,
        "tags": tags,
        "tags_json": json.dumps(tags, ensure_ascii=False),
    }


def _is_robots_allowed(url: str, timeout_sec: float = 3.5) -> bool:
    """Polite robots.txt check with strict HTTP timeout (never hangs unlike stdlib RobotFileParser.read)."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if not netloc:
            return False
        now_m = time.monotonic()
        with _robots_lock:
            cached = _robots_cache.get(netloc)
            if cached and (now_m - cached[0]) < _ROBOTS_TTL_SEC:
                rp = cached[1]
                return True if rp is None else bool(rp.can_fetch("*", url))

        robots_url = f"{parsed.scheme}://{netloc}/robots.txt"
        rp = None
        try:
            resp = requests.get(robots_url, headers={"User-Agent": _BOT_UA}, timeout=(2.5, timeout_sec))
            if resp.status_code == 200 and resp.text:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(resp.text.splitlines())
        except Exception:
            rp = None

        with _robots_lock:
            _robots_cache[netloc] = (time.monotonic(), rp)
        return True if rp is None else bool(rp.can_fetch("*", url))
    except Exception:
        return False


def _estimate_end_hour(start_time_str: str, default_hour: int = 21) -> int:
    if start_time_str:
        m = re.search(r"(\d{1,2})\s*:\s*(\d{2})", str(start_time_str))
        if m:
            sh = int(m.group(1))
            sm = int(m.group(2))
            est = sh + 3 if sm >= 30 else sh + 2
            return max(17, min(23, est))
    return default_hour


def _clean_title(raw: str) -> str:
    t = re.sub(r"\s+", " ", str(raw or "")).strip()
    t = re.sub(r"^(コンサート|イベント|音楽・芸能|取扱公演|アリーナ)\s*", "", t).strip()
    return t[:110]


def parse_tokyo_dome_html(html_bytes: bytes, ref_year: int, ref_month: int) -> list:
    """Parse Tokyo Dome official monthly schedule (`https://www.tokyo-dome.co.jp/dome/event/`)."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    out = []
    for li in soup.select("li.m-link-event__item"):
        txt = " ".join(li.get_text(" ", strip=True).split())
        m_date = re.search(r"(\d{1,2})月\s*(\d{1,2})日", txt)
        if not m_date:
            continue
        mo, dy = int(m_date.group(1)), int(m_date.group(2))
        yr = ref_year + 1 if mo < ref_month - 1 else ref_year
        # Strip leading date + weekday
        rest = txt[m_date.end():].strip()
        rest = re.sub(r"^[（(][月火水木金土日・祝]+[)）]\s*", "", rest).strip()
        if not rest or len(rest) < 4:
            continue
        open_m = re.search(r"開場\s*(\d{1,2}:\d{2})", rest)
        start_m = re.search(r"(?:開演|開始|試合開始)\s*(\d{1,2}:\d{2})", rest)
        title_part = re.split(r"開場|開演|開始|お問い合わせ|【公演", rest)[0].strip()
        title = _clean_title(title_part)
        if not title:
            continue
        open_t = open_m.group(1) if open_m else ""
        start_t = start_m.group(1) if start_m else ""
        try:
            ev_date = datetime.date(yr, mo, dy).strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({
            "event_date": ev_date,
            "venue_id": "tokyo_dome_budokan",
            "venue_name": "東京ドーム",
            "city_key": "tokyo",
            "event_title": title,
            "open_time": open_t,
            "start_time": start_t,
            "est_end_hour": _estimate_end_hour(start_t, 21),
            "source_url": "https://www.tokyo-dome.co.jp/dome/event/",
        })
    return out


def parse_k_arena_html(html_bytes: bytes) -> list:
    """Parse K-Arena Yokohama official schedule (`https://k-arena.com/schedule/`)."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    out = []
    for li in soup.select("li.schedule-list-item"):
        txt = " ".join(li.get_text(" | ", strip=True).split())
        m_date = re.search(r"(202\d)[./\-](\d{1,2})[./\-](\d{1,2})", txt)
        if not m_date:
            continue
        yr, mo, dy = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        open_m = re.search(r"OPEN\s*(\d{1,2}:\d{2})", txt, re.I)
        start_m = re.search(r"START\s*(\d{1,2}:\d{2})", txt, re.I)
        parts = [p.strip() for p in txt.split("|") if p.strip()]
        # Filter out date, badge, and OPEN/START parts
        cand_parts = [
            p for p in parts
            if not re.search(r"^202\d[./\-]|^OPEN\s|^START\s|^取扱公演$", p, re.I)
        ]
        title = _clean_title(cand_parts[0] if cand_parts else "")
        if not title:
            continue
        try:
            ev_date = datetime.date(yr, mo, dy).strftime("%Y-%m-%d")
        except ValueError:
            continue
        start_t = start_m.group(1) if start_m else ""
        out.append({
            "event_date": ev_date,
            "venue_id": "yokohama_arenas",
            "venue_name": "Kアリーナ横浜",
            "city_key": "yokohama",
            "event_title": title,
            "open_time": open_m.group(1) if open_m else "",
            "start_time": start_t,
            "est_end_hour": _estimate_end_hour(start_t, 20),
            "source_url": "https://k-arena.com/schedule/",
        })
    return out


def parse_osaka_jo_hall_html(html_bytes: bytes, ref_year: int, ref_month: int) -> list:
    """Parse Osaka-Jo Hall official schedule (`https://www.osaka-johall.com/event/`)."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    full_txt = soup.get_text(" ", strip=True)
    m_ym = re.search(r"(202\d)\s*[/年.]\s*(\d{1,2})", full_txt)
    yr = int(m_ym.group(1)) if m_ym else ref_year
    mo = int(m_ym.group(2)) if m_ym else ref_month

    out = []
    for li in soup.select("li.event-detail"):
        txt = " ".join(li.get_text(" | ", strip=True).split())
        m_day = re.match(r"^(\d{1,2})\s*\|?\s*[（(][月火水木金土日・祝]+[)）]", txt)
        if not m_day:
            continue
        dy = int(m_day.group(1))
        name_el = li.select_one(".event-name, dt, h3, h4")
        if name_el:
            raw_title = " ".join(name_el.get_text(" ", strip=True).split())
            raw_title = re.split(r"開場|開演|座席|主催", raw_title)[0].strip()
        else:
            rest = txt[m_day.end():].lstrip(" |").strip()
            raw_title = re.split(r"\|\s*(?:開場|開演|座席|主催)", rest)[0].strip()
        title = _clean_title(raw_title)
        if not title:
            continue
        open_m = re.search(r"開場\s*\|?\s*(\d{1,2}:\d{2})", txt)
        start_m = re.search(r"開演\s*\|?\s*(\d{1,2}:\d{2})", txt)
        start_t = start_m.group(1) if start_m else ""
        try:
            ev_date = datetime.date(yr, mo, dy).strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({
            "event_date": ev_date,
            "venue_id": "osaka_jo_hall",
            "venue_name": "大阪城ホール",
            "city_key": "osaka",
            "event_title": title,
            "open_time": open_m.group(1) if open_m else "",
            "start_time": start_t,
            "est_end_hour": _estimate_end_hour(start_t, 20),
            "source_url": "https://www.osaka-johall.com/event/",
        })
    return out


def parse_kyocera_dome_html(html_bytes: bytes) -> list:
    """Parse Kyocera Dome Osaka official schedule (`https://www.kyoceradome-osaka.jp/schedule/`)."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    out = []
    for box in soup.select("section.event-box"):
        txt = " ".join(box.get_text(" | ", strip=True).split())
        m_date = re.search(r"(202\d)年\s*(\d{1,2})月\s*(\d{1,2})日", txt)
        if not m_date:
            continue
        yr, mo, dy = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        parts = [p.strip() for p in txt.split("|") if p.strip()]
        cand = [
            p for p in parts[1:5]
            if not re.search(r"^202\d年|^日時$|^コンサート$|^野球$|^開場時間|^開始時間|^入場料", p)
        ]
        title = _clean_title(" ".join(cand[:2]) if cand else "")
        if not title:
            continue
        open_m = re.search(r"開場時間[：:]\s*(\d{1,2}:\d{2})", txt)
        start_m = re.search(r"開始時間[：:]\s*(\d{1,2}:\d{2})", txt)
        start_t = start_m.group(1) if start_m else ""
        try:
            ev_date = datetime.date(yr, mo, dy).strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({
            "event_date": ev_date,
            "venue_id": "kyocera_dome_osaka",
            "venue_name": "京セラドーム大阪",
            "city_key": "osaka",
            "event_title": title,
            "open_time": open_m.group(1) if open_m else "",
            "start_time": start_t,
            "est_end_hour": _estimate_end_hour(start_t, 21),
            "source_url": "https://www.kyoceradome-osaka.jp/schedule/",
        })
    return out


def parse_vantelin_dome_html(html_bytes: bytes, ref_year: int) -> list:
    """
    Parse Vantelin Dome Nagoya official concert schedule (`https://www.nagoya-dome.co.jp/enjoy/index.php`).
    Resolves exact calendar year by matching the Japanese day-of-week `(土)` against `datetime.date(yr, m, d).weekday()`
    so multi-year archive tables on the same page never collide!
    """
    soup = BeautifulSoup(html_bytes, "html.parser")
    out = []
    candidate_years = (ref_year, ref_year + 1, ref_year - 1)
    for tr in soup.find_all("tr"):
        txt = " ".join(tr.get_text(" ", strip=True).split())
        m_row = re.match(r"^(\d{1,2})/(\d{1,2})\s*\(\s*([月火水木金土日])(?:[・･]祝)?\s*\)\s*(.+)$", txt)
        if not m_row:
            continue
        mo, dy = int(m_row.group(1)), int(m_row.group(2))
        wd_ja = m_row.group(3)
        rest = m_row.group(4).strip()
        target_wd = _WEEKDAY_JA_TO_INT.get(wd_ja)
        matched_year = None
        for cy in candidate_years:
            try:
                if datetime.date(cy, mo, dy).weekday() == target_wd:
                    matched_year = cy
                    break
            except ValueError:
                continue
        if matched_year is None or matched_year < ref_year:
            continue
        times = re.findall(r"\b(\d{1,2}:\d{2})\b", rest)
        open_t = times[0] if len(times) >= 2 else ""
        start_t = times[1] if len(times) >= 2 else (times[0] if len(times) == 1 else "")
        title_part = re.sub(r"^(?:\d{1,2}:\d{2}\s*)+", "", rest).strip()
        title = _clean_title(title_part)
        if not title:
            continue
        ev_date = datetime.date(matched_year, mo, dy).strftime("%Y-%m-%d")
        out.append({
            "event_date": ev_date,
            "venue_id": "vantelin_dome_nagoya",
            "venue_name": "バンテリンドーム ナゴヤ",
            "city_key": "nagoya",
            "event_title": title,
            "open_time": open_t,
            "start_time": start_t,
            "est_end_hour": _estimate_end_hour(start_t, 20),
            "source_url": "https://www.nagoya-dome.co.jp/enjoy/index.php",
        })
    return out


def parse_ssa_html(html_bytes: bytes, url: str) -> list:
    """Parse Saitama Super Arena official monthly schedule (`https://www.saitama-arena.co.jp/schedule/YYYY/MM/`)."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    out = []
    for li in soup.select("li.concert-show, li.sports"):
        cls = " ".join(li.get("class") or [])
        # Skip small plaza-only free markets that don't drive terminal lounge surges
        if "keyaki-plaza" in cls and "main-arena" not in cls and "stadium" not in cls:
            continue
        txt = " ".join(li.get_text(" | ", strip=True).split())
        m_dates = re.findall(r"(202\d)/(\d{1,2})/(\d{1,2})", txt)
        if not m_dates:
            continue
        yr, mo, dy = int(m_dates[0][0]), int(m_dates[0][1]), int(m_dates[0][2])
        parts = [p.strip() for p in txt.split("|") if p.strip()]
        title_cand = ""
        for idx, p in enumerate(parts):
            if re.search(r"202\d/\d{1,2}/\d{1,2}", p) and idx + 1 < len(parts):
                title_cand = parts[idx + 1]
                break
        title = _clean_title(title_cand)
        if not title:
            continue
        start_m = re.search(r"開演[：:\s]*(\d{1,2}:\d{2})", txt)
        start_t = start_m.group(1) if start_m else ""
        try:
            ev_date = datetime.date(yr, mo, dy).strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({
            "event_date": ev_date,
            "venue_id": "saitama_super_arena",
            "venue_name": "さいたまスーパーアリーナ",
            "city_key": "tokyo",
            "event_title": title,
            "open_time": "",
            "start_time": start_t,
            "est_end_hour": _estimate_end_hour(start_t, 20),
            "source_url": url,
        })
    return out


def fetch_official_arena_schedules(now_jst: datetime.datetime = None, timeout_sec: float = 5.0) -> list:
    """
    Polite 1x/day fetcher for official public dome/arena event calendars.
    Checks `robots.txt` per host, deduplicates `(event_date, venue_id, event_title)`,
    persists to `data/arena_schedules_cache.json` and `local_db.upsert_arena_schedule_events`.
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(JST)
    elif now_jst.tzinfo is None:
        now_jst = now_jst.replace(tzinfo=JST)
    else:
        now_jst = now_jst.astimezone(JST)

    yr, mo = now_jst.year, now_jst.month
    ssa_url = f"https://www.saitama-arena.co.jp/schedule/{yr}/{mo:02d}/"
    sources = [
        ("tokyo_dome", "https://www.tokyo-dome.co.jp/dome/event/", lambda b: parse_tokyo_dome_html(b, yr, mo)),
        ("k_arena", "https://k-arena.com/schedule/", parse_k_arena_html),
        ("osaka_jo", "https://www.osaka-johall.com/event/", lambda b: parse_osaka_jo_hall_html(b, yr, mo)),
        ("kyocera", "https://www.kyoceradome-osaka.jp/schedule/", parse_kyocera_dome_html),
        ("vantelin", "https://www.nagoya-dome.co.jp/enjoy/index.php", lambda b: parse_vantelin_dome_html(b, yr)),
        ("ssa", ssa_url, lambda b: parse_ssa_html(b, ssa_url)),
    ]

    collected = []
    seen = set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": _BOT_UA, "Accept-Language": "ja,en;q=0.8"})
    try:
        for name, url, parser_fn in sources:
            try:
                if not _is_robots_allowed(url, timeout_sec=3.5):
                    logger.info(f"[arena_calendar] Skipping {name} per robots.txt")
                    continue
                resp = sess.get(url, timeout=(3.0, timeout_sec))
                if resp.status_code != 200 or not resp.content:
                    continue
                parsed_rows = parser_fn(resp.content) or []
                for row in parsed_rows:
                    dedup_key = (row["event_date"], row["venue_id"], row["event_title"])
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        collected.append(row)
            except Exception as err:
                logger.debug(f"[arena_calendar] {name} fetch warning: {err}")
    finally:
        try:
            sess.close()
        except Exception:
            pass

    collected.sort(key=lambda x: (x["event_date"], x["venue_id"]))
    if collected:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            payload = {
                "updated_at": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                "events": collected,
            }
            with open(ARENA_CACHE_FILE, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as err:
            logger.debug(f"[arena_calendar] Disk cache write warning: {err}")

        try:
            import local_db
            if hasattr(local_db, "upsert_arena_schedule_events"):
                local_db.upsert_arena_schedule_events(collected)
        except Exception as err:
            logger.debug(f"[arena_calendar] DB upsert warning: {err}")

    return collected


def _in_test_harness() -> bool:
    if os.environ.get("ALLOW_ARENA_CACHE_IN_TESTS") == "1":
        return False
    return ("unittest" in sys.modules) or ("pytest" in sys.modules)


def reset_for_tests():
    with _arena_lock:
        _arena_state["ts"] = 0.0
        _arena_state["fail_ts"] = -999.0
        _arena_state["fetching"] = False
        _arena_state["loaded_disk"] = False
        _arena_state["events"] = []
    with _transit_lock:
        _transit_state["ts"] = 0.0
        _transit_state["fail_ts"] = -999.0
        _transit_state["fetching"] = False
        _transit_state["cities"] = {}


def _load_disk_arena_cache() -> list:
    if _in_test_harness() or not os.path.exists(ARENA_CACHE_FILE):
        return []
    try:
        with open(ARENA_CACHE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        evs = data.get("events") if isinstance(data, dict) else None
        return evs if isinstance(evs, list) else []
    except Exception:
        return []


def get_arena_schedule_events(allow_blocking: bool = False, force_refresh: bool = False) -> list:
    """Return cached official arena schedule list in 0ms on HTTP threads; refresh asynchronously when stale."""
    if os.environ.get("DISABLE_EXTERNAL_WEATHER") == "1":
        return []

    now_m = time.monotonic()
    with _arena_lock:
        if not _arena_state["loaded_disk"] and not _arena_state["events"]:
            disk_evs = _load_disk_arena_cache()
            if disk_evs:
                _arena_state["events"] = disk_evs
                _arena_state["ts"] = now_m - (_ARENA_TTL_SEC * 0.5)
            _arena_state["loaded_disk"] = True
        cached = list(_arena_state["events"])
        age = now_m - _arena_state["ts"]
        fail_age = now_m - _arena_state["fail_ts"]
        fetching = _arena_state["fetching"]

    if not force_refresh and cached and age < _ARENA_TTL_SEC:
        return cached

    if (allow_blocking or force_refresh) and not _in_test_harness():
        fresh = fetch_official_arena_schedules()
        with _arena_lock:
            if fresh:
                _arena_state["events"] = fresh
                _arena_state["ts"] = time.monotonic()
                _arena_state["fail_ts"] = -999.0
                return list(fresh)
            _arena_state["fail_ts"] = time.monotonic()
        return cached

    if not fetching and fail_age >= 300.0 and not _in_test_harness() and os.environ.get("DISABLE_SCHEDULER") != "1" and os.environ.get("DISABLE_SHEETS") != "1":
        should_spawn = False
        with _arena_lock:
            if not _arena_state["fetching"]:
                _arena_state["fetching"] = True
                should_spawn = True

        def _bg():
            ok = False
            try:
                fresh = fetch_official_arena_schedules()
                if fresh:
                    ok = True
                    with _arena_lock:
                        _arena_state["events"] = fresh
                        _arena_state["ts"] = time.monotonic()
                        _arena_state["fail_ts"] = -999.0
            except Exception:
                pass
            finally:
                with _arena_lock:
                    if not ok:
                        _arena_state["fail_ts"] = time.monotonic()
                    _arena_state["fetching"] = False

        if should_spawn:
            threading.Thread(target=_bg, daemon=True, name="arena-cal-sync").start()

    return cached


def get_arena_events_for_date(target_date=None, allow_blocking: bool = False) -> dict:
    """Return `{venue_id: [event_dict, ...]}` for `target_date` (defaults to today JST)."""
    if target_date is None:
        ds = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    elif isinstance(target_date, datetime.datetime):
        ds = (target_date.astimezone(JST) if target_date.tzinfo else target_date).strftime("%Y-%m-%d")
    elif isinstance(target_date, datetime.date):
        ds = target_date.strftime("%Y-%m-%d")
    else:
        ds = str(target_date).strip()[:10]

    evs = get_arena_schedule_events(allow_blocking=allow_blocking)
    by_venue = {}
    for ev in evs:
        if ev.get("event_date") == ds:
            vid = ev.get("venue_id") or ""
            if vid:
                by_venue.setdefault(vid, []).append(ev)
    return by_venue


# ==============================================================================
# Official JR East Terminal Line Disruption Context (15-min TTL, non-blocking)
# ==============================================================================
_JR_LINE_TO_CITY = {
    "山手線": "tokyo",
    "京浜東北線": "tokyo",
    "埼京線": "tokyo",
    "中央線快速電車": "tokyo",
    "中央・総武各駅停車": "tokyo",
    "湘南新宿ライン": "tokyo",
    "上野東京ライン": "tokyo",
    "常磐線快速電車": "tokyo",
    "総武快速線": "tokyo",
    "横須賀線": "yokohama",
    "根岸線": "yokohama",
    "横浜線": "yokohama",
    "東海道線": "yokohama",
    "仙石線": "sendai",
    "東北本線": "sendai",
}


def parse_jreast_status_html(html_bytes: bytes) -> dict:
    """Parse JR East official route status items (`li.traininfo-routes__table__item`)."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    city_delays = {}
    for li in soup.select("li.traininfo-routes__table__item"):
        parts = [p.strip() for p in li.get_text("|", strip=True).split("|") if p.strip()]
        if len(parts) < 2:
            continue
        line_name, status_txt = parts[0], parts[1]
        city_key = _JR_LINE_TO_CITY.get(line_name)
        if not city_key:
            continue
        entry = city_delays.setdefault(city_key, {"disrupted": False, "delayed_lines": []})
        if "平常運転" not in status_txt and any(k in status_txt for k in ("遅れ", "見合わせ", "運休", "乱れ", "情報")):
            entry["disrupted"] = True
            label = f"{line_name}（{status_txt[:24]}）"
            if label not in entry["delayed_lines"]:
                entry["delayed_lines"].append(label)
    return city_delays


def _fetch_jr_transit_status_live(timeout_sec: float = 4.5) -> dict:
    merged = {}
    urls = (
        "https://traininfo.jreast.co.jp/train_info/kanto.aspx",
        "https://traininfo.jreast.co.jp/train_info/tohoku.aspx",
    )
    sess = requests.Session()
    sess.headers.update({"User-Agent": _BOT_UA, "Accept-Language": "ja"})
    try:
        for u in urls:
            if not _is_robots_allowed(u, timeout_sec=3.0):
                continue
            r = sess.get(u, timeout=(2.5, timeout_sec))
            if r.status_code != 200 or not r.content:
                continue
            parsed = parse_jreast_status_html(r.content)
            for ck, info in parsed.items():
                dst = merged.setdefault(ck, {"disrupted": False, "delayed_lines": []})
                if info.get("disrupted"):
                    dst["disrupted"] = True
                for ln in info.get("delayed_lines") or []:
                    if ln not in dst["delayed_lines"]:
                        dst["delayed_lines"].append(ln)
    finally:
        try:
            sess.close()
        except Exception:
            pass
    return merged


def get_city_transit_status(allow_blocking: bool = False) -> dict:
    """Return `{city_key: {'disrupted': bool, 'delayed_lines': [...]}}` without ever blocking HTTP threads."""
    if os.environ.get("DISABLE_EXTERNAL_WEATHER") == "1":
        return {}
    now_m = time.monotonic()
    with _transit_lock:
        cached = dict(_transit_state["cities"])
        age = now_m - _transit_state["ts"]
        fail_age = now_m - _transit_state["fail_ts"]
        fetching = _transit_state["fetching"]

    if cached and age < _TRANSIT_TTL_SEC:
        return cached

    if allow_blocking and not _in_test_harness():
        try:
            fresh = _fetch_jr_transit_status_live()
            with _transit_lock:
                _transit_state["cities"] = fresh
                _transit_state["ts"] = time.monotonic()
                _transit_state["fail_ts"] = -999.0
            return fresh
        except Exception:
            with _transit_lock:
                _transit_state["fail_ts"] = time.monotonic()
            return cached

    if not fetching and fail_age >= 120.0 and not _in_test_harness() and os.environ.get("DISABLE_SCHEDULER") != "1" and os.environ.get("DISABLE_SHEETS") != "1":
        should_spawn_tr = False
        with _transit_lock:
            if not _transit_state["fetching"]:
                _transit_state["fetching"] = True
                should_spawn_tr = True

        def _bg():
            ok = False
            try:
                fresh = _fetch_jr_transit_status_live()
                ok = True
                with _transit_lock:
                    _transit_state["cities"] = fresh
                    _transit_state["ts"] = time.monotonic()
                    _transit_state["fail_ts"] = -999.0
            except Exception:
                pass
            finally:
                with _transit_lock:
                    if not ok:
                        _transit_state["fail_ts"] = time.monotonic()
                    _transit_state["fetching"] = False

        if should_spawn_tr:
            threading.Thread(target=_bg, daemon=True, name="jr-transit-sync").start()

    return cached
