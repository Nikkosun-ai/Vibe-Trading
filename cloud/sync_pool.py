import json, re, sys  
from pathlib import Path  
  
ROOT = Path(__file__).parent.parent  
  
def main():  
    pool = json.loads((ROOT / 'agent' / 'pool.json').read_text())  
    entries = []  
    for sector, stocks in pool['sectors'].items():  
        for s in stocks:  
            entries.append(f\"    '{s['code']}': {{'name': '{s['name']}', 'sector': '{sector}'}},\")  
    block = 'NQP_POOL = {' + chr(10) + chr(10).join(entries) + chr(10) + '}'  
    dp = (ROOT / 'agent' / 'daily_pipeline.py').read_text()  
    dp = re.sub(r'NQP_POOL = \{.*?\}', block, dp, flags=re.DOTALL)  
    (ROOT / 'agent' / 'daily_pipeline.py').write_text(dp)  
    print(f'synced {len(entries)} stocks')  
  
if __name__ == '__main__':  
    main() 
