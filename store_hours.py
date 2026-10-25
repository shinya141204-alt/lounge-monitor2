"""
store_hours.py - Official Per-Store Business Hours Engine for Aiseki Navi.

Determines whether each lounge store is currently within its official operating hours
(based on official websites: oriental-lounge.com, ag-ree.jp, jis.bar, yatakoi.com,
xix-thelounge.com, bar-clovers.com, aiseki-hiroshima.com) so that a `0 : 0` count
during open hours is accurately shown as `0 : 0` (open) rather than falsely labeled
as "営業時間外" (closed / off-hours).
"""

import datetime

JST = datetime.timezone(datetime.timedelta(hours=9))

# Japanese public holidays (2025-2028) for accurate pre-holiday (祝前日) & holiday hours detection.
JP_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-13", "2025-02-11", "2025-02-23", "2025-02-24",
    "2025-03-20", "2025-04-29", "2025-05-03", "2025-05-04", "2025-05-05", "2025-05-06",
    "2025-07-21", "2025-08-11", "2025-09-15", "2025-09-23", "2025-10-13",
    "2025-11-03", "2025-11-23", "2025-11-24",
    # 2026
    "2026-01-01", "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20",
    "2026-04-29", "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",
    "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
    "2026-10-12", "2026-11-03", "2026-11-23",
    # 2027
    "2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-21", "2027-03-22",
    "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23", "2027-10-11",
    "2027-11-03", "2027-11-23",
    # 2028
    "2028-01-01", "2028-01-10", "2028-02-11", "2028-02-23", "2028-03-20",
    "2028-04-29", "2028-05-03", "2028-05-04", "2028-05-05",
    "2028-07-17", "2028-08-11", "2028-09-18", "2028-09-22", "2028-10-09",
    "2028-11-03", "2028-11-23",
}


def _spec(mt, fs=None, sh=None, cw=None, label=""):
    """Helper to build a compact schedule dict.
    Hours use 24h+ notation across midnight (e.g. 18 -> 18:00, 29 -> 05:00 next morning, 31 -> 07:00).
    mt = Mon-Thu, fs = Fri/Sat/Pre-holiday, sh = Sun/Holiday.
    cw = list of closed weekday ints (0=Mon .. 6=Sun) when non-pre-holiday.
    """
    fs = fs or mt
    sh = sh or mt
    return {
        "mt": list(mt),
        "fs": list(fs),
        "sh": list(sh),
        "cw": list(cw or []),
        "label": label,
    }


# Official business hours per store (all 53 active stores + legacy aliases)
STORE_BUSINESS_HOURS = {
    # --- Oriental Lounge & ag (Kanto) ---
    "OLG 渋谷本店": _spec((18, 29), (18, 31), (18, 29), label="日〜木 18:00〜翌5:00 / 金土祝前 18:00〜翌7:00"),
    "OLG 渋谷駅前": _spec((18, 29), (18, 30), (18, 29), label="日〜木 18:00〜翌5:00 / 金土祝前 18:00〜翌6:00"),
    "OLG 恵比寿": _spec((18, 29), (18, 30), (18, 29), label="日〜木 18:00〜翌5:00 / 金土祝前 18:00〜翌6:00"),
    "OLG ag渋谷": _spec((19, 28), (19, 29), (19, 28), label="日〜木 19:00〜翌4:00 / 金土祝前 19:00〜翌5:00"),
    "OLG 新宿": _spec((19, 29), (19, 29), (19, 29), label="19:00〜翌5:00"),
    "OLG ag新宿": _spec((19, 29), (19, 29), (19, 29), label="19:00〜翌5:00"),
    "OLG 上野": _spec((18, 25), (18, 29), (18, 25), label="日〜木 18:00〜翌1:00 / 金土祝前 18:00〜翌5:00"),
    "OLG ag上野": _spec((18, 29), (18, 30), (18, 29), label="日〜木 18:00〜翌5:00 / 金土祝前 18:00〜翌6:00"),
    "OLG 錦糸町": _spec((18, 26), (18, 29), (18, 26), label="日〜木 18:00〜翌2:00 / 金土祝前 18:00〜翌5:00"),
    "OLG 柏": _spec((18, 26), (18, 29), (18, 26), label="日〜木 18:00〜翌2:00 / 金土祝前 18:00〜翌5:00"),
    "OLG 町田": _spec((19, 28), (18, 29), (19, 28), label="日〜木 19:00〜翌4:00 / 金土祝前 18:00〜翌5:00"),
    "OLG 横浜": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "OLG 大宮": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 宇都宮": _spec((19, 28), (19, 29), (19, 28), cw=[2], label="日〜木 19:00〜翌4:00 / 金土祝前 19:00〜翌5:00（水曜定休）"),
    "OLG 高崎": _spec((19, 29), (19, 29), (19, 29), label="19:00〜翌5:00"),

    # --- Oriental Lounge & ag (Kansai / Chubu / Hokuriku / Tohoku / Hokkaido) ---
    "OLG 大阪駅前": _spec((18, 30), (18, 31), (18, 30), label="日〜木 18:00〜翌6:00 / 金土祝前 18:00〜翌7:00"),
    "OLG 梅田": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 茶屋町": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG ag梅田": _spec((17, 29), (17, 29), (17, 29), label="17:00〜翌5:00"),
    "OLG 天満": _spec((17, 29), (17, 29), (17, 29), label="17:00〜翌5:00"),
    "OLG 心斎橋": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "OLG 難波": _spec((19, 30), (19, 30), (19, 30), label="19:00〜翌6:00"),
    "OLG 京都": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "OLG 神戸": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 岡山": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 広島": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG ag広島": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG ag名古屋": _spec((19, 31), (19, 31), (19, 31), label="19:00〜翌7:00"),
    "OLG 名古屋 錦": _spec((18, 30), (18, 31), (18, 30), label="日〜木 18:00〜翌6:00 / 金土祝前 18:00〜翌7:00"),
    "OLG 名古屋 栄": _spec((17, 29), (17, 29), (17, 29), label="17:00〜翌5:00"),
    "OLG 静岡": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 浜松": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 金沢": _spec((19, 31), (19, 31), (19, 31), label="19:00〜翌7:00"),
    "OLG ag金沢": _spec((19, 31), (19, 31), (19, 31), label="19:00〜翌7:00"),
    "OLG 仙台": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "OLG ag仙台": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "OLG 札幌": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG ag札幌": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),

    # --- Oriental Lounge & ag (Kyushu / Okinawa / Korea) ---
    "OLG 福岡": _spec((19, 30), (19, 30), (19, 30), label="19:00〜翌6:00"),
    "OLG ag福岡": _spec((19, 30), (19, 30), (19, 30), label="19:00〜翌6:00"),
    "OLG 小倉": _spec((18, 26), (18, 29), (18, 26), label="日〜木 18:00〜翌2:00 / 金土祝前 18:00〜翌5:00"),
    "OLG 長崎": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 大分": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 熊本": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 宮崎": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 鹿児島": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "OLG 沖縄": _spec((18, 31), (18, 31), (18, 31), label="18:00〜翌7:00"),
    "OLG ag沖縄": _spec((18, 31), (18, 31), (18, 31), label="18:00〜翌7:00"),
    "OLG ソウル カンナム": _spec((18, 26), (18, 29), (18, 26), cw=[0], label="火〜木・日 18:00〜翌2:00 / 金土 18:00〜翌5:00（月曜定休）"),
    "OLG ソウル ホンデ": _spec((19, 28), (19, 29), (19, 28), label="日〜木 19:00〜翌4:00 / 金土 19:00〜翌5:00"),

    # --- JIS (All 10 stores) ---
    "JIS SAPPORO": _spec((19, 30), (19, 30), (19, 30), label="19:00〜翌6:00"),
    "JIS OMIYA": _spec((19, 30), (18, 30), (18, 30), label="月〜木 19:00〜翌6:00 / 金土日祝前 18:00〜翌6:00"),
    "JIS SHINJUKU": _spec((19, 30), (19, 30), (19, 30), label="19:00〜翌6:00"),
    "JIS NISHISHINJUKU": _spec((19, 25), (18, 29), (18, 25), label="月〜木 19:00〜翌1:00 / 日祝 18:00〜翌1:00 / 金土祝前 18:00〜翌5:00"),
    "JIS UMEDA": _spec((18, 30), (18, 30), (18, 30), label="18:00〜翌6:00"),
    "JIS NAMBA": _spec((19, 30), (19, 30), (19, 30), label="19:00〜翌6:00"),
    "JIS CHAYAMACHI": _spec((18, 29), (18, 30), (18, 29), label="日〜木 18:00〜翌5:00 / 金土祝前 18:00〜翌6:00"),
    "JIS FUKUOKA": _spec((19, 30), (18, 31), (18, 30), label="月〜木 19:00〜翌6:00 / 日祝 18:00〜翌6:00 / 金土祝前 18:00〜翌7:00"),
    "JIS KUMAMOTO": _spec((20, 28), (20, 28), (20, 28), label="20:00〜翌4:00"),
    "JIS MATSUYAMA": _spec((19, 25), (19, 27), (19, 25), label="日〜木 19:00〜翌1:00 / 金土祝前 19:00〜翌3:00"),

    # --- Regional Independent Lounges ---
    "XIX OKAYAMA": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "YATAKOI UMEDA": _spec((18, 26), (18, 27), (18, 26), label="日〜木祝 18:00〜翌2:00 / 金土祝前 18:00〜翌3:00"),
    "CLOVERS HIROSHIMA": _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00"),
    "ALFA HIROSHIMA": _spec((18, 29), (18, 30), (18, 29), label="日〜木 18:00〜翌5:00 / 金土祝前 18:00〜翌6:00"),
}

DEFAULT_SPEC = _spec((18, 29), (18, 29), (18, 29), label="18:00〜翌5:00")


def _fmt_hour_span(open_h: int, close_h: int) -> str:
    c_disp = close_h - 24 if close_h >= 24 else close_h
    prefix = "翌" if close_h >= 24 else ""
    return f"{open_h:02d}:00〜{prefix}{c_disp}:00"


def get_store_schedule_spec(store_name: str) -> dict:
    return STORE_BUSINESS_HOURS.get(store_name, DEFAULT_SPEC)


def get_business_hours_status(store_name: str, now_jst: datetime.datetime = None, total_guests: int = 0) -> dict:
    """Evaluate whether `store_name` is currently open based on official business hours.

    - Business day rolls over at 12:00 noon JST (so 03:30 AM Sunday belongs to Saturday night's shift).
    - If `total_guests > 0`, the store is unconditionally treated as open (`is_open_now = True`).
    - If `total_guests == 0` and current time is within `[open_h, close_h)` on an active day,
      `is_open_now` is `True` so UI displays `0 : 0` rather than falsely claiming off-hours.
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(JST)
    elif now_jst.tzinfo is None:
        now_jst = now_jst.replace(tzinfo=JST)
    else:
        now_jst = now_jst.astimezone(JST)

    spec = get_store_schedule_spec(store_name)
    is_korea = ("ソウル" in (store_name or "")) or ("カンナム" in (store_name or "")) or ("ホンデ" in (store_name or ""))

    if now_jst.hour < 12:
        biz_date = (now_jst - datetime.timedelta(days=1)).date()
        cur_minutes = (24 + now_jst.hour) * 60 + now_jst.minute
    else:
        biz_date = now_jst.date()
        cur_minutes = now_jst.hour * 60 + now_jst.minute

    wd = biz_date.weekday()  # 0=Mon .. 6=Sun
    next_date_iso = (biz_date + datetime.timedelta(days=1)).isoformat()
    biz_date_iso = biz_date.isoformat()

    is_pre_holiday = (not is_korea) and (next_date_iso in JP_HOLIDAYS)
    is_holiday = (not is_korea) and (biz_date_iso in JP_HOLIDAYS)

    closed_weekdays = spec.get("cw") or []
    is_regular_closed_day = (wd in closed_weekdays) and (not is_pre_holiday)

    if is_pre_holiday or wd in (4, 5):  # Fri, Sat, or Pre-holiday
        open_h, close_h = spec["fs"]
    elif wd == 6 or is_holiday:         # Sun or Public Holiday (non-pre-holiday)
        open_h, close_h = spec["sh"]
    else:                               # Mon - Thu
        open_h, close_h = spec["mt"]

    if is_regular_closed_day:
        today_hours_label = "定休日"
        scheduled_open = False
    else:
        today_hours_label = _fmt_hour_span(open_h, close_h)
        scheduled_open = (open_h * 60) <= cur_minutes < (close_h * 60)

    is_open_now = True if (int(total_guests or 0) > 0) else scheduled_open

    return {
        "is_open_now": is_open_now,
        "scheduled_open": scheduled_open,
        "hours_label": spec["label"],
        "today_hours_label": today_hours_label,
        "schedule_spec": spec,
    }
