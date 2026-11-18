"""
moshimo_reel_queue.py - 3D Voxel "Moshimo Series" Instagram Reel & YouTube Shorts Queue Engine

Schedules and publishes pre-rendered 3D Voxel Moshimo `.mp4` episodes hosted under
`/static/reels/moshimo/` via Meta's Instagram Graph API (`@aiseki_navi1`), while exposing
1-click YouTube Shorts (`@aiseki_navi`) title, description/pinned-comment, and MP4 download
packages so cross-channel short-video publishing never stalls.

Anti-cannibalization & uninterrupted continuity rules:
  1. When a Moshimo episode is due on the `20:00` JST slot (`Mon–Fri`, plus initial kick-off),
     it publishes that episode and marks `moshimo_posted_date:{biz_date}` + timestamp.
  2. When a Moshimo episode has published on `biz_date` (or on `Mon–Thu` / `Sun` while the
     Moshimo campaign still has unposted episodes), the `22:50` late-night auto-reel slot is
     suppressed so each Moshimo Reel enjoys an uncontested 24-hour discovery window.
  3. On `Fri`/`Sat` nights with no due Moshimo episode, or automatically once all queued
     Moshimo episodes have finished, scheduled jobs seamlessly fall back to live real-time
     Reels (`instagram_poster.publish_live_reel_to_instagram`).
  4. Optional Evergreen Winner Recycle (`moshimo_evergreen_recycle_enabled`): once all fresh
     episodes have aired and `>= 21` days have elapsed since an episode's last post, `Tue/Thu`
     `20:00` slots can automatically re-air top Moshimo episodes while `Mon/Wed/Fri/Sat` run
     live data Reels.
"""

import datetime
import json
import logging
import os

import instagram_poster
import job_state
import reel_generator

_JST = datetime.timezone(datetime.timedelta(hours=9))
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

MOSHIMO_QUEUE_ENABLED_KEY = "moshimo_reel_queue_enabled"
MOSHIMO_EVERGREEN_CYCLE_KEY = "moshimo_evergreen_recycle_enabled"
MOSHIMO_CUSTOM_EPISODES_KEY = "moshimo_custom_episodes_v1"
MOSHIMO_EPISODE_DONE_PREFIX = "moshimo_reel_posted:"
MOSHIMO_EPISODE_LAST_TS_PREFIX = "moshimo_reel_last_ts:"
MOSHIMO_DATE_POSTED_PREFIX = "moshimo_posted_date:"

EVERGREEN_COOLDOWN_DAYS = 21

MOSHIMO_EPISODES = (
    {
        "ep_id": "ep1_vanished",
        "order": 1,
        "scheduled_date": "2026-11-14",
        "title": "もし日本から相席ラウンジが消えたら？",
        "youtube_title": "もし日本から相席ラウンジが消えたら？金曜23時の実測データで検証 #もしもシリーズ #雑学",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep1_vanished.mp4",
        "duration_sec": 25.7,
        "related_column_path": "/guide/crowd-rush-paradox",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "あなたの街の金曜夜はどこに人があふれそうですか？\n"
            "▼ 全国63店舗のリアルタイム男女比はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "金曜23時、梅田のビルの中だけで913人（女性512名・男性401名）。\n"
            "新宿の587人と合わせると、たった2つの街だけで1,500人が同じ時間にビルの上にいます。\n\n"
            "もし一斉に消えたら、あなたの街の金曜夜はどこに人があふれそうですか？\n"
            "※全国63店舗のリアルタイム男女比はプロフィールの『相席ナビ』から見られます\n"
            "#もしもシリーズ #相席ラウンジ #梅田 #新宿 #雑学 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep2_matching_app",
        "order": 2,
        "scheduled_date": "2026-11-16",
        "title": "もし日本からマッチングアプリが消えたら？",
        "youtube_title": "もし日本からマッチングアプリが消えたら？夜の街に起きること #もしもシリーズ #マッチングアプリ",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep2_matching_app.mp4",
        "duration_sec": 30.0,
        "related_column_path": "/guide/app-fatigue-vs-lounge",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "アプリが消えたら街に出ますか？それとも何もしない派ですか？\n"
            "▼ 今の各ビルのリアル人数はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "国の調査（こども家庭庁）では、直近で結婚したカップルの4組に1組（25.1%）がアプリきっかけ。\n"
            "もし今夜アプリが消えたら、みんな一斉にリアルな街へ出ると思いますか？それとも何もしない派ですか？\n\n"
            "実はアプリがある今でも、週末ピークのビルの上には同時3,000人規模が集まっています。\n"
            "※今の各ビルのリアル人数はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #マッチングアプリ #恋愛データ #雑学 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep3_women_free",
        "order": 3,
        "scheduled_date": "2026-11-17",
        "title": "もし相席の「女性無料」が有料になったら？",
        "youtube_title": "もし相席ラウンジの「女性無料」が有料になったらどうなる？ #もしもシリーズ #相席ラウンジ",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep3_women_free.mp4",
        "duration_sec": 44.4,
        "related_column_path": "/guide/free-food-reality",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "男だけの卓になった夜、ある人いますか？（いま男4人できっちり割り勘中…）\n"
            "▼ 全国63店舗のリアルタイム男女比はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "過去30日の実測データでは、JIS（ジス）梅田（63.9%）やJIS（ジス）新宿（64.0%）など人気店は女性比率6割超え。\n"
            "もし「男女完全同額」になったら、お店はどうなると思いますか？\n\n"
            "『これで平等だ』と喜んだ彼はいま、男4人できっちり割り勘中。\n"
            "女性0円は優遇ではなく、二面市場の男女バランスを保つための仕組みです。\n\n"
            "男だけの卓になった夜、ある人いますか？\n"
            "※全国63店舗のリアルタイム男女比はプロフィールの『相席ナビ』から見られます\n"
            "#もしもシリーズ #相席ラウンジ #男女比 #雑学 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep4_last_train",
        "order": 4,
        "scheduled_date": "2026-11-18",
        "title": "もし金曜の夜から「終電」が消えたら？",
        "youtube_title": "もし金曜の夜から「終電」が消えたら？深夜0時半に起きる逆転現象 #もしもシリーズ #終電",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep4_last_train.mp4",
        "duration_sec": 43.7,
        "related_column_path": "/guide/last-train-cliff",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "「終電なんで」に救われた夜、ありますか？（始発のホームで終電の優しさを噛み締めた人へ）\n"
            "▼ 深夜帯のリアルタイム人数波形はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "実測データを見ると、23時20分前後に店内の約3割が一斉に駅へ向かいます（終電崖）。\n"
            "もし終電が消えたら、最強の退店口実「終電なんで…」が使えなくなり、噛み合わない席から誰も立てない沈黙が訪れます。\n\n"
            "現実は、終電で帰る人と深夜0時半から合流する第2波がきれいに入れ替わっています。\n\n"
            "「終電なんで」に救われた夜、ありますか？\n"
            "※深夜帯のリアルタイム人数波形はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #終電 #金曜の夜 #雑学 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep5_crowd_paradox",
        "order": 5,
        "scheduled_date": "2026-11-19",
        "title": "もし全員が同じ「女余りの店」に向かったら？",
        "youtube_title": "もし全員が同じ「女余りの店」を見て向かったら60分後に起きる逆転 #もしもシリーズ #相席ナビ",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep5_crowd_paradox.mp4",
        "duration_sec": 40.8,
        "related_column_path": "/guide/crowd-rush-paradox",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "着いたら男だらけだった店、どこでしたか？（※この動画がバズった夜については責任を負いかねます）\n"
            "▼ 60分後予測＆徒歩4分の近隣比較はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "金曜22時に「女性60%」の看板店を見て向かうと、実測ではなんと約3割（29.8%）の店が60分後に『男余り』へ逆転します。\n"
            "みんなが同じ画面を見て1店舗に集中する「群衆殺到パラドックス」です。\n\n"
            "一方、そこから徒歩4分の別ブランド店とは最大25ポイントもの差が開きます。\n"
            "※この動画がバズった夜のことは知りません。\n\n"
            "着いたら男だらけだった店、どこでしたか？\n"
            "※60分後の予測＆徒歩圏内の穴場比較はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #相席ラウンジ #データ分析 #雑学 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep6_rainy_payday",
        "order": 6,
        "scheduled_date": "2026-11-20",
        "title": "もし給料日前の「雨の夜」に街へ出たら？",
        "youtube_title": "もし給料日前の「雨の夜」に街へ出たら？9.6万件実測で判明した穴場 #もしもシリーズ #雑学",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep6_rainy_payday.mp4",
        "duration_sec": 44.9,
        "related_column_path": "/guide/rain-payday",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "いちばん条件のいい夜は、いちばん出かけたくない夜でした。雨の給料日前に行ったことある人いますか？\n"
            "▼ 雨天の駅直結店ランキングはチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "毎月15〜24日の給料日前、9.6万件の実測ログでは給料日直後（54.7%）より女性比率が62.1%（+7.4pt）まで上がります。\n"
            "さらに雨の夜は前髪を守りたい女性がオリエンタルラウンジ新宿（72.7%）やJIS梅田（64.7%）など地下直結ビルへ集中。\n\n"
            "その頃、男性陣は家で残高と天気予報を確認中…いちばん条件のいい夜は、いちばん出かけたくない夜でした。\n"
            "※雨の夜のリアルタイム男女比はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #雨の日 #給料日前 #データ分析 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep7_dome_live",
        "order": 7,
        "scheduled_date": "2026-11-23",
        "title": "もし5万人のドーム公演が一斉に終わったら？",
        "youtube_title": "もし5万人のドームライブが一斉に終わったら？終演45分後の街データ #もしもシリーズ #ライブ",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep7_dome_live.mp4",
        "duration_sec": 42.9,
        "related_column_path": "/guide/dome-live-after-surge",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "この夜に効くひとことは、ひとつだけ。「誰推しですか？」（ライブ帰りの熱狂に遭遇した夜、ありますか）\n"
            "▼ 6大ドーム日程×現在人数はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "ドームライブが20時半に終わる夜、会場まわりの居酒屋が満席になった45分後、感想を語りたい女性2人組が梅田や新宿の大型ラウンジへ合流します。\n"
            "実測ピークではJIS梅田で女性206名を記録。\n\n"
            "ただし彼女たちが話したいのは今日のセットリスト。この夜に効くひとことはひとつだけ、「誰推しですか」。\n"
            "※ドーム公演連動の現在人数はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #ドームライブ #推し活 #梅田 #新宿 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep8_no_change",
        "order": 8,
        "scheduled_date": "2026-11-24",
        "title": "もし相席ラウンジから「席替え」が消えたら？",
        "youtube_title": "もし相席ラウンジから「席替え」が消えたら？110分粘る人vs45分の損益分岐点 #もしもシリーズ",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep8_no_change.mp4",
        "duration_sec": 39.6,
        "related_column_path": "/guide/cost-break-even",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "相席でいちばん長かった沈黙、教えてください（相手のお仕事、3回聞いてまだ覚えてない人へ）\n"
            "▼ 今の女性人数＆2軒目マップはチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "席替えシステムが消えると、噛み合わない席でも110分粘ってお会計は約9,800円。\n"
            "「お仕事、何されてるんですか？」は本日3回目、終電の改札でそのまま解散します。\n\n"
            "現実の損益分岐点は着席から45分前後。45分で夜カフェへ移れば約6,400円と3,000円以上お得です。\n"
            "ちなみに110分座った彼、相手のお仕事をまだ覚えていません。\n"
            "※全店舗の時間帯料金＆現在人数比較はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #相席ラウンジ #コスパ #損益分岐点 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep9_sunday_night",
        "order": 9,
        "scheduled_date": "2026-11-25",
        "title": "もし日曜21時半に会社員が一斉に帰ったら？",
        "youtube_title": "もし日曜21時半に会社員が一斉に帰ったら？22時から始まる逆転現象 #もしもシリーズ #日曜の夜",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep9_sunday_night.mp4",
        "duration_sec": 43.5,
        "related_column_path": "/guide/sunday-lounge-strategy",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "谷間の30分に入った経験、ありますか？（日曜の会社員の魔法はシンデレラより2時間半早く解けます）\n"
            "▼ 曜日・時間別の人数波形はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "日曜夜の平均女性比率は61.2%と高水準ですが、金曜より2時間早い「21時30分」に会社員が一斉に駅へ帰宅します。\n"
            "日曜の会社員の魔法はシンデレラより2時間半早く解け、22時からは月曜休みの美容師・アパレル層が合流。\n\n"
            "谷間の30分に入った人だけが、誰の金曜にも間に合いません。\n"
            "※日曜夜のリアルタイム人数波形はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #日曜の夜 #美容師の休日 #データ分析 #相席ナビ"
        ),
    },
    {
        "ep_id": "ep10_solo_night",
        "order": 10,
        "scheduled_date": "2026-11-26",
        "title": "もし夜の街に「1人参戦」だけで入ったら？",
        "youtube_title": "もし夜の街に「1人参戦」だけで入ったら？全国43店舗の意外な実態 #もしもシリーズ #ひとり飲み",
        "static_rel_path": "/static/reels/moshimo/moshimo_ep10_solo_night.mp4",
        "duration_sec": 42.8,
        "related_column_path": "/guide/solo-vs-pair",
        "thumb_offset_ms": 500,
        "pinned_comment": (
            "今夜は彼が、既読スルーする番です。1人参戦したことある人いますか？\n"
            "▼ 1名OK店舗＆リアルタイム人数はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から"
        ),
        "caption": (
            "予定が空いてふらっと1人で向かうと、一部店舗は2名以上限定で門前払い。\n"
            "誘える友達がいたらそもそも1人で来ていませんが、実はJIS全9店・オリエンタルラウンジ24店・ASOBIBAR全10店など計43店舗は1名入店OK。\n\n"
            "相方に気を使わず自分のペースで動けるソロは身軽。深夜に届いた友達からの「ごめん寝てた」には、既読スルーする番です。\n"
            "※1名入店OK店舗と今の男女比はプロフィールの『相席ナビ』から\n"
            "#もしもシリーズ #ひとり飲み #出張 #相席ラウンジ #相席ナビ"
        ),
    },
)

_EPISODE_MAP = {ep["ep_id"]: ep for ep in MOSHIMO_EPISODES}


def _normalize_now_jst(now_jst: datetime.datetime = None) -> datetime.datetime:
    if now_jst is None:
        return datetime.datetime.now(_JST)
    if now_jst.tzinfo is None:
        return now_jst.replace(tzinfo=_JST)
    return now_jst.astimezone(_JST)


def _business_dt(now_jst: datetime.datetime = None) -> datetime.datetime:
    return _normalize_now_jst(now_jst) - datetime.timedelta(hours=7)


def _next_weekday_after(date_str: str) -> str:
    """Return the next `Mon–Fri` `YYYY-MM-DD` strictly after `date_str`."""
    try:
        d = datetime.date.fromisoformat(str(date_str or "").strip()[:10])
    except Exception:
        d = datetime.datetime.now(_JST).date()
    d = d + datetime.timedelta(days=1)
    while d.weekday() >= 5:  # Skip Sat (5) & Sun (6)
        d = d + datetime.timedelta(days=1)
    return d.isoformat()


def _load_custom_episodes(allow_blocking: bool = False) -> list:
    raw = job_state.get_value(MOSHIMO_CUSTOM_EPISODES_KEY, allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict) and x.get("ep_id") and x.get("static_rel_path")]
    except Exception:
        pass
    return []


def get_all_moshimo_episodes(allow_blocking: bool = False) -> list:
    """Combine built-in episodes (`#1..#10`) with any validated custom episodes added via `job_state`."""
    import re as _re
    _valid_rel_re = _re.compile(r"^/static/reels/moshimo/[A-Za-z0-9_.-]+\.mp4$")
    out = [dict(ep) for ep in MOSHIMO_EPISODES]
    seen = {ep["ep_id"] for ep in out}
    last_date = out[-1]["scheduled_date"] if out else "2026-11-26"
    next_order = len(out) + 1
    for c_ep in _load_custom_episodes(allow_blocking=allow_blocking):
        try:
            eid = str(c_ep.get("ep_id") or "").strip()
            rel_p = str(c_ep.get("static_rel_path") or "").strip()
            if not eid or eid in seen or not _valid_rel_re.match(rel_p):
                continue
            sched = str(c_ep.get("scheduled_date") or "").strip()[:10]
            if not sched:
                sched = _next_weekday_after(last_date)
            order_val = int(c_ep.get("order") or next_order)
            dur_raw = c_ep.get("duration_sec")
            dur_val = float(dur_raw) if dur_raw is not None else 0.0
            thumb_val = int(c_ep.get("thumb_offset_ms") or 500)
        except (TypeError, ValueError):
            continue
        seen.add(eid)
        last_date = max(last_date, sched)
        item = {
            "ep_id": eid,
            "order": order_val,
            "scheduled_date": sched,
            "title": str(c_ep.get("title") or eid),
            "youtube_title": str(c_ep.get("youtube_title") or f"{c_ep.get('title') or eid} #もしもシリーズ #雑学"),
            "static_rel_path": rel_p,
            "duration_sec": dur_val,
            "related_column_path": str(c_ep.get("related_column_path") or "/columns"),
            "thumb_offset_ms": thumb_val,
            "pinned_comment": str(c_ep.get("pinned_comment") or ""),
            "caption": str(c_ep.get("caption") or ""),
        }
        out.append(item)
        next_order += 1
    return out


def build_youtube_shorts_description(ep: dict) -> str:
    """Build ready-to-paste YouTube Shorts description / pinned comment with `@aiseki_navi` profile cue."""
    base_cap = str(ep.get("caption") or "").strip()
    col_path = str(ep.get("related_column_path") or "").strip()
    lines = [
        base_cap,
        "",
        "▼ 全国63店舗のリアルタイム男女比・待ち時間予測はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から見られます",
    ]
    if col_path and col_path.startswith("/"):
        lines.append(f"（データ検証コラム：相席ナビ内 {col_path} ）")
    return "\n".join(lines).strip()


def build_youtube_shorts_pinned_comment(ep: dict) -> str:
    """Build ready-to-paste YouTube Shorts pinned comment (Fable 5.2 engagement prompt + bio link)."""
    pinned = str(ep.get("pinned_comment") or "").strip()
    if pinned:
        return pinned
    return (
        "▼ 全国63店舗のリアルタイム男女比・待ち時間予測はチャンネルトップ（@aiseki_navi）のリンク『相席ナビ』から見られます"
    )


def resolve_episode_local_path(ep: dict) -> str:
    rel = str((ep or {}).get("static_rel_path") or "").lstrip("/")
    allowed_root = os.path.realpath(os.path.join(_REPO_DIR, "static", "reels", "moshimo"))
    cand = os.path.realpath(os.path.join(_REPO_DIR, rel))
    if not (cand.startswith(allowed_root + os.sep) and cand.lower().endswith(".mp4")):
        return os.path.join(allowed_root, "__invalid_path__.mp4")
    return cand


def is_moshimo_queue_enabled(allow_blocking: bool = False) -> bool:
    raw = job_state.get_value(MOSHIMO_QUEUE_ENABLED_KEY, allow_blocking=allow_blocking)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    env_raw = (os.environ.get("MOSHIMO_REEL_QUEUE_ENABLED") or "1").strip().lower()
    return env_raw in ("1", "true", "on", "yes")


def set_moshimo_queue_enabled(enabled: bool) -> bool:
    val = bool(enabled)
    job_state.set_value(MOSHIMO_QUEUE_ENABLED_KEY, "1" if val else "0")
    return val


def is_evergreen_recycle_enabled(allow_blocking: bool = False) -> bool:
    raw = job_state.get_value(MOSHIMO_EVERGREEN_CYCLE_KEY, allow_blocking=allow_blocking)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    env_raw = (os.environ.get("MOSHIMO_EVERGREEN_RECYCLE_DEFAULT") or "0").strip().lower()
    return env_raw in ("1", "true", "on", "yes")


def set_evergreen_recycle_enabled(enabled: bool) -> bool:
    val = bool(enabled)
    job_state.set_value(MOSHIMO_EVERGREEN_CYCLE_KEY, "1" if val else "0")
    return val


def is_episode_posted(ep_id: str) -> bool:
    return job_state.is_done(f"{MOSHIMO_EPISODE_DONE_PREFIX}{ep_id}")


def get_episode_last_posted_ts(ep_id: str, allow_blocking: bool = False) -> str:
    return str(job_state.get_value(f"{MOSHIMO_EPISODE_LAST_TS_PREFIX}{ep_id}", allow_blocking=allow_blocking) or "").strip()


def has_moshimo_posted_on_biz_date(biz_date: str) -> bool:
    return job_state.is_done(f"{MOSHIMO_DATE_POSTED_PREFIX}{biz_date}")


def get_episode_by_id(ep_id: str) -> dict:
    key = (ep_id or "").strip()
    all_eps = get_all_moshimo_episodes(allow_blocking=False)
    for ep in all_eps:
        if ep["ep_id"] == key:
            return dict(ep)
    for ep in all_eps:
        if key in (str(ep["order"]), f"ep{ep['order']}"):
            return dict(ep)
    return None


def get_moshimo_queue_status(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> dict:
    now_jst = _normalize_now_jst(now_jst)
    biz_dt = _business_dt(now_jst)
    biz_date = biz_dt.strftime("%Y-%m-%d")
    all_eps = get_all_moshimo_episodes(allow_blocking=allow_blocking)
    items = []
    unposted_count = 0
    posted_count = 0
    yt_posted_count = 0
    latest_posted_ts = ""
    next_ep_summary = None

    yt_auto_status = {}
    try:
        import youtube_poster
        yt_auto_status = youtube_poster.get_youtube_auto_post_status(allow_blocking=allow_blocking)
    except Exception:
        yt_auto_status = {}

    for ep in all_eps:
        posted = is_episode_posted(ep["ep_id"])
        last_ts = get_episode_last_posted_ts(ep["ep_id"], allow_blocking=allow_blocking)
        if last_ts and last_ts > latest_posted_ts:
            latest_posted_ts = last_ts
        yt_done, yt_vid, yt_ts = False, "", ""
        yt_comm_done, yt_comm_id = False, ""
        try:
            import youtube_poster
            yt_done, yt_vid, yt_ts = youtube_poster.find_synced_youtube_video_for_episode(ep, allow_blocking=allow_blocking)
            yt_comm_done, yt_comm_id, _ = youtube_poster.is_episode_youtube_commented(ep["ep_id"], allow_blocking=allow_blocking)
        except Exception:
            yt_done, yt_vid, yt_ts = False, "", ""
            yt_comm_done, yt_comm_id = False, ""
        if yt_done:
            yt_posted_count += 1
        local_path = resolve_episode_local_path(ep)
        file_exists = os.path.isfile(local_path)
        file_bytes = os.path.getsize(local_path) if file_exists else 0
        yt_title = str(ep.get("youtube_title") or f"{ep['title']} #もしもシリーズ #雑学")
        yt_desc = build_youtube_shorts_description(ep)
        yt_pinned = build_youtube_shorts_pinned_comment(ep)
        entry = {
            "ep_id": ep["ep_id"],
            "order": ep["order"],
            "scheduled_date": ep["scheduled_date"],
            "title": ep["title"],
            "youtube_title": yt_title,
            "youtube_description": yt_desc,
            "youtube_pinned_comment": yt_pinned,
            "caption": ep["caption"],
            "duration_sec": float(ep.get("duration_sec") or 30.0),
            "related_column_path": str(ep.get("related_column_path") or "/columns"),
            "static_rel_path": ep["static_rel_path"],
            "video_url": f"{instagram_poster.SITE_URL}{ep['static_rel_path']}?v={ep['ep_id']}",
            "posted": posted,
            "last_posted_at": last_ts,
            "yt_posted": yt_done,
            "yt_video_id": yt_vid,
            "yt_shorts_url": f"https://www.youtube.com/shorts/{yt_vid}" if yt_vid else "",
            "yt_last_posted_at": yt_ts,
            "yt_commented": yt_comm_done,
            "yt_comment_id": yt_comm_id,
            "file_exists": file_exists,
            "file_size_mb": round(file_bytes / (1024 * 1024), 2) if file_bytes else 0.0,
        }
        if posted:
            posted_count += 1
        else:
            unposted_count += 1
            if next_ep_summary is None and file_exists:
                next_ep_summary = entry
        items.append(entry)

    due = select_due_moshimo_episode(now_jst=now_jst)
    evergreen_on = is_evergreen_recycle_enabled(allow_blocking=allow_blocking)
    return {
        "enabled": is_moshimo_queue_enabled(allow_blocking=allow_blocking),
        "evergreen_recycle": evergreen_on,
        "biz_date": biz_date,
        "posted_today": has_moshimo_posted_on_biz_date(biz_date),
        "total_episodes": len(all_eps),
        "posted_count": posted_count,
        "youtube_posted_count": yt_posted_count,
        "youtube_auto_post": yt_auto_status,
        "remaining_episodes": unposted_count,
        "remaining_count": unposted_count,
        "last_posted_at": latest_posted_ts,
        "runway_low": unposted_count <= 2,
        "due_today": due["ep_id"] if due else None,
        "next_episode": next_ep_summary,
        "youtube_bio_url": f"{instagram_poster.SITE_URL}/?s=short_bio",
        "episodes": items,
    }


def any_unposted_moshimo_episodes() -> bool:
    for ep in get_all_moshimo_episodes(allow_blocking=False):
        if not is_episode_posted(ep["ep_id"]) and os.path.isfile(resolve_episode_local_path(ep)):
            return True
    return False


def _select_evergreen_recycle_episode(biz_dt: datetime.datetime) -> dict:
    """
    When all queued episodes have been posted at least once and `evergreen_recycle` is ON,
    select the oldest-aired episode on `Tue` (`weekday == 1`) or `Thu` (`weekday == 3`)
    provided `>= EVERGREEN_COOLDOWN_DAYS` (`21` days) have elapsed since its last post.
    """
    if biz_dt.weekday() not in (1, 3):
        return None
    biz_date_obj = biz_dt.date()
    candidates = []
    for ep in get_all_moshimo_episodes(allow_blocking=False):
        if not os.path.isfile(resolve_episode_local_path(ep)):
            continue
        last_ts = get_episode_last_posted_ts(ep["ep_id"], allow_blocking=False)
        ref_date_str = (last_ts[:10] if len(last_ts) >= 10 else ep.get("scheduled_date") or "")
        try:
            ref_date = datetime.date.fromisoformat(ref_date_str)
        except Exception:
            continue
        age_days = (biz_date_obj - ref_date).days
        if age_days >= EVERGREEN_COOLDOWN_DAYS:
            candidates.append((ref_date, ep["order"], ep))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    chosen = dict(candidates[0][2])
    chosen["is_evergreen_recycle"] = True
    return chosen


def select_due_moshimo_episode(now_jst: datetime.datetime = None) -> dict:
    """
    Return the Moshimo episode that should publish on `biz_date`'s `20:00` JST slot, or `None`:
      1. Never publish more than 1 Moshimo episode on the same `biz_date`.
      2. Exact match (`scheduled_date == biz_date`) wins immediately.
      3. Catch-up fallback: if an earlier episode (`scheduled_date < biz_date`) was missed due to a
         transient outage, catch it up on `Mon–Fri` (`weekday in 0..4`), skipping Sunday rest days (`weekday == 6`).
      4. Optional Evergreen Recycle: once all episodes have aired and `evergreen_recycle` is enabled,
         re-air eligible episodes (`>= 21d` since last post) on `Tue/Thu`.
    """
    biz_dt = _business_dt(now_jst)
    biz_date = biz_dt.strftime("%Y-%m-%d")
    biz_weekday = biz_dt.weekday()

    if has_moshimo_posted_on_biz_date(biz_date):
        return None

    all_eps = get_all_moshimo_episodes(allow_blocking=False)

    # 1. Exact scheduled date match
    for ep in all_eps:
        if ep["scheduled_date"] == biz_date and not is_episode_posted(ep["ep_id"]):
            if os.path.isfile(resolve_episode_local_path(ep)):
                return dict(ep)

    # 2. Overdue catch-up on Mon–Fri (never on Sunday rest day)
    if biz_weekday in (0, 1, 2, 3, 4):
        for ep in all_eps:
            if ep["scheduled_date"] < biz_date and not is_episode_posted(ep["ep_id"]):
                if os.path.isfile(resolve_episode_local_path(ep)):
                    return dict(ep)

    # 3. Post-campaign Evergreen Recycle on Tue/Thu when enabled
    if not any_unposted_moshimo_episodes() and is_evergreen_recycle_enabled(allow_blocking=False):
        recycled = _select_evergreen_recycle_episode(biz_dt)
        if recycled is not None:
            return recycled

    return None


def publish_moshimo_episode_to_instagram(
    ep_id: str = None,
    slot_label: str = "20:00",
    share_to_feed: bool = True,
    custom_caption: str = "",
    force: bool = False,
    allow_recycle: bool = False,
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Publish a specific (or next due) 3D Voxel Moshimo episode via Instagram Graph API,
    record delivery + Insights Loop metadata (`reel_type="moshimo_3d"`), and mark completion.
    """
    if not instagram_poster._ig_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        now_jst = _normalize_now_jst(now_jst)
        biz_date = _business_dt(now_jst).strftime("%Y-%m-%d")
        slot_dedup_key = f"ig_reel_slot:{biz_date}_{slot_label}"

        if not force:
            if not instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False):
                return {"status": "skipped", "ok": False, "reason": "Reel auto-schedule is disabled"}
            if not is_moshimo_queue_enabled(allow_blocking=False):
                return {"status": "skipped", "ok": False, "reason": "Moshimo Reel queue is disabled"}
            if job_state.is_done(slot_dedup_key):
                return {"status": "skipped", "ok": False, "reason": "Already posted scheduled Reel for this slot"}
            in_cd, cd_reason = instagram_poster.check_ig_auto_post_cooldown(min_interval_minutes=95, now_jst=now_jst)
            if in_cd:
                logging.info(f"[moshimo_reel_queue] Deferring Moshimo IG Reel ({slot_label}): {cd_reason}")
                return {"status": "skipped", "ok": False, "reason": cd_reason}

        if ep_id:
            ep = get_episode_by_id(ep_id)
        else:
            ep = select_due_moshimo_episode(now_jst=now_jst)
            if ep is None and force:
                for cand in get_all_moshimo_episodes(allow_blocking=False):
                    if not is_episode_posted(cand["ep_id"]) and os.path.isfile(resolve_episode_local_path(cand)):
                        ep = dict(cand)
                        break

        if not ep:
            return {"status": "skipped", "ok": False, "reason": "No matching or due Moshimo episode found"}

        resolved_ep_id = ep["ep_id"]
        is_recycle = bool(allow_recycle or ep.get("is_evergreen_recycle"))
        ep_done_key = f"{MOSHIMO_EPISODE_DONE_PREFIX}{resolved_ep_id}"
        if not force and not is_recycle and job_state.is_done(ep_done_key):
            return {
                "status": "skipped",
                "ok": False,
                "reason": f"Moshimo episode '{resolved_ep_id}' was already posted",
                "ep_id": resolved_ep_id,
            }

        local_path = resolve_episode_local_path(ep)
        if not os.path.isfile(local_path):
            err_detail = f"Moshimo MP4 file missing on disk: {ep['static_rel_path']}"
            logging.error(f"[moshimo_reel_queue] {err_detail}")
            return {"status": "error", "ok": False, "detail": err_detail, "ep_id": resolved_ep_id}

        instagram_poster.refresh_ig_token_if_needed(force=False)
        raw_cap = (custom_caption or ep["caption"] or "").strip()
        caption = reel_generator.enforce_brand_rule(raw_cap)
        video_url = f"{instagram_poster.SITE_URL}{ep['static_rel_path']}?v={resolved_ep_id}"
        thumb_ms = int(ep.get("thumb_offset_ms", 500))
        eff_share_feed = bool(share_to_feed)
        style_tag = f"moshimo_{resolved_ep_id}"

        res = instagram_poster.post_reel_to_instagram(
            video_url,
            caption,
            share_to_feed=eff_share_feed,
            thumb_offset_ms=thumb_ms,
        )
        res["caption"] = caption
        res["video_url"] = video_url
        res["ep_id"] = resolved_ep_id
        res["title"] = ep["title"]
        res["reel_type"] = style_tag
        res["share_to_feed"] = eff_share_feed
        res["is_evergreen_recycle"] = is_recycle
        res.setdefault("ok", res.get("status") == "success")

        try:
            import sns_editorial
            if res.get("status") == "success":
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    "success",
                    slot_label=slot_label,
                    post_id=res.get("id") or "",
                    now_jst=now_jst,
                )
                try:
                    import sns_insights_loop
                    sns_insights_loop.register_published_post(
                        channel="reels",
                        post_id=str(res.get("id") or ""),
                        text=caption,
                        style=style_tag,
                        reel_type="moshimo_3d",
                        slot_label=slot_label,
                        s_param=style_tag,
                        now_jst=now_jst,
                    )
                except Exception:
                    pass
                sns_editorial.persist_editorial_session_record("instagram", {
                    "post_id": str(res.get("id") or ""),
                    "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                    "slot_label": slot_label,
                    "selected_style": style_tag,
                    "reel_type": "moshimo_3d",
                    "selected_text": caption,
                })
            else:
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    res.get("status") or "error",
                    slot_label=slot_label,
                    detail=res.get("detail") or res.get("reason") or "",
                    now_jst=now_jst,
                )
        except Exception:
            pass

        if res.get("status") == "success":
            job_state.mark_done(ep_done_key)
            job_state.mark_done(f"{MOSHIMO_DATE_POSTED_PREFIX}{biz_date}")
            job_state.set_value(
                f"{MOSHIMO_EPISODE_LAST_TS_PREFIX}{resolved_ep_id}",
                now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if not force:
                job_state.mark_done(slot_dedup_key)
            instagram_poster.record_ig_auto_post_timestamp(
                now_jst,
                is_feed=eff_share_feed,
                reel_type=style_tag if eff_share_feed else "",
            )
            logging.info(
                f"[moshimo_reel_queue] Published Moshimo Reel '{resolved_ep_id}' ({ep['title']}) -> post_id={res.get('id')}"
            )
        return res
    finally:
        instagram_poster._ig_post_lock.release()


def dispatch_scheduled_ig_reel(
    stores: list,
    slot_label: str = "20:00",
    is_seed: bool = False,
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Unified scheduled Instagram Reel dispatcher called by `worker.py` and `app.py`:
      - Prioritizes due Moshimo episodes at `20:00` JST.
      - Suppresses `22:50` late-night auto-reels on days a Moshimo Reel ran (and Mon–Thu / Sun during the campaign).
      - Preserves Sunday as an algorithm rest day while Moshimo episodes remain queued.
      - Falls back cleanly to `instagram_poster.publish_live_reel_to_instagram` on Fri/Sat nights with no
        Moshimo scheduled, or once all queued Moshimo episodes have been published.
    """
    now_jst = _normalize_now_jst(now_jst)
    biz_dt = _business_dt(now_jst)
    biz_date = biz_dt.strftime("%Y-%m-%d")
    biz_weekday = biz_dt.weekday()

    queue_on = is_moshimo_queue_enabled(allow_blocking=False)
    has_remaining = any_unposted_moshimo_episodes() if queue_on else False
    is_late_slot = instagram_poster._is_late_night_reel_slot(slot_label)

    # 1. Anti-cannibalization guard on 22:50 late-night slot
    if is_late_slot:
        if has_moshimo_posted_on_biz_date(biz_date):
            reason = f"Suppressed {slot_label} auto-reel because Moshimo Reel already posted on {biz_date}"
            logging.info(f"[moshimo_reel_queue] {reason}")
            return {"status": "skipped", "ok": False, "skipped": True, "reason": reason}
        if queue_on and has_remaining and biz_weekday in (0, 1, 2, 3, 6):
            reason = f"Suppressed {slot_label} weekday/Sunday auto-reel during active Moshimo 5x/week campaign"
            logging.info(f"[moshimo_reel_queue] {reason}")
            return {"status": "skipped", "ok": False, "skipped": True, "reason": reason}

    # 2. Primary 20:00 slot Moshimo dispatch
    if not is_late_slot and queue_on:
        due_ep = select_due_moshimo_episode(now_jst=now_jst)
        if due_ep is not None:
            return publish_moshimo_episode_to_instagram(
                ep_id=due_ep["ep_id"],
                slot_label=slot_label,
                share_to_feed=True,
                force=False,
                allow_recycle=bool(due_ep.get("is_evergreen_recycle")),
                now_jst=now_jst,
            )
        if has_remaining and biz_weekday == 6:
            reason = "Sunday rest day during active Moshimo 5x/week campaign"
            logging.info(f"[moshimo_reel_queue] {reason}")
            return {"status": "skipped", "ok": False, "skipped": True, "reason": reason}

    # 3. Fallback to live real-time data Reel (Fri/Sat live surge or post-campaign)
    if not stores or is_seed:
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "No visible non-seed live stores"}
    return instagram_poster.publish_live_reel_to_instagram(
        stores,
        reel_type="auto",
        slot_label=slot_label,
        force=False,
        now_jst=now_jst,
    )
