"""
store_master.py - 店舗マスタ（static/store_master.json）の読み込み

店舗ごとの地域・エリア表示・座標・マップ検索の起点は、この1ファイルを正とします。
フロントエンド（static/app.js）も同じ JSON を読み込みます。
"""
import os
import json
import threading

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'store_master.json')
_lock = threading.Lock()
_cache = None


def load():
    global _cache
    with _lock:
        if _cache is None:
            try:
                with open(_PATH, 'r', encoding='utf-8') as f:
                    _cache = json.load(f).get('stores', {})
            except Exception:
                _cache = {}
        return _cache


def get(store_name):
    return load().get(store_name)


def region_of(store_name):
    info = get(store_name)
    return info.get('region') if info else None


def unknown_stores(names):
    """マスタ未登録の店舗名（新店舗・表記変更の検知用）。"""
    master = load()
    return sorted({n for n in names if n and n not in master})
