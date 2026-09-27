"""Package shared runtime into Vercel's lib directory; --check detects drift."""
from pathlib import Path
import argparse
import shutil

ROOT=Path(__file__).resolve().parents[1]
FILES=['strategy_v5.py','cost.py','trade_logic.py','indicators.py','bybit_client.py',
       'runtime_store.py','execution_policy.py','execution_engine.py','market_data.py','inventory_ledger.py',
       'risk_modules/__init__.py','risk_modules/adaptive_exit.py']

def sync(check=False):
    drift=[]
    for name in FILES:
        src=ROOT/name;dst=ROOT/'vercel/lib'/name
        if not dst.exists() or src.read_bytes()!=dst.read_bytes():
            drift.append(name)
            if not check:
                dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
    if check and drift:raise SystemExit('Runtime drift: '+', '.join(drift))
    print(('Checked' if check else 'Packaged')+f' {len(FILES)} shared runtime files')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');sync(p.parse_args().check)
