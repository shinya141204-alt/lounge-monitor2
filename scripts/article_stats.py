#!/usr/bin/env python3
"""コラム加筆用の実データ集計スクリプト（再現用）

対象記事:
  /guide/weekend-strategy      → weekend セクション
  /guide/club-area-midnight    → club セクション
  /guide/first-10-minutes      → first10 セクション

使い方（本番 DB で実行する場合）:
  flyctl ssh console -a aiseki-navi-tokyo -C "python3 /app/scripts/article_stats.py --days 56"
ローカル SQLite でも同じコマンドで動く（local_db の接続先を使う）。

集計ルール:
  - monitor_logs（日時・店舗名・男性人数・女性人数）のみを使う
  - 男女とも 0 名の記録は除外（営業時間外・取得失敗）
  - 0時〜6時台は前日の営業日として扱う
  - 女性比率は男女合計 6 名以上（エリア比較は 10 名以上）の記録のみ
  - 韓国店舗（ソウル）は除外
"""
import argparse
import datetime
import json
import os
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('DISABLE_SHEETS', '1')

import local_db  # noqa: E402

WD = ['月', '火', '水', '木', '金', '土', '日']
HOURS = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3]
WEEKEND = (4, 5)

WALK_CLUSTERS = {
    '新宿': ['OLG 新宿', 'JIS SHINJUKU', 'JIS NISHISHINJUKU'],
    '渋谷': ['OLG 渋谷本店', 'OLG ag渋谷'],
    '梅田・茶屋町': ['OLG 大阪駅前', 'OLG ag梅田', 'JIS UMEDA', 'JIS CHAYAMACHI'],
    '名古屋 栄・錦': ['OLG 名古屋 栄', 'OLG 名古屋 錦', 'OLG ag名古屋'],
    '福岡 天神': ['OLG 福岡', 'JIS FUKUOKA'],
}
CLUB_AREA_STORES = {
    'JIS UMEDA', 'OLG 大阪駅前', 'OLG ag梅田', 'JIS CHAYAMACHI', 'YATAKOI UMEDA',
    'OLG 心斎橋', 'OLG 難波', 'JIS NAMBA',
    'OLG 渋谷本店', 'OLG ag渋谷', 'OLG 恵比寿',
    'JIS SHINJUKU', 'OLG 新宿', 'JIS NISHISHINJUKU',
    'OLG ag名古屋', 'OLG 名古屋 栄', 'OLG 名古屋 錦',
    'JIS FUKUOKA', 'OLG 福岡', 'JIS SAPPORO',
}


def _mean(v):
    return sum(v) / len(v) if v else None


def _r(x, n=1):
    return None if x is None else round(x, n)


def _parse(ts):
    s = str(ts)
    try:
        return datetime.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]))
    except (ValueError, TypeError):
        return None


def load_rows(store, start_ts):
    rows = local_db._fetch_all_rows(
        'SELECT timestamp, women, men FROM monitor_logs WHERE store_name = ? AND timestamp >= ? ORDER BY timestamp ASC',
        (store, start_ts),
    )
    out = []
    for ts, w, m in rows:
        dt = _parse(ts)
        if dt is None:
            continue
        w, m = int(w or 0), int(m or 0)
        if w + m <= 0:
            continue
        biz = (dt - datetime.timedelta(days=1)).date() if dt.hour <= 6 else dt.date()
        out.append((dt, biz, w, m))
    return out


def store_names(start_ts):
    rows = local_db._fetch_all_rows('SELECT DISTINCT store_name FROM monitor_logs WHERE timestamp >= ?', (start_ts,))
    return sorted(r[0] for r in rows if r[0] and 'ソウル' not in r[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=56)
    ap.add_argument('--out', default='')
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    start_ts = (now - datetime.timedelta(days=args.days)).strftime('%Y-%m-%d %H:%M:%S')

    names = store_names(start_ts)
    data = {}
    total_rows = 0
    for n in names:
        rows = load_rows(n, start_ts)
        if len(rows) >= 100:
            data[n] = rows
            total_rows += len(rows)
    all_biz = sorted({r[1] for rows in data.values() for r in rows})
    meta = {
        'generated_at': now.strftime('%Y-%m-%d %H:%M'),
        'days': args.days,
        'stores': len(data),
        'rows': total_rows,
        'biz_date_from': all_biz[0].isoformat() if all_biz else None,
        'biz_date_to': all_biz[-1].isoformat() if all_biz else None,
        'biz_days': len(all_biz),
    }

    # ---------- weekend ----------
    store_day_peak = []  # (store, biz, wd, peak_w, peak_m, peak_hour)
    for n, rows in data.items():
        by_day = defaultdict(list)
        for r in rows:
            by_day[r[1]].append(r)
        for d, lst in by_day.items():
            if len(lst) < 6:
                continue
            p = max(lst, key=lambda r: r[2])
            store_day_peak.append((n, d, d.weekday(), p[2], p[3], p[0].hour))
    wd_rows = []
    for wd in range(7):
        ps = [p for p in store_day_peak if p[2] == wd]
        if not ps:
            continue
        hidx = sorted(HOURS.index(p[5]) if p[5] in HOURS else (len(HOURS) if p[5] <= 6 else -1) for p in ps)
        mid = hidx[len(hidx) // 2]
        wd_rows.append({
            'wd': WD[wd], 'store_days': len(ps),
            'avg_peak_w': _r(_mean([p[3] for p in ps])), 'avg_peak_m': _r(_mean([p[4] for p in ps])),
            'median_peak_hour': HOURS[mid] if 0 <= mid < len(HOURS) else None,
        })
    hour_cmp = []
    for h in HOURS:
        def _agg(pred):
            obs = [(r[2], r[3]) for rows in data.values() for r in rows if r[0].hour == h and pred(r[1].weekday())]
            if len(obs) < 30:
                return None
            ratio_obs = [(w, m) for w, m in obs if w + m >= 6]
            return {
                'n': len(obs),
                'avg_w': _r(_mean([o[0] for o in obs])), 'avg_m': _r(_mean([o[1] for o in obs])),
                'men_first_pct': _r(sum(1 for w, m in ratio_obs if m > w) / len(ratio_obs) * 100, 0) if ratio_obs else None,
                'wpct': _r(_mean([w / (w + m) * 100 for w, m in ratio_obs]), 0) if ratio_obs else None,
            }
        hour_cmp.append({'hour': h, 'weekend': _agg(lambda wd: wd in WEEKEND), 'weekday': _agg(lambda wd: wd not in WEEKEND)})

    cluster_rows = []
    for cname, members in WALK_CLUSTERS.items():
        members = [m for m in members if m in data]
        if len(members) < 2:
            continue
        buckets = defaultdict(dict)
        for m in members:
            for dt, biz, w, mm in data[m]:
                if biz.weekday() not in WEEKEND or not (21 <= dt.hour <= 22) or w + mm < 10:
                    continue
                key = (dt.date(), dt.hour, dt.minute // 10)
                buckets[key][m] = (w, mm)
        full = [v for v in buckets.values() if len(v) == len(members)]
        if len(full) < 10:
            continue
        gaps = [max(w / (w + m) for w, m in v.values()) * 100 - min(w / (w + m) for w, m in v.values()) * 100 for v in full]
        flips = sum(1 for v in full if any(m > w for w, m in v.values()) and any(w > m for w, m in v.values()))
        cluster_rows.append({
            'area': cname, 'stores': len(members), 'buckets': len(full),
            'median_gap_pt': _r(statistics.median(gaps), 0),
            'gap15_pct': _r(sum(1 for g in gaps if g >= 15) / len(gaps) * 100, 0),
            'flip_pct': _r(flips / len(full) * 100, 0),
        })

    # ---------- club ----------
    store_ret = []
    for n, rows in data.items():
        by_day = defaultdict(list)
        for r in rows:
            by_day[r[1]].append(r)
        rets, rets_we, late = [], [], []
        for d, lst in by_day.items():
            pre = [r[2] for r in lst if r[0].hour in (21, 22)]
            post = [r[2] for r in lst if r[0].hour in (0, 1)]
            l23 = [r[2] for r in lst if r[0].hour in (2, 3)]
            if len(pre) >= 2 and _mean(pre) >= 3:
                if len(post) >= 2:
                    v = _mean(post) / _mean(pre) * 100
                    rets.append(v)
                    if d.weekday() in WEEKEND:
                        rets_we.append(v)
                if len(l23) >= 2:
                    late.append(_mean(l23) / _mean(pre) * 100)
        # weekend hourly profile for 2-peak detection
        prof = {}
        for h in [20, 21, 22, 23, 0, 1, 2, 3]:
            v = [r[2] for r in rows if r[0].hour == h and r[1].weekday() in WEEKEND]
            if len(v) >= 6:
                prof[h] = _mean(v)
        two_peak = False
        if all(h in prof for h in (21, 22, 23, 0, 1, 2)):
            first = max(prof[21], prof[22])
            trough = min(prof[23], prof[0])
            second = max(prof[1], prof[2], prof.get(3, 0))
            two_peak = trough < first * 0.95 and second >= trough * 1.05
        if len(rets) >= 4:
            store_ret.append({
                'store': n, 'club_area': n in CLUB_AREA_STORES, 'days': len(rets),
                'retention_pct': _r(statistics.median(rets), 0),
                'retention_weekend_pct': _r(statistics.median(rets_we), 0) if len(rets_we) >= 2 else None,
                'late_pct': _r(statistics.median(late), 0) if len(late) >= 4 else None,
                'two_peak_weekend': two_peak,
            })
    club = [s['retention_pct'] for s in store_ret if s['club_area']]
    non = [s['retention_pct'] for s in store_ret if not s['club_area']]
    club_we = [s['retention_weekend_pct'] for s in store_ret if s['club_area'] and s['retention_weekend_pct'] is not None]
    non_we = [s['retention_weekend_pct'] for s in store_ret if not s['club_area'] and s['retention_weekend_pct'] is not None]
    club_late = [s['late_pct'] for s in store_ret if s['club_area'] and s['late_pct'] is not None]
    non_late = [s['late_pct'] for s in store_ret if not s['club_area'] and s['late_pct'] is not None]
    club_summary = {
        'club_stores': len(club), 'other_stores': len(non),
        'club_median_retention': _r(statistics.median(club), 0) if club else None,
        'other_median_retention': _r(statistics.median(non), 0) if non else None,
        'club_median_retention_weekend': _r(statistics.median(club_we), 0) if club_we else None,
        'other_median_retention_weekend': _r(statistics.median(non_we), 0) if non_we else None,
        'club_median_late': _r(statistics.median(club_late), 0) if club_late else None,
        'other_median_late': _r(statistics.median(non_late), 0) if non_late else None,
        'two_peak_stores': sorted(s['store'] for s in store_ret if s['two_peak_weekend']),
        'two_peak_club': sum(1 for s in store_ret if s['two_peak_weekend'] and s['club_area']),
        'two_peak_other': sum(1 for s in store_ret if s['two_peak_weekend'] and not s['club_area']),
    }
    store_ret.sort(key=lambda s: -s['retention_pct'])

    # ---------- first 10 minutes ----------
    bands = {'20時台': (20,), '21〜22時台': (21, 22), '23時台': (23,), '0〜1時台': (0, 1)}
    deltas = {k: [] for k in bands}
    pair_gaps = []
    ratio_obs = defaultdict(list)
    for n, rows in data.items():
        j = 0
        for i, (dt, biz, w, m) in enumerate(rows):
            for k, hs in bands.items():
                if dt.hour in hs and w + m >= 6:
                    ratio_obs[k].append((w, m))
            target = dt + datetime.timedelta(minutes=10)
            if j < i:
                j = i
            while j < len(rows) and rows[j][0] < target - datetime.timedelta(minutes=2):
                j += 1
            if j < len(rows) and rows[j][0] <= target + datetime.timedelta(minutes=2) and w + m >= 6:
                for k, hs in bands.items():
                    if dt.hour in hs:
                        deltas[k].append((rows[j][2] - w, rows[j][3] - m))
                pair_gaps.append((rows[j][0] - dt).total_seconds() / 60)
    first10 = []
    for k in bands:
        dl = deltas[k]
        ro = ratio_obs[k]
        if len(dl) < 30:
            continue
        first10.append({
            'band': k, 'pairs': len(dl),
            'median_abs_dw': _r(statistics.median([abs(a) for a, _ in dl]), 1),
            'change2_pct': _r(sum(1 for a, b in dl if abs(a) >= 2 or abs(b) >= 2) / len(dl) * 100, 0),
            'w_up_pct': _r(sum(1 for a, _ in dl if a > 0) / len(dl) * 100, 0),
            'w_down_pct': _r(sum(1 for a, _ in dl if a < 0) / len(dl) * 100, 0),
            'women_ge_men_pct': _r(sum(1 for w, m in ro if w >= m) / len(ro) * 100, 0) if ro else None,
            'median_w_per_m': _r(statistics.median([w / m for w, m in ro if m > 0]), 2) if ro else None,
        })

    result = {
        'meta': meta,
        'weekend': {'weekday_peaks': wd_rows, 'hourly_weekend_vs_weekday': hour_cmp, 'walk_clusters': cluster_rows},
        'club': {'summary': club_summary, 'top_retention': store_ret[:8], 'bottom_retention': store_ret[-5:]},
        'first10': {'bands': first10, 'median_pair_gap_min': _r(statistics.median(pair_gaps), 1) if pair_gaps else None},
    }
    text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text)
    print(text)


if __name__ == '__main__':
    main()
