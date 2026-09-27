"""One-shot, GET-only live shadow evaluation, using isolated local storage."""
import argparse
import json
import sys
from pathlib import Path
from bybit_client import BybitClient
from execution_engine import run_step
from runtime_store import RuntimeStore

class ReadOnlyClient(BybitClient):
    def _request(self,method,*args,**kwargs):
        if method.upper()!='GET':
            raise RuntimeError('Read-only shadow client refuses every non-GET request')
        return super()._request(method,*args,**kwargs)

def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    root=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default=str(root/'vercel/lib/config.json'))
    p.add_argument('--env',default=str(root/'.env'))
    p.add_argument('--output',default=str(root/'data/shadow_v7'))
    a=p.parse_args(argv)
    cfg=json.loads(Path(a.config).read_text(encoding='utf-8'))
    cfg['enable_trading']=False;cfg.setdefault('strategy',{})['news_enabled']=False
    env={};bare=[]
    for line in Path(a.env).read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if '=' in line:
            k,v=line.split('=',1);env[k.strip()]=v.strip().strip('"').strip("'")
        else:bare.append(line)
    key=env.get('BYBIT_API_KEY') or (bare[0] if bare else '')
    secret=env.get('BYBIT_API_SECRET') or (bare[1] if len(bare)>1 else '')
    if not key or not secret:raise ValueError('Bybit credentials missing')
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    import db
    db.DATA_DIR=str(out);db.META_FILE=str(out/'meta.json');db.TRADES_FILE=str(out/'trades.json');db.SIGNALS_FILE=str(out/'signals.json');db.init_db()
    client=ReadOnlyClient(api_key=key,api_secret=secret,testnet=cfg.get('testnet',False),max_retries=1,timeout=20)
    snapshots={}
    for symbol in cfg.get('symbols',['SOLUSDT']):
        store=RuntimeStore('readonly-shadow',out/'runtime.db')
        try:snapshots[symbol]=run_step(client,cfg,symbol,store=store)
        finally:store.close()
    (out/'latest.json').write_text(json.dumps(snapshots,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({s:{k:r.get(k) for k in ('decision','reason','last_price','position_pct','data_complete','placed','health')} for s,r in snapshots.items()},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
