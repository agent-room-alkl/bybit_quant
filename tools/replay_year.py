"""One-year replay of the already selected configuration; no parameter search."""
import argparse
import gzip
import hashlib
from collections import Counter
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from replay import prepare, simulate, metrics, timestamp


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,default=8)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',type=Path,default=ROOT/'data/replay_v7_year')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    start,split,end=map(timestamp,('2025-09-27','2026-03-31','2026-09-27'))
    output=args.output;output.mkdir(parents=True,exist_ok=True)
    cache=ROOT/'data/market_cache'
    # Freeze the configuration selected in the preceding 180-day study.
    if args.config:
        cfg=json.loads(args.config.read_text(encoding='utf-8'))
        configuration_source=str(args.config)
    else:
        configuration_source='data/replay_v7/corrected_legacy_rules.json'
        prior=json.loads((ROOT/configuration_source).read_text(encoding='utf-8'))
        cfg=prior['config']
    cfg['enable_trading']=False
    for key in ('api_key','api_secret','gpt_api_key','claude_api_key'):cfg.pop(key,None)
    print(json.dumps({'trend_rebuild_enabled':cfg['strategy'].get('trend_rebuild_enabled',False),'config':configuration_source}),flush=True)
    early=prepare(start,split,cache,args.workers)
    # These causal features were computed before line-ending-only normalization.
    # Reuse them explicitly; strategy and execution are rerun over the entire year.
    historical_cache=cache/'features_1774915200000_1790467200000_9e81d3a1f033.json.gz'
    if historical_cache.exists():
        with gzip.open(historical_cache,'rt',encoding='utf-8') as f:late=json.load(f)
    else:late=prepare(split,end,cache,args.workers)
    prepared=early+late
    expected=list(range(start,end,900000))
    if [int(x['bar'][0]) for x in prepared]!=expected:
        raise ValueError('Annual candle coverage is not continuous')
    print(f'Replaying fixed configuration: {len(prepared)} bars',flush=True)
    run=simulate(prepared,cfg)
    (output/'fixed_strategy.json').write_text(json.dumps(run,ensure_ascii=False),encoding='utf-8')
    initial=run['initial'];first=prepared[0]['f15']['close']
    hold=[]
    for item in prepared:
        price=float(item['bar'][4]);equity=initial*.6+initial*.4*price/first
        hold.append({'ts':int(item['bar'][0])+900000,'equity':equity,'position_pct':initial*.4*price/first/equity*100})
    def summary(curve):
        result=metrics(curve,initial)
        result.update(initial_equity=initial,final_equity=curve[-1]['equity'],annualized_return_pct=((curve[-1]['equity']/initial)**(365*86400000/(end-start))-1)*100)
        return result
    monthly=[];previous=initial;groups={}
    for row in run['curve']:
        month=datetime.fromtimestamp((row['ts']-1)/1000,timezone.utc).strftime('%Y-%m')
        groups.setdefault(month,[]).append(row)
    for month,rows in groups.items():
        monthly.append({'month':month,**metrics(rows,previous)});previous=rows[-1]['equity']
    report={'start_utc':'2025-09-27 00:00:00','end_utc_exclusive':'2026-09-27 00:00:00','days':365,
            'configuration_source':configuration_source,
            'trend_rebuild_enabled':cfg['strategy'].get('trend_rebuild_enabled',False),
            'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('replay.py','execution_engine.py','execution_policy.py','strategy_v5.py','market_data.py','inventory_ledger.py','indicators.py')},
            'orders_by_intent':dict(Counter(o['intent'] for o in run['orders'])),
            'strategy':summary(run['curve']),'hold_40pct_sol_60pct_usdt':summary(hold),
            'orders':len(run['orders']),'fees_usdt':run['fees'],'attempts':run['attempts'],
            'unfilled_attempts':run['unfilled_attempts'],'monthly':monthly,
            'limitations':['Retrospective study: configuration was already selected using part of this year.',
                           '15-minute decisions and modeled IOC fills; no historical orderbook.',
                           'Initial SOL inventory is marked at the starting price; not the actual account.',
                           'Includes configured fees and 5bps slippage plus 1bps spread per execution.',
                           'Drawdown is sampled at 15-minute closes, not intrabar equity.']}
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2),flush=True)


if __name__=='__main__':main()
