import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime
import sys

# Global error log for debugging Render timeouts
fetch_errors = {}

# URLs to monitor
ORIENTAL_URL = "https://oriental-lounge.com/"
JIS_URL = "https://jis.bar/"
XIX_URL = "https://aiseki-okayama.conohawing.com/aiseki/parts/get_cs_info.php"
ALFA_URL = "https://aiseki-hiroshima.com/wp/display.php"
YATAKOI_URL = "https://asobibar-823d1.firebaseio.com/shops/chayamachi.json"

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,ja-JP;q=0.9',
}

# Cache for OLG data (fallback when connection fails)
_olg_cache = {
    'data': [],
    'last_success': None
}

def get_oriental_data():
    global _olg_cache
    try:
        response = requests.get(ORIENTAL_URL, timeout=(3, 7), headers=_HEADERS)
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching Oriental data: {e}", file=sys.stderr)
        if _olg_cache['data']:
            print(f"Using cached OLG data from {_olg_cache['last_success']}", file=sys.stderr)
            return _olg_cache['data']
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    stores = soup.select('a.card.wave-anime-wrap')
    
    if not stores:
        print(f"WARNING: OLG page returned {len(response.content)} bytes but no store elements found", file=sys.stderr)
        if _olg_cache['data']:
            return _olg_cache['data']
        return []
    
    store_data = []
    for store in stores:
        try:
            name_tag = store.select_one('h4')
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            
            men_tag = store.select_one('.num-male')
            men_count = int(men_tag.get_text(strip=True)) if men_tag else 0
            
            women_tag = store.select_one('.num-female')
            women_count = int(women_tag.get_text(strip=True)) if women_tag else 0
            
            store_data.append({
                'name': f"OLG {name}",
                'men': men_count,
                'women': women_count,
                'source': 'oriental'
            })
        except ValueError:
            continue
    
    if store_data:
        _olg_cache['data'] = store_data
        _olg_cache['last_success'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return store_data
    
    if _olg_cache['data']:
        return _olg_cache['data']
    return []


def get_jis_data():
    try:
        response = requests.get(JIS_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching JIS data: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    scripts = soup.find_all('script')
    target_script = None
    for script in scripts:
        if script.string and 'var datas =' in script.string:
            target_script = script.string
            break
            
    if not target_script:
        print("Could not find JIS data script", file=sys.stderr)
        return []

    store_data = []
    
    import re
    import json
    
    match = re.search(r'var datas\s*=\s*({.*?});', target_script, re.DOTALL)
    if match:
        json_str = match.group(1)
        try:
            data = json.loads(json_str)
            for store_key, store_info in data.items():
                if 'shared' in store_info:
                    men_count = store_info['shared'].get('mens_customer_num', 0)
                    women_count = store_info['shared'].get('ladys_customer_num', 0)
                    
                    name_map = {
                        'sapporo_b1': 'SAPPORO',
                        'omiya': 'OMIYA',
                        'shinjuku': 'SHINJUKU',
                        'nishishinjuku': 'NISHISHINJUKU',
                        'umeda': 'UMEDA',
                        'namba': 'NAMBA',
                        'chayamachi': 'CHAYAMACHI',
                        'fukuoka': 'FUKUOKA',
                        'kumamoto': 'KUMAMOTO',
                        'matsuyama': 'MATSUYAMA'
                    }
                    name = name_map.get(store_key, store_key.upper())
                    
                    store_data.append({
                        'name': f"JIS {name}",
                        'men': men_count,
                        'women': women_count,
                        'source': 'jis'
                    })
        except json.JSONDecodeError as e:
            print(f"Error parsing JIS JSON: {e}", file=sys.stderr)
            
    return store_data

def get_xix_data():
    try:
        response = requests.get(XIX_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        data = response.json()
        
        if data and len(data) > 0:
            info = data[0]
            men_count = int(info.get('m_cnt', 0))
            women_count = int(info.get('w_cnt', 0))
            return [{
                'name': "XIX OKAYAMA",
                'men': men_count,
                'women': women_count,
                'source': 'xix'
            }]
    except Exception as e:
        print(f"Error fetching XIX data: {e}", file=sys.stderr)
    return []

def get_alfa_data():
    try:
        response = requests.get(ALFA_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            import json
            data = json.loads(response.content.decode('utf-8-sig'))
        
        men_count = int(data.get('man_num', 0))
        women_count = int(data.get('woman_num', 0))
        
        return [{
            'name': "ALFA HIROSHIMA",
            'men': men_count,
            'women': women_count,
            'source': 'alfa'
        }]
    except Exception as e:
        print(f"Error fetching ALFA data: {e}", file=sys.stderr)
    return []

def get_yatakoi_data():
    try:
        response = requests.get(YATAKOI_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        data = response.json()
        
        if data:
            men = int(data.get('males', 0))
            women = int(data.get('females', 0))
            yk_men = int(data.get('ykMales', 0))
            yk_women = int(data.get('ykFemales', 0))
            
            return [{
                'name': "YATAKOI UMEDA",
                'men': men + yk_men,
                'women': women + yk_women,
                'source': 'yatakoi'
            }]
    except Exception as e:
        print(f"Error fetching Yatakoi data: {e}", file=sys.stderr)
    return []

def get_clovers_data():
    """Scrape 相席CLOVERS (Hiroshima) from bar-clovers.com"""
    try:
        response = requests.get("https://bar-clovers.com/", headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching CLOVERS data: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    visit_table = soup.select_one('table.visit')
    if not visit_table:
        print("Could not find visit table on CLOVERS site", file=sys.stderr)
        return []
    
    try:
        rows = visit_table.select('tr')
        if len(rows) >= 2:
            cells = rows[1].select('td')
            if len(cells) >= 2:
                men_count = int(cells[0].get_text(strip=True))
                women_count = int(cells[1].get_text(strip=True))
                return [{
                    'name': 'CLOVERS HIROSHIMA',
                    'men': men_count,
                    'women': women_count,
                    'source': 'clovers'
                }]
    except (ValueError, IndexError) as e:
        print(f"Error parsing CLOVERS data: {e}", file=sys.stderr)
    
    return []

def _run_with_timeout(fn, timeout=15):
    """Run fn in a daemon thread with a hard timeout. Returns result or raises."""
    import threading
    result_holder = [None]
    error_holder = [None]
    
    def wrapper():
        try:
            result_holder[0] = fn()
        except Exception as e:
            error_holder[0] = e
    
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    t.join(timeout=timeout)
    
    if t.is_alive():
        # Thread is still running - abandon it (daemon thread will die with process)
        raise TimeoutError(f"Exceeded {timeout}s hard timeout")
    
    if error_holder[0]:
        raise error_holder[0]
    
    return result_holder[0]

def get_all_data():
    fetchers = [
        get_oriental_data,
        get_jis_data,
        get_xix_data,
        get_alfa_data,
        get_yatakoi_data,
        get_clovers_data,
    ]
    
    data = []
    for fn in fetchers:
        name = fn.__name__
        try:
            result = _run_with_timeout(fn, timeout=15)
            if result:
                data.extend(result)
                print(f"  ✓ {name}: {len(result)} stores")
            else:
                fetch_errors[name] = "Returned empty"
                print(f"  ✗ {name}: returned empty", file=sys.stderr)
        except TimeoutError:
            fetch_errors[name] = "Hard timeout (>15s)"
            print(f"  ✗ {name}: hard timeout exceeded 15s!", file=sys.stderr)
        except Exception as e:
            fetch_errors[name] = str(e)
            print(f"  ✗ {name}: {e}", file=sys.stderr)
    
    return data



def find_store_with_max_women(data):
    if not data:
        return None
    sorted_data = sorted(data, key=lambda x: x['women'], reverse=True)
    return sorted_data[0]
