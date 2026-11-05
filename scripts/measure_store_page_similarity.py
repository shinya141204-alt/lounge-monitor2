#!/usr/bin/env python3
"""店舗ページ（/store/<slug>）同士の本文類似度を difflib.SequenceMatcher で測定する。

使い方:
    python3 scripts/measure_store_page_similarity.py              # ローカル (Flask test_client)
    python3 scripts/measure_store_page_similarity.py --prod       # 本番 https://aiseki-navi.com
    python3 scripts/measure_store_page_similarity.py --slugs a,b,c

本文 = <script>/<style>/<noscript>/<svg> を除去し、タグを空白へ置換した可視テキスト。
"""
import argparse
import html
import itertools
import os
import re
import statistics
import sys
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_SLUGS = [
    'oriental-lounge-shinjuku', 'oriental-lounge-shibuya-honten', 'ag-umeda',
    'oriental-lounge-nagasaki', 'oriental-lounge-kokura', 'jis-shinjuku',
    'jis-sapporo', 'asobibar-shibuya',
]


def visible_text(raw_html: str, strip_chrome: bool = False) -> str:
    body = raw_html
    m = re.search(r'<body[^>]*>(.*)</body>', raw_html, re.S | re.I)
    if m:
        body = m.group(1)
    body = re.sub(r'<(script|style|noscript|svg|template)\b.*?</\1>', ' ', body, flags=re.S | re.I)
    if strip_chrome:
        # サイト共通のナビゲーション・フッターを除いた「本文」だけで比較する
        body = re.sub(r'<(nav|footer)\b.*?</\1>', ' ', body, flags=re.S | re.I)
        body = re.sub(r'<div class="portal-top-header".*?</div>\s*</div>', ' ', body, flags=re.S | re.I)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = html.unescape(body)
    return re.sub(r'\s+', ' ', body).strip()


def fetch_local(slugs):
    os.environ.setdefault('DISABLE_SHEETS', '1')
    os.environ.setdefault('APP_ROLE', 'web')
    import app as app_module
    client = app_module.app.test_client()
    out = {}
    for s in slugs:
        r = client.get(f'/store/{s}', headers={'User-Agent': 'Mozilla/5.0 similarity-check'})
        if r.status_code == 200:
            out[s] = r.data.decode('utf-8', 'ignore')
        else:
            print(f'  skip {s}: HTTP {r.status_code}', file=sys.stderr)
    return out


def fetch_prod(slugs):
    import requests
    out = {}
    for s in slugs:
        r = requests.get(f'https://aiseki-navi.com/store/{s}', headers={'User-Agent': 'Mozilla/5.0 similarity-check'}, timeout=30)
        if r.status_code == 200:
            out[s] = r.text
        else:
            print(f'  skip {s}: HTTP {r.status_code}', file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prod', action='store_true')
    ap.add_argument('--slugs', default='')
    ap.add_argument('--strip-chrome', action='store_true', help='共通ナビ・フッターを除いて測る')
    args = ap.parse_args()
    slugs = [x for x in args.slugs.split(',') if x] or DEFAULT_SLUGS
    pages = fetch_prod(slugs) if args.prod else fetch_local(slugs)
    texts = {s: visible_text(h, strip_chrome=args.strip_chrome) for s, h in pages.items()}
    for s, t in texts.items():
        print(f'{s}: {len(t)} chars')
    ratios = []
    for a, b in itertools.combinations(texts.keys(), 2):
        r = SequenceMatcher(None, texts[a], texts[b]).ratio()
        ratios.append(r)
        print(f'{a} vs {b}: {r * 100:.1f}%')
    if ratios:
        print(f'min={min(ratios) * 100:.1f}% median={statistics.median(ratios) * 100:.1f}% max={max(ratios) * 100:.1f}%')


if __name__ == '__main__':
    main()
