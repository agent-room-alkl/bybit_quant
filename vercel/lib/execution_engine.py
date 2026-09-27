"""Account-locked live orchestrator with a durable outbox and fill reconciliation."""
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from runtime_store import RuntimeStore
from execution_policy import VERSION, apply_policy, update_equity_risk, trading_day
from market_data import features, make_market
from strategy_v5 import create_smart_strategy, TradeSignal
from trade_logic import calculate_position, calculate_trade_qty, generate_order, parse_instr_filters, round_to_step
from cost import fetch_execs
from inventory_ledger import inventory_from_transactions

TERMINAL = {'Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated'}
FLOW_TYPES = {'TRANSFER_IN','TRANSFER_OUT','DEPOSIT','WITHDRAW','WITHDRAWAL'}

def result(response):
    if not response or response.get('retCode') != 0:
        raise RuntimeError('Exchange read failed: '+str((response or {}).get('retCode')))
    return response.get('result',{})

def inventory(fills):
    qty=cost=0.0
    for f in sorted(fills,key=lambda e:(int(e['execTime']),e['execId'])):
        q,p,fee=float(f['execQty']),float(f['execPrice']),float(f.get('execFee') or 0)
        base=f['symbol'][:-4];ccy=f.get('feeCurrency') or (base if f['side']=='Buy' else 'USDT')
        if ccy not in (base,'USDT'):raise ValueError('Unsupported fee currency in cost ledger')
        if f['side']=='Buy':
            qty+=q-(fee if ccy==base else 0);cost+=q*p+(fee if ccy=='USDT' else 0)
        elif qty>0:
            removal=min(q+(fee if ccy==base else 0),qty)
            cost-=cost/qty*removal;qty-=removal
    return qty,cost/qty if qty>1e-10 else 0.0

def aggregate_fills(fills):
    groups=defaultdict(list)
    for f in fills:groups[f['orderId']].append(f)
    orders=[]
    for oid,fs in groups.items():
        q=sum(float(f['execQty']) for f in fs)
        orders.append({'order_id':oid,'link':fs[0].get('orderLinkId',''),'side':fs[0]['side'],
                       'qty':q,'price':sum(float(f['execQty'])*float(f['execPrice']) for f in fs)/q,
                       'ts':max(int(f['execTime']) for f in fs),
                       'executions':[{'ts':int(f['execTime']),'value':float(f['execQty'])*float(f['execPrice'])} for f in fs]})
    return sorted(orders,key=lambda o:o['ts'])

def context_from_orders(orders, now, cfg, equity):
    day=trading_day(now,cfg.get('risk',{}).get('timezone','Pacific/Auckland'))
    ctx={'equity':equity,'buy_count':0,'sell_count':0,'buy_volume':0,'sell_volume':0,'last_buy':0,'last_sell':0}
    for o in orders:
        side=o['side'].lower();ctx['last_'+side]=max(ctx['last_'+side],o['ts'])
        if o['link'].startswith(('smart_','v7_')):
            todays=[f for f in o.get('executions',[{'ts':o['ts'],'value':o['qty']*o['price']}])
                    if trading_day(f['ts'],cfg.get('risk',{}).get('timezone','Pacific/Auckland'))==day]
            if todays:
                ctx[side+'_count']+=1;ctx[side+'_volume']+=sum(f['value'] for f in todays)
    return ctx

def reconcile(state, symbol, incoming, client):
    """Idempotent fills + outbox state. Terminal status waits for all fills."""
    ss=state['symbols'].setdefault(symbol,{'fills':{},'strategy':{},'last_risk_exit':0})
    events=[]
    for f in incoming:
        eid=f['execId']
        if f.get('execType','Trade')!='Trade':continue
        if eid not in ss['fills']:
            ss['fills'][eid]=f
            events.append({'event_id':'fill:'+eid,'ts_ms':int(f['execTime']),'kind':'fill','data':f})
    orders=aggregate_fills(ss['fills'].values());by_link={o['link']:o for o in orders if o['link']}
    for link,o in state['orders'].items():
        if o['symbol']!=symbol:continue
        fill=by_link.get(link)
        if fill and o['intent']=='risk_exit':
            ss['last_risk_exit']=max(ss['last_risk_exit'],fill['ts'])
            if not o.get('risk_fill_recorded'):
                strategy=create_smart_strategy(o['strategy_config'])
                strategy.restore_state(ss['strategy']);strategy.on_risk_fill(fill['price'],fill['ts'])
                ss['strategy']=strategy.export_state();o['risk_fill_recorded']=True
        if o['phase'] in TERMINAL:continue
        rows=result(client.get_order_status(symbol,link)).get('list',[])
        if rows:
            row=rows[0];expected=float(row.get('cumExecQty') or 0);known=fill['qty'] if fill else 0
            o.update(exchange_status=row['orderStatus'],order_id=row.get('orderId',''),filled_qty=known)
            o['phase']=row['orderStatus'] if row['orderStatus'] in TERMINAL and abs(expected-known)<1e-9 else 'PENDING'
        else:
            # An absent order is NOT proof that a timed-out submission failed.
            o['phase']='UNKNOWN'
    return ss,orders,events

def cashflows(client, start, end, seen, prices):
    total=0.0;events=[];new_seen=set(seen)
    while start<end:
        stop=min(end,start+7*86400000-1);cursor=None;visited=set()
        while True:
            r=result(client.get_transaction_log(start,stop,cursor))
            for row in r.get('list',[]):
                if row['type'] not in FLOW_TYPES or row['id'] in new_seen:continue
                ccy=row['currency']
                if ccy not in prices:raise ValueError('Cannot value external cash flow currency')
                value=float(row['change'])*prices[ccy];total+=value;new_seen.add(row['id'])
                events.append({'event_id':'cashflow:'+row['id'],'ts_ms':int(row['transactionTime']),'kind':'cashflow','usd_value':value,'data':row})
            nxt=r.get('nextPageCursor')
            if not nxt or not r.get('list'):break
            if nxt in visited:raise RuntimeError('Repeated transaction cursor')
            visited.add(nxt);cursor=nxt
        start=stop+1
    return total,list(new_seen),events

def fetch_inventory_transactions(client,currency,start,end):
    rows={}
    while start<end:
        stop=min(end,start+7*86400000-1);cursor=None;visited=set()
        while True:
            r=result(client.get_transaction_log(start,stop,cursor,currency=currency))
            for row in r.get('list',[]):rows[row['id']]=row
            nxt=r.get('nextPageCursor')
            if not nxt or not r.get('list'):break
            if nxt in visited:raise RuntimeError('Repeated inventory cursor')
            visited.add(nxt);cursor=nxt
        start=stop+1
    return list(rows.values())

def evaluate(strategy, market, position, now, cfg, context):
    # Strategy clock is supplied by the shared engine, not monkey-patched wall time.
    from strategy_v5 import set_simulated_time
    set_simulated_time(now)
    # An unsellable remainder stays in equity/FIFO but cannot preempt all entries
    # with an impossible stop order. Real tradable holdings retain hard stops.
    sale_qty=round_to_step(position.base_balance,context.get('qty_step',0))
    market.untradeable_dust=(position.base_balance>0 and
        (sale_qty<context.get('min_qty',0) or sale_qty*market.last_price<context.get('min_notional',0)))
    try:
        signal=strategy.analyze(market,position.position_pct,context.get('last_buy_price',0),position.total_value_usdt)
        if now<context.get('halt_until',0) and position.position_pct>float(cfg.get('risk',{}).get('halt_target_position_pct',0)):
            signal=TradeSignal('SELL',100,'账户净值熔断风险减仓',position.position_pct-float(cfg.get('risk',{}).get('halt_target_position_pct',0)),intent='risk_exit')
        return apply_policy(signal,market,position.position_pct,now,cfg,context)
    finally:
        set_simulated_time(0)

def size_order(signal, position, price, book, instr, cfg, symbol):
    if signal.action not in ('BUY','SELL'):return None
    # Budget the cash fee/slippage reserve without changing the position denominator.
    from dataclasses import replace
    reserve=1+(float(cfg.get('fees',{}).get('spot_taker_bps',10))+float(cfg.get('risk',{}).get('slippage_bps',5)))/10000
    affordable=replace(position,usdt_balance=position.usdt_balance/reserve)
    qty,_=calculate_trade_qty(signal.action,affordable,signal.position_pct,price,instr,float(cfg.get('min_usdt_per_buy',5)),1.0)
    slip=float(cfg.get('risk',{}).get('slippage_bps',5))/10000
    bounded_book=(book[0]*(1-slip),book[1]*(1+slip))
    return generate_order(symbol,signal.action,qty,price,bounded_book,instr)

def submit(store,state,client,symbol,signal,order,cfg,now):
    link='v7_'+symbol+'_'+uuid.uuid4().hex[:12]
    intent={'symbol':symbol,'phase':'PLANNED','intent':signal.intent,'created_ms':now,'order':order,
            'reason':signal.reason,'strategy_config':{'strategy':cfg.get('strategy',{}),'fees':cfg.get('fees',{}),'risk':cfg.get('risk',{})}}
    state['orders'][link]=intent
    store.renew()
    store.save(state,[{'event_id':'intent:'+link,'ts_ms':now,'kind':'order_intent','data':intent}])
    try:
        response=client.place_order(symbol=symbol,side=order['side'],order_type=order['orderType'],qty=order['qty'],price=order['price'],tif='IOC',order_link_id=link,isLeverage=0)
        code=response.get('retCode')
        intent['phase']='SUBMITTED' if code==0 else 'Rejected' if code in (10001,170131,170133,170140) else 'UNKNOWN'
        intent['order_id']=response.get('result',{}).get('orderId','')
        intent['ret_code']=code
    except Exception:
        intent['phase']='UNKNOWN'
        response={'retCode':-1,'retMsg':'Submission outcome unknown; reconcile before retry'}
    store.save(state,[{'event_id':'ack:'+link,'ts_ms':now,'kind':'order_ack','phase':intent['phase'],'response':response}])
    return {'status':intent['phase'],'resp':response,'order_link_id':link}

def run_step(client,cfg,symbol,news_provider=None,store=None):
    import db
    account=hashlib.sha256((str(client.base)+':'+client.api_key).encode()).hexdigest()[:20]
    own_store=store is None
    if own_store:
        pg=None
        if hasattr(db,'_dsn'):
            import psycopg2
            pg=psycopg2.connect(db._dsn(),connect_timeout=15)
            pg.autocommit=True
        store=RuntimeStore(account,pg=pg)
    try:
        if not store.acquire():return {'decision':'HOLD','reason':'账户执行锁被占用','placed':None}
        state=store.load()
        now=int(time.time()*1000)
        ss=state['symbols'].get(symbol,{})
        if cfg.get('risk',{}).get('leverage_enabled',False):
            return {'decision':'HOLD','reason':'v7执行器仅支持无借款现货；杠杆需独立负债与利息模型','placed':None}
        if not symbol.endswith('USDT'):raise ValueError('Only USDT quoted spot pairs are supported')
        hist_start=ss.get('sync_ms',now-int(cfg.get('history_days_for_cost',365))*86400000)-86400000
        unresolved=[o['created_ms'] for o in state['orders'].values() if o['symbol']==symbol and o['phase'] not in TERMINAL]
        if unresolved:hist_start=min(hist_start,min(unresolved)-60000)
        ledger_start=ss.get('ledger_sync_ms',now-int(cfg.get('history_days_for_cost',365))*86400000)-86400000
        with ThreadPoolExecutor(max_workers=7) as pool:
            jobs={
                'fills':pool.submit(fetch_execs,client,symbol,hist_start,now),
                'f15':pool.submit(client.get_kline,symbol,interval='15',limit=201),
                'h1':pool.submit(client.get_kline,symbol,interval='60',limit=401),
                'btc':pool.submit(client.get_kline,'BTCUSDT',interval='60',limit=401),
                'book':pool.submit(client.get_orderbook,symbol,limit=5),
                'ticker':pool.submit(client.get_ticker,symbol),
                'inventory':pool.submit(fetch_inventory_transactions,client,symbol[:-4],ledger_start,now),
            }
            raw={k:f.result() for k,f in jobs.items()}
        ss,orders,events=reconcile(state,symbol,raw['fills'],client)
        ss['sync_ms']=now
        ledger=ss.setdefault('transactions',{})
        for row in raw['inventory']:
            if row['id'] not in ledger:
                ledger[row['id']]=row
                events.append({'event_id':'inventory:'+row['id'],'ts_ms':int(row['transactionTime']),'kind':'inventory_change','data':row})
        ss['ledger_sync_ms']=now
        store.save(state,events)
        if hasattr(db,'update_trade_status'):
            by_link={o['link']:o for o in orders if o['link']}
            for link,o in state['orders'].items():
                if o['symbol']!=symbol:continue
                fill=by_link.get(link,{})
                try:
                    db.update_trade_status(o.get('order_id',''),link,o['phase'],fill.get('qty',0),fill.get('price',0))
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning('Dashboard projection failed; durable fill ledger is intact')
        wallet_response=client.get_wallet_balance()
        wallet=result(wallet_response)['list'][0]
        wallet_ms=int(wallet_response.get('time') or int(time.time()*1000))
        coins={c['coin']:c for c in wallet['coin']}
        for c in coins.values():
            if float(c.get('spotBorrow') or 0)>0 or float(c.get('borrowAmount') or 0)>0:
                raise ValueError('Outstanding borrowing requires a margin-aware execution model')
        base_info=coins.get(symbol[:-4],{});cash_info=coins.get('USDT',{})
        total_base=float(base_info.get('walletBalance') or 0);total_cash=float(cash_info.get('walletBalance') or 0)
        base=max(0,total_base-float(base_info.get('locked') or 0));cash=max(0,total_cash-float(cash_info.get('locked') or 0))
        price=float(result(raw['ticker'])['list'][0]['lastPrice'])
        equity=float(wallet['totalEquity'])
        prices={'USDT':1.0,symbol[:-4]:price}
        for name,c in coins.items():
            amount=float(c.get('equity') or 0)
            if amount:prices[name]=float(c.get('usdValue') or 0)/amount
        cashflow=0;flow_events=[];flow_complete=True
        try:
            if state.get('cashflow_ms'):
                start_flow=max(state.get('cashflow_origin_ms',0)+1,state['cashflow_ms']-60000)
                cashflow,seen,flow_events=cashflows(client,start_flow,wallet_ms,state.get('flow_ids',[]),prices)
                state['flow_ids']=seen
            else:
                state['cashflow_origin_ms']=wallet_ms
            state['risk']=update_equity_risk(state['risk'],equity,cashflow,now,cfg.get('risk',{}))
            state['cashflow_ms']=wallet_ms
        except (RuntimeError,ValueError,KeyError):
            if not state['risk']:raise
            flow_complete=False
        store.save(state,flow_events)
        book=inventory_from_transactions(ledger.values(),ss['fills'].values())
        reconstructed,cost=float(book.qty),book.average
        complete=book.known and abs(reconstructed-total_base)<=max(1e-6,total_base*1e-7)
        if not complete:cost=0.0
        f15=features(result(raw['f15'])['list'],15,now,200)
        h1=features(result(raw['h1'])['list'],60,now,400)
        btc=features(result(raw['btc'])['list'],60,now,400)
        news=news_provider(cfg) if news_provider and cfg.get('strategy',{}).get('news_enabled',False) else {}
        market=make_market(f15,h1,btc,price,cost,total_base,total_cash,orders,ss['last_risk_exit'],news)
        position=calculate_position(total_base,total_cash,price)
        ctx=context_from_orders(orders,now,cfg,position.total_value_usdt)
        buys=[o for o in orders if o['side']=='Buy']
        ctx.update(data_complete=complete and flow_complete,halt_until=state['risk'].get('halt_until',0),last_buy_price=buys[-1]['price'] if buys else 0,
                   account_equity=equity,account_position_pct=max(0,(equity-float(cash_info.get('usdValue') or total_cash))/equity*100),
                   account_pending_buy=any(o['order']['side']=='Buy' and o['phase'] not in TERMINAL for o in state['orders'].values()),
                   pending_order=any(o['symbol']==symbol and o['phase'] not in TERMINAL for o in state['orders'].values()))
        instr=parse_instr_filters({'result':result(client.get_instruments_info(symbol))})
        ctx.update(qty_step=instr.qty_step,min_qty=instr.min_qty,min_notional=instr.min_notional)
        strategy=create_smart_strategy(cfg);strategy.restore_state(ss['strategy'])
        ctx['risk_exits_today']=strategy._daily_stop_loss_count if strategy._last_stop_loss_date==trading_day(now,cfg.get('risk',{}).get('timezone','Pacific/Auckland')) else 0
        signal=evaluate(strategy,market,position,now,cfg,ctx)
        ss['strategy']=strategy.export_state()
        fingerprint=hashlib.sha256(json.dumps({'strategy':cfg.get('strategy',{}),'risk':cfg.get('risk',{}),'fees':cfg.get('fees',{})},sort_keys=True).encode()).hexdigest()[:16]
        shadow=strategy.classify_shadow(market,position.position_pct,daily_pnl_pct=state['risk']['daily_pnl_pct']).as_dict()
        snapshot={'ts_ms':now,'symbol':symbol,'last_price':price,'cost_price':cost,'position_pct':position.position_pct,'total_value_usdt':position.total_value_usdt,
                  'decision':signal.action,'reason':signal.reason,'confidence':signal.confidence,'intent':signal.intent,'placed':None,'shadow':shadow,
                  'version':VERSION,'config_hash':fingerprint,'data_complete':complete and flow_complete,'risk':dict(state['risk']),
                  'rsi14':market.rsi14,'rsi7':market.rsi7,'macd_hist':market.macd_hist,'bb_position':market.bb_position,'trend_score':market.trend_score,
                  'atr_pct':market.atr_pct,'sma7':market.sma7,'sma24':market.sma24,'sma72':market.sma72,'vol':market.atr_pct/100,'support':market.support,'resistance':market.resistance}
        gap=(now-ss.get('last_signal_ms',now))/1000
        snapshot['health']={'signal_gap_seconds':gap,'gap_warning':gap>float(cfg.get('risk',{}).get('signal_gap_alert_sec',300)),
                            'cashflow_complete':flow_complete,'cost_reconciled':complete}
        ss['last_signal_ms']=now
        book=client.extract_best_prices(raw['book'])
        if any(v is None or not math.isfinite(v) or v<=0 for v in book):raise ValueError('Invalid orderbook')
        # Preserve total equity denominator while capping actual quantity by free balances.
        from dataclasses import replace
        spendable=replace(position,base_balance=base,usdt_balance=cash)
        order=size_order(signal,spendable,price,book,instr,cfg,symbol)
        if signal.action in ('BUY','SELL') and order is None:
            snapshot.update(decision='HOLD',reason='风险预算下订单小于交易所最小金额或可用余额')
        store.save(state,[{'event_id':'signal:'+symbol+':'+str(now),'ts_ms':now,'kind':'signal','snapshot':snapshot,'market':asdict(market),'signal':asdict(signal),'policy_context':ctx}])
        if order and cfg.get('enable_trading',False):
            snapshot['placed']=submit(store,state,client,symbol,signal,order,cfg,now)
            db.log_trade(now,symbol,order['side'],order['qty'],order['price'],'Limit','IOC',signal.reason,snapshot['placed']['status'],snapshot['placed']['resp'].get('result',{}).get('orderId',''),{**snapshot['placed']['resp'],'orderLinkId':snapshot['placed']['order_link_id']})
        db.log_signal(now,symbol,price,cost,market.rsi14,market.sma7,market.sma24,market.sma72,market.atr_pct/100,*book,snapshot['decision'],snapshot['reason'],extra={**shadow,'version':VERSION,'config_hash':fingerprint,'risk':state['risk'],'data_complete':complete,'intent':signal.intent,'health':snapshot['health']})
        db.set_meta('v7_cost_'+symbol.upper(),json.dumps({'ts_ms':now,'quantity':total_base,'cost':cost,'complete':complete}))
        # Compatibility display fields, derived from fills, never acknowledgements.
        day=trading_day(now)
        for side in ('buy','sell'):
            db.set_meta(f'cnt_{side}_{symbol}_{day}',str(ctx[side+'_count']))
            db.set_meta(f'daily_vol_{side}_{symbol}_{day}',str(ctx[side+'_volume']))
            db.set_meta(f'last_{side}_{symbol}',str(ctx['last_'+side]))
        db.set_meta(f'daily_pnl_{symbol}_{day}',str(state['risk']['daily_pnl_pct']))
        return snapshot
    finally:
        if own_store:store.close()
        else:store.release()
