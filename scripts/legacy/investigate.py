import requests
from bs4 import BeautifulSoup
import sys

TARGETS = {
    "xix": "https://xix-thelounge.com/",
    "yatakoi": "https://yatakoi.com/",
    "alfa": "https://aiseki-hiroshima.com/"
}

def inspect(name, url):
    print(f"--- {name} ({url}) ---")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.content, 'html.parser')
        
        # Search for common indicators
        keywords = ['male', 'female', 'men', 'women', '男性', '女性', '人', 'count']
        
        # Check script tags for logic
        scripts = soup.find_all('script')
        for script in scripts:
            if not script.string:
                continue
                
            content = script.string
            if 'show_cs_info' in content or '$.ajax' in content or 'fetch(' in content or 'customer_num' in content:
                print(f"--- Relevant Script in {name} ---")
                # Print the whole script or a large chunk to find the URL
                print(content[:4000]) # Limit to 4000 chars
                print("---------------------------------")

        # Check visual elements
        # XIX pattern seen in markdown: MEN人 / WOMEN人
        if name == 'xix':
            men = soup.find(string=lambda t: 'MEN' in str(t))
            if men:
                print(f"Found MEN text parent: {men.parent}")
            
        # Generic search
        for k in ['num-male', 'num-female', 'customer_num', 'situation']:
             elements = soup.select(f'.{k}')
             for el in elements:
                 print(f"Found class .{k}: {el}")
                 
        # Dump partial content if unsure
        # print(soup.prettify()[:1000])

    except Exception as e:
        print(f"Error: {e}")
    print("\n")

if __name__ == "__main__":
    for name, url in TARGETS.items():
        inspect(name, url)
