import requests
import cloudscraper
from bs4 import BeautifulSoup
import time
from datetime import datetime
import sys

# Create a cloudscraper instance to bypass WAFs
scraper = cloudscraper.create_scraper()

# URLs to monitor
ORIENTAL_URL = "https://oriental-lounge.com/"
JIS_URL = "https://jis.bar/"
XIX_URL = "https://aiseki-okayama.conohawing.com/aiseki/parts/get_cs_info.php"
ALFA_URL = "https://aiseki-hiroshima.com/wp/display.php"
YATAKOI_URL = "https://asobibar-823d1.firebaseio.com/shops/chayamachi.json"

def debug_connections():
    results = {}
    urls = {
        "Oriental": ORIENTAL_URL,
        "JIS": JIS_URL,
        "XIX": XIX_URL,
        "ALFA": ALFA_URL,
        "YATAKOI": YATAKOI_URL
    }
    
    for name, url in urls.items():
        try:
            start = time.time()
            resp = scraper.get(url, timeout=10)
            duration = time.time() - start
            results[name] = {
                "status": resp.status_code,
                "time": f"{duration:.2f}s",
                "length": len(resp.content)
            }
        except Exception as e:
            results[name] = {"error": str(e)}
            
    return results
# Cache for OLG data (fallback when connection fails)
_olg_cache = {
    'data': [],
    'last_success': None
}

def get_oriental_data():
    global _olg_cache
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            # Create a fresh scraper per attempt to avoid stale sessions
            fresh_scraper = cloudscraper.create_scraper()
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ja,ja-JP;q=0.9',
            }
            response = fresh_scraper.get(ORIENTAL_URL, timeout=20, headers=headers)
            response.raise_for_status()
        except Exception as e:
            print(f"Error fetching Oriental data (attempt {attempt+1}/{max_retries}): {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            # All retries failed - return cached data
            if _olg_cache['data']:
                print(f"Using cached OLG data from {_olg_cache['last_success']}", file=sys.stderr)
                return _olg_cache['data']
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Stores are in 'a' tags with class 'card' and 'wave-anime-wrap'
        stores = soup.select('a.card.wave-anime-wrap')
        
        if not stores:
            print(f"WARNING: OLG page returned {len(response.content)} bytes but no store elements found (attempt {attempt+1})", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            # Return cached data as fallback
            if _olg_cache['data']:
                print(f"Using cached OLG data from {_olg_cache['last_success']}", file=sys.stderr)
                return _olg_cache['data']
            return []
        
        store_data = []
        
        for store in stores:
            try:
                name_tag = store.select_one('h4')
                if not name_tag:
                    continue
                name = name_tag.get_text(strip=True)
                
                # Men count
                men_tag = store.select_one('.num-male')
                men_count = int(men_tag.get_text(strip=True)) if men_tag else 0
                
                # Women count
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
            # Update cache on success
            _olg_cache['data'] = store_data
            _olg_cache['last_success'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return store_data
        
        if attempt < max_retries - 1:
            time.sleep(3)
    
    # Final fallback to cache
    if _olg_cache['data']:
        print(f"Using cached OLG data from {_olg_cache['last_success']}", file=sys.stderr)
        return _olg_cache['data']
    return []


def get_jis_data():
    try:
        # JIS usually requires User-Agent
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        response = requests.get(JIS_URL, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching JIS data: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # JIS data is in a script tag with "var datas ="
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
    
    # Extract JSON string: var datas = { ... };
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
                    
                    # Map store keys to readable names if possible, or uppercase key
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
        fresh_scraper = cloudscraper.create_scraper()
        response = fresh_scraper.get(XIX_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # XIX API returns a list of objects, we take the first one
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
        fresh_scraper = cloudscraper.create_scraper()
        response = fresh_scraper.get(ALFA_URL, timeout=10)
        response.raise_for_status()
        # Handle UTF-8 BOM if present
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
        fresh_scraper = cloudscraper.create_scraper()
        response = fresh_scraper.get(YATAKOI_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data:
            men = int(data.get('males', 0))
            women = int(data.get('females', 0))
            # Some stores might have extra 'ykMales'/'ykFemales' to add
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
        fresh_scraper = cloudscraper.create_scraper()
        response = fresh_scraper.get("https://bar-clovers.com/", timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching CLOVERS data: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # Data is in: <table class="visit"><tr><th>男性</th><th>女性</th></tr><tr><td>0</td><td>0</td></tr></table>
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

def get_all_data():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    fetchers = [
        get_oriental_data,
        get_jis_data,
        get_xix_data,
        get_alfa_data,
        get_yatakoi_data,
        get_clovers_data,
    ]
    
    data = []
    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
    futures = {executor.submit(fn): fn.__name__ for fn in fetchers}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=25):
            name = futures[future]
            try:
                result = future.result()
                if result:
                    data.extend(result)
                    print(f"  ✓ {name}: {len(result)} stores")
            except Exception as e:
                print(f"  ✗ {name}: {e}", file=sys.stderr)
    except concurrent.futures.TimeoutError:
        print("  ✗ Warning: Some scrapers hung and timed out!", file=sys.stderr)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    
    return data

def find_store_with_max_women(data):
    if not data:
        return None
    
    # Sort by women count descending
    sorted_data = sorted(data, key=lambda x: x['women'], reverse=True)
    return sorted_data[0]

def main():
    print("Starting Oriental Lounge Monitor (Interval: 5 minutes)")
    print("Press Ctrl+C to stop.")
    print("-" * 50)
    
    try:
        while True:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data = get_all_data()
            
            if data:
                top_store = find_store_with_max_women(data)
                
                if top_store:
                    print(f"[{timestamp}] Most Ladies: {top_store['name']}")
                    print(f"  Ladies: {top_store['women']} / Gentlemen: {top_store['men']}")
                else:
                    print(f"[{timestamp}] No store data found.")
            else:
                 print(f"[{timestamp}] Failed to retrieve data.")
            
            print("-" * 50)
            # Sleep for 5 minutes (300 seconds)
            time.sleep(300)
            
    except KeyboardInterrupt:
        print("\nStopping monitor...")

if __name__ == "__main__":
    main()
