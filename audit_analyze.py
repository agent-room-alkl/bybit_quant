"""Analyze exported account ledger without placing orders or modifying remote data."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict
from bisect import bisect_right
import json, math, statistics

P=Path(__file__).resolve().parent/'data'/'audit_2026-09-27'
def load(n):return json.loads((P/(n+'.json')).read_text(encoding='utf-8'))
def save(n,v):(P/(n+'.json')).write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def dt(ms):return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
w=load('window');start,end=w['start_ms'],w['end_ms']
execs=[e for e in load('executions') if start<=int(e['execTime'])<=end]
trades=load('db_trades');signals=load('db_signals')
tx=sorted([r for r in load('transactions') if start<=int(r['transactionTime'])<=end],key=lambda r:(int(r['transactionTime']),r['id']))
tx=list({r['id']:r for r in tx}.values());tx.sort(key=lambda r:int(r['transactionTime']))
kl=load('klines_1h');p0=float(next(k[1] for k in kl if int(k[0])==start))
last_signal=signals[-1];mark_ts=last_signal['ts_ms'];p1=float(last_signal['last_price'])
initial={}
for r in tx:
    if r['currency'] not in initial:initial[r['currency']]=float(r['cashBalance'])-float(r['change'])
balances=dict(initial)
for r in tx:balances[r['currency']]+=float(r['change'])
flows=[r for r in tx if r['type']!='TRADE']
flow_total=sum(float(r['change']) for r in flows if r['currency']=='USDT')
v0=initial.get('SOL',0)*p0+initial.get('USDT',0)
v1=balances.get('SOL',0)*p1+balances.get('USDT',0)
hold=initial.get('SOL',0)*p1+initial.get('USDT',0)+flow_total
by_order=defaultdict(list)
for e in execs:by_order[e['orderId']].append(e)
db_orders={r['order_id']:r for r in trades}
orders=[]
for oid,es in by_order.items():
    qty=sum(float(e['execQty']) for e in es);val=sum(float(e['execValue']) for e in es)
    fee=sum(float(e['execFee'])*(float(e['execPrice']) if e['feeCurrency']=='SOL' else 1) for e in es)
    log=db_orders.get(oid,{})
    orders.append({'ts':min(int(e['execTime']) for e in es),'side':es[0]['side'],'qty':qty,'value':val,'price':val/qty,'fee_usdt':fee,'fills':len(es),'logged_qty':float(log['qty']) if log else None,'reason':log.get('reason',''),'order_id':oid})
orders.sort(key=lambda r:r['ts'])
monthly={}
for o in orders:
    month=dt(o['ts'])[:7];m=monthly.setdefault(month,{'buy_orders':0,'sell_orders':0,'buy_value':0,'sell_value':0,'fees':0})
    k=o['side'].lower();m[k+'_orders']+=1;m[k+'_value']+=o['value'];m['fees']+=o['fee_usdt']

# Mark holdings at each recorded signal; unitize the sole USDT transfer at the latest observed price.
b=dict(initial);j=0;units=v0;peak=1.0;maxdd=0;dd_peak_ts=start;running_peak_ts=start;dd_trough_ts=start
curve=[];prevprice=p0
for s in signals:
    ts=int(s['ts_ms']);price=float(s['last_price'])
    if price<=0:continue
    while j<len(tx) and int(tx[j]['transactionTime'])<=ts:
        r=tx[j]
        if r['type']!='TRADE':
            prevalue=b.get('USDT',0)+b.get('SOL',0)*prevprice
            cash=float(r['change'])*(prevprice if r['currency']=='SOL' else 1)
            if prevalue>0:units*=1+cash/prevalue
        b[r['currency']]=b.get(r['currency'],0)+float(r['change']);j+=1
    value=b.get('USDT',0)+b.get('SOL',0)*price;nav=value/units
    if nav>peak:peak=nav;running_peak_ts=ts
    dd=1-nav/peak
    if dd>maxdd:maxdd=dd;dd_peak_ts=running_peak_ts;dd_trough_ts=ts
    curve.append({'ts':ts,'price':price,'equity':value,'nav':nav,'position_pct':b.get('SOL',0)*price/value*100})
    prevprice=price
save('equity_curve',curve)
dur=sum(curve[i+1]['ts']-r['ts'] for i,r in enumerate(curve[:-1]))
weighted_pos=sum(r['position_pct']*(curve[i+1]['ts']-r['ts']) for i,r in enumerate(curve[:-1]))/dur
lowpos=sum(curve[i+1]['ts']-r['ts'] for i,r in enumerate(curve[:-1]) if r['position_pct']<15)/dur
largest_gaps=sorted([(signals[i+1]['ts_ms']-s['ts_ms'])/60000 for i,s in enumerate(signals[:-1])],reverse=True)[:5]
shadow_errors=Counter(str(s.get('extra',{}).get('adaptive_exit',{}).get('error')) for s in signals if isinstance(s.get('extra'),dict) and isinstance(s['extra'].get('adaptive_exit'),dict) and s['extra']['adaptive_exit'].get('error'))
reason_counts={name:sum(token in str(s.get('reason','')) for s in signals) for name,token in [('insufficient_profit','利润空间不足'),('low_position','仓位过低'),('risk_gate','风控拦截'),('btc_gate','BTC崩盘门控'),('news_blocks','新闻利空'),('above_last_sell','高于最近卖出价'),('daily_loss','今日亏损')]}
stimes=[s['ts_ms'] for s in signals]
sell_context=[]
for o in orders:
    if o['side']!='Sell':continue
    idx=max(0,bisect_right(stimes,o['ts'])-1);s=signals[idx];cost=float(s['cost_price'])
    sell_context.append({'ts':o['ts'],'price':o['price'],'qty':o['qty'],'cost_reference':cost,'reference_gross_pnl':(o['price']-cost)*o['qty'],'reason':o['reason']})
rapid=[]
for prev,o in zip(orders,orders[1:]):
    if prev['side']!=o['side'] and o['ts']-prev['ts']<=60*60000:
        rapid.append({'from':dt(prev['ts']),'to':dt(o['ts']),'side':prev['side']+'->'+o['side'],'minutes':(o['ts']-prev['ts'])/60000,'price_change_pct':(o['price']/prev['price']-1)*100,'first_value':prev['value'],'second_value':o['value']})
summary={'window':w,'valuation_end_utc':dt(mark_ts),'price_start':p0,'price_end':p1,'SOL_return_pct':(p1/p0-1)*100,'initial_balances':initial,'ending_balances':balances,'initial_equity_SOL_USDT':v0,'ending_equity_SOL_USDT':v1,'external_cashflow_USDT':flow_total,'net_pnl_cashflow_adjusted':v1-v0-flow_total,'simple_pnl_over_initial_pct':(v1-v0-flow_total)/v0*100,'unitized_return_pct':(curve[-1]['nav']-1)*100,'signal_sample_max_drawdown_pct':maxdd*100,'drawdown_peak_utc':dt(dd_peak_ts),'drawdown_trough_utc':dt(dd_trough_ts),'initial_inventory_hold_end_equity':hold,'difference_vs_initial_inventory_hold':v1-hold,'fees_USDT_equivalent':sum(o['fee_usdt'] for o in orders),'turnover_USDT':sum(o['value'] for o in orders),'execution_count':len(execs),'order_count':len(orders),'order_sides':dict(Counter(o['side'] for o in orders)),'db_order_count':len(trades),'db_orders_without_fills':len(set(db_orders)-set(by_order)),'fills_without_db_orders':len(set(by_order)-set(db_orders)),'quantity_mismatches':sum(abs(o['qty']-o['logged_qty'])>0.0000001 for o in orders if o['logged_qty'] is not None),'monthly':monthly,'weighted_average_position_pct':weighted_pos,'time_below_15pct_position_pct':lowpos*100,'signal_largest_gaps_minutes':largest_gaps,'signal_counts':dict(Counter(s['decision'] for s in signals)),'signal_reason_counts':reason_counts,'shadow_errors':dict(shadow_errors),'reference_cost_losing_sell_orders':sum(x['reference_gross_pnl']<0 for x in sell_context),'explicit_stop_orders':sum('触发止损' in o['reason'] for o in orders),'rapid_opposite_orders_60min':rapid}
save('summary',summary);save('orders',orders);save('sell_reference_context',sell_context)
print(json.dumps(summary,ensure_ascii=False,indent=2))
