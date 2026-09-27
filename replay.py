"""Causal, shared-policy spot replay. No private API methods or account keys.

Decide from closed candles; execute at the following candle's open subject to
the submitted IOC limit. This is a scenario model, not reconstructed orderbook
liquidity. Historical synthetic news is deliberately excluded.
"""
import argparse
from bisect import bisect_left
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import gzip
import hashlib
import json
import logging
import math
import time
import requests

from execution_engine import evaluate, size_order, context_from_orders
from execution_policy import update_equity_risk, VERSION, trading_day
from market_data import features, make_market
from strategy_v5 import create_smart_strategy
from trade_logic import calculate_position, InstrFilters, round_to_step
from inventory_ledger import FIFOInventory

ROOT=Path(__file__).resolve().parent

def timestamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()*1000)

def fetch_candles(symbol,interval,start,end,cache):
    path=cache/f'{symbol}_{interval}_{start}_{end}.json'
    if path.exists():return json.loads(path.read_text())
    rows={};cursor=end-1
    while cursor>=start:
        for attempt in range(3):
            try:
                response=requests.get('https://api.bybit.com/v5/market/kline',params={'category':'spot','symbol':symbol,'interval':str(interval),'start':start,'end':cursor,'limit':1000},timeout=20)
                response.raise_for_status();data=response.json()
                if data.get('retCode')!=0:raise RuntimeError('Market API failed')
                batch=data['result']['list'];break
            except (requests.RequestException,ValueError,RuntimeError):
                if attempt==2:raise
                time.sleep(1+attempt)
        if not batch:break
        for row in batch:rows[int(row[0])]=row
        oldest=min(int(r[0]) for r in batch)
        if oldest>cursor:raise RuntimeError('Nonadvancing market cursor')
        cursor=oldest-1
    ordered=[rows[t] for t in sorted(rows) if start<=t<end]
    if not ordered or int(ordered[0][0])>start or int(ordered[-1][0])+interval*60000<end:
        raise ValueError(f'Incomplete market coverage: {symbol}/{interval}')
    path.write_text(json.dumps(ordered),encoding='utf-8')
    print(f'Market cache: {symbol} {interval}m, {len(ordered)} bars',flush=True)
    return ordered

def feature_job(job):
    rows,interval,now,limit=job
    return features(rows,interval,now,limit)

def prepare(start,end,cache,workers=2):
    # Rolling-window features are identical to live, including EMA initialization.
    digest=hashlib.sha256((ROOT/'market_data.py').read_bytes()+(ROOT/'indicators.py').read_bytes()).hexdigest()[:12]
    path=cache/f'features_{start}_{end}_{digest}.json.gz'
    if path.exists():
        with gzip.open(path,'rt',encoding='utf-8') as f:return json.load(f)
    sol15=fetch_candles('SOLUSDT',15,start-201*900000,end,cache)
    solh=fetch_candles('SOLUSDT',60,start-401*3600000,end,cache)
    btch=fetch_candles('BTCUSDT',60,start-401*3600000,end,cache)
    f15jobs=[];bars=[]
    for i,row in enumerate(sol15):
        ts=int(row[0])
        if start<=ts<end:
            f15jobs.append((sol15[max(0,i-200):i],15,ts,200));bars.append(row)
    times=sorted({int(r[0])//3600000*3600000 for r in bars})
    hourly=[]
    for rows in (solh,btch):
        rowtimes=[int(r[0]) for r in rows];jobs=[]
        for ts in times:
            i=bisect_left(rowtimes,ts)
            jobs.append((rows[max(0,i-400):i],60,ts,400))
        hourly.append(jobs)
    combined=f15jobs+hourly[0]+hourly[1];computed=[]
    print(f'Computing {len(combined)} causal feature windows ({workers} workers)',flush=True)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i,f in enumerate(pool.map(feature_job,combined,chunksize=40)):
            computed.append(f)
            if (i+1)%1000==0:print(f'Features {i+1}/{len(combined)}',flush=True)
    count=len(f15jobs);hours=len(times)
    hmap=dict(zip(times,computed[count:count+hours]));bmap=dict(zip(times,computed[count+hours:]))
    prepared=[{'bar':r,'f15':f,'h1':hmap[int(r[0])//3600000*3600000],'btc':bmap[int(r[0])//3600000*3600000]} for r,f in zip(bars,computed[:count])]
    with gzip.open(path,'wt',encoding='utf-8') as f:json.dump(prepared,f)
    return prepared

def simulate(prepared,cfg,initial=1000,initial_position_pct=40,slippage_bps=5,fill_fraction=1):
    strategy=create_smart_strategy(cfg)
    cash=initial*(1-initial_position_pct/100);base=initial*initial_position_pct/100/prepared[0]['f15']['close']
    cost=prepared[0]['f15']['close'] if base else 0
    inventory_book=FIFOInventory();inventory_book.add(base,base*cost)
    orders=[];curve=[];risk={};last_exit=0;fees=0;attempts=0;no_fills=0;last_bar_ms=None
    filters=InstrFilters(.01,.0001,.0001,5)
    fee_rate=float(cfg.get('fees',{}).get('spot_taker_bps',10))/10000
    for item in prepared:
        bar=item['bar'];now=int(bar[0]);price=item['f15']['close']
        m=make_market(item['f15'],item['h1'],item['btc'],price,cost,base,cash,orders,last_exit)
        pos=calculate_position(base,cash,price)
        risk=update_equity_risk(risk,pos.total_value_usdt,0,now,cfg.get('risk',{}))
        # Only the day's orders and last fills are needed for throttles.
        ctx=context_from_orders(orders,now,cfg,pos.total_value_usdt)
        buys=[o for o in orders if o['side']=='Buy']
        ctx.update(halt_until=risk['halt_until'],last_buy_price=buys[-1]['price'] if buys else 0,data_complete=True,
                   qty_step=filters.qty_step,min_qty=filters.min_qty,min_notional=filters.min_notional)
        ctx['risk_exits_today']=strategy._daily_stop_loss_count if strategy._last_stop_loss_date==trading_day(now,cfg.get('risk',{}).get('timezone','Pacific/Auckland')) else 0
        signal=evaluate(strategy,m,pos,now,cfg,ctx)
        # Freeze signal and IOC price BEFORE seeing the following open.
        book=(price*(1-.0001),price*(1+.0001))
        od=size_order(signal,pos,price,book,filters,cfg,'SOLUSDT')
        if od:
            attempts+=1
            side=od['side'];direction=1 if side=='Buy' else -1
            px=float(bar[1])*(1+direction*(slippage_bps+1)/10000)
            limit=float(od['price'])
            executable=px<=limit if side=='Buy' else px>=limit
            qty=round_to_step(float(od['qty'])*fill_fraction,filters.qty_step)
            if side=='Buy':qty=min(qty,round_to_step(cash/px,filters.qty_step))
            else:qty=min(qty,round_to_step(base,filters.qty_step))
            if executable and qty>0:
                notional=qty*px;fee=notional*fee_rate;fees+=fee
                if side=='Buy':
                    received=qty*(1-fee_rate);inventory_book.add(received,notional)
                    base+=received;cash-=notional
                else:
                    inventory_book.remove(qty)
                    base-=qty;cash+=notional-fee
                    if base<1e-10:base=0;cost=0
                cost=inventory_book.average
                o={'order_id':str(len(orders)),'link':'v7_replay_'+str(len(orders)),'side':side,'qty':qty,'price':px,'ts':now,'fee':fee,'intent':signal.intent,'reason':signal.reason}
                orders.append(o)
                if signal.intent=='risk_exit':last_exit=now;strategy.on_risk_fill(px,now)
            else:no_fills+=1
        # Mark at close, but do not feed that close into the already-made decision.
        close=float(bar[4]);value=cash+base*close
        curve.append({'ts':now+900000,'equity':value,'position_pct':base*close/value*100,'price':close,'fees':fees,'orders':len(orders)})
    return {'curve':curve,'orders':orders,'attempts':attempts,'unfilled_attempts':no_fills,'fees':fees,'initial':initial,'config':cfg}

def metrics(curve,initial=None):
    if not curve:return {}
    start=initial if initial is not None else curve[0]['equity']
    peak=start;drawdown=0
    for r in curve:
        peak=max(peak,r['equity']);drawdown=max(drawdown,1-r['equity']/peak)
    return {'return_pct':(curve[-1]['equity']/start-1)*100,'max_drawdown_pct':drawdown*100,
            'average_position_pct':sum(r['position_pct'] for r in curve)/len(curve),
            'time_below_15pct':sum(r['position_pct']<15 for r in curve)/len(curve)*100}

def summarize(run,train_end,test_start):
    curve=run['curve']
    slices={'train':[r for r in curve if r['ts']<=train_end],
            'validation':[r for r in curve if train_end<r['ts']<=test_start],
            'holdout':[r for r in curve if r['ts']>test_start]}
    report={'overall':metrics(curve,run['initial']),'orders':len(run['orders']),'fees':run['fees'],'attempts':run['attempts'],'unfilled_attempts':run['unfilled_attempts']}
    previous=run['initial']
    for name,part in slices.items():
        report[name]=metrics(part,previous)
        if part:previous=part[-1]['equity']
    # Rolling 30-day forward segments; never stitch candidates without costs.
    report['forward_segments']=[]
    for offset in range(90*96,len(curve),30*96):
        segment=curve[offset:offset+30*96]
        report['forward_segments'].append({'start_ms':segment[0]['ts'],'end_ms':segment[-1]['ts'],**metrics(segment,curve[offset-1]['equity'])})
    return report

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default='2026-03-31');parser.add_argument('--end',default='2026-09-27')
    parser.add_argument('--config',default=str(ROOT/'vercel/lib/config.json'))
    parser.add_argument('--sweep',action='store_true');parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--output',default=str(ROOT/'data/replay_v7'))
    args=parser.parse_args(argv)
    start,end=timestamp(args.start),timestamp(args.end)
    if end<=start or end>int(time.time()*1000):raise ValueError('Invalid/future replay interval')
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);cache=ROOT/'data/market_cache';cache.mkdir(parents=True,exist_ok=True)
    cfg=json.loads(Path(args.config).read_text(encoding='utf-8'));cfg['enable_trading']=False
    # Config artifacts must not retain credentials supplied in a user's file.
    for key in ('api_key','api_secret','gpt_api_key','claude_api_key'):cfg.pop(key,None)
    cfg.setdefault('strategy',{})['news_enabled']=False
    logging.disable(logging.CRITICAL)
    prepared=prepare(start,end,cache,args.workers)
    duration=end-start;test_start=end-min(60*86400000,int(duration/3));train_end=test_start-min(30*86400000,int(duration/6))
    candidates={'v7_default':deepcopy(cfg)}
    baseline=deepcopy(cfg);baseline['strategy']['trend_rebuild_enabled']=False
    candidates['corrected_legacy_rules']=baseline
    if args.sweep:
        for target in (30,40,50):
            for budget in (.25,.5):
                c=deepcopy(cfg);c['strategy'].update(bull_target_position_pct=target,trend_entry_step_pct=10)
                c['risk'].update(risk_per_trade_pct=budget,max_single_trade_pct=10)
                candidates[f'target{target}_risk{budget}']=c
    reports={};runs={}
    for name,config in candidates.items():
        print(f'Replaying {name}',flush=True)
        run=simulate(prepared,config);runs[name]=run
        reports[name]=summarize(run,train_end,test_start)
        (out/(name+'.json')).write_text(json.dumps(run,ensure_ascii=False),encoding='utf-8')
        print(json.dumps({'candidate':name,**reports[name]['overall'],'orders':len(run['orders'])}),flush=True)
    # Predeclared training objective: return minus twice maximum drawdown.
    selected=max(reports,key=lambda n:reports[n]['train']['return_pct']-2*reports[n]['train']['max_drawdown_pct'])
    stress={}
    for slip,fraction in ((10,1),(20,1),(5,.5)):
        print(f'Stress {selected}: slippage={slip}bps fill_fraction={fraction}',flush=True)
        stress[f'slip{slip}_fill{fraction}']=summarize(simulate(prepared,candidates[selected],slippage_bps=slip,fill_fraction=fraction),train_end,test_start)
    report={'version':VERSION,'start_ms':start,'end_ms':end,'train_end_ms':train_end,'holdout_start_ms':test_start,
            'selection_objective':'training return_pct - 2 * max_drawdown_pct','selected_on_training':selected,
            'candidates':reports,'stress':stress,
            'limitations':['15-minute decisions; live checks every minute.','IOC fill is a bounded-price scenario, not historical orderbook reconstruction.','Initial 40% SOL inventory marked at start price; this is not the actual account.','The most recent two months were previously inspected during design; chronological holdout is not wholly unseen research data.','Requires subsequent forward shadow observation before production promotion.']}
    (out/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Selected on training only: '+selected,flush=True)
    print('Report: '+str(out/'comparison.json'),flush=True)
    return report

if __name__=='__main__':main()
