import json
from dataclasses import replace
from types import SimpleNamespace
from copy import deepcopy
import pytest

from runtime_store import RuntimeStore
from execution_policy import apply_policy, update_equity_risk, trading_day
from execution_engine import reconcile, submit, inventory, aggregate_fills, context_from_orders, size_order
from trade_logic import calculate_position, InstrFilters, parse_instr_filters
from strategy_v5 import TradeSignal, create_smart_strategy, MarketState, set_simulated_time
from market_data import closed_rows, features

NOW=1785585600000
CFG={'risk':{'cooldown_min':10,'max_orders_per_symbol_per_day':4,'max_single_trade_pct':10,'risk_per_trade_pct':0.25,'slippage_bps':5},'strategy':{'stop_loss_pct':4,'trend_rebuild_enabled':True,'bull_target_position_pct':40,'min_position_pct':0},'fees':{'spot_taker_bps':10},'min_usdt_per_buy':5}

def market(**kw):
    m=MarketState(last_price=100,cost_price=95,rsi14=55,rsi7=55,macd_hist=1,bb_position=.5,trend_score=50,atr_pct=1,volume_ratio=1.2,support=90,resistance=110,sma7=99,sma24=98,sma72=97,h1_sma7=100,h1_sma24=98,h1_sma72=95,h1_trend_score=60,h1_rsi14=55,adx=30,regime='BULL',regime_confidence=1,base_balance=2,usdt_balance=800)
    return replace(m,**kw)

def fill(eid='e1',oid='o1',side='Buy',q='1',ts=NOW,link='v7_SOLUSDT_test'):
    return {'execId':eid,'orderId':oid,'orderLinkId':link,'symbol':'SOLUSDT','execTime':str(ts),'execQty':q,'execPrice':'100','execFee':str(float(q)*.001 if side=='Buy' else float(q)*.1),'feeCurrency':'SOL' if side=='Buy' else 'USDT','side':side,'execType':'Trade'}

def fresh():return {'schema':1,'symbols':{},'orders':{},'risk':{}}

def test_cashflow_is_not_profit_and_halt_survives_midnight():
    r=update_equity_risk({},1000,0,NOW,{'max_daily_loss_pct':3})
    r=update_equity_risk(r,1100,100,NOW+1000,{'max_daily_loss_pct':3})
    assert r['daily_pnl_pct']==pytest.approx(0)
    r=update_equity_risk(r,1040,0,NOW+2000,{'max_daily_loss_pct':3})
    assert r['halt_until']>NOW+86400000
    again=update_equity_risk(r,1040,0,NOW+12*3600000,{'max_daily_loss_pct':3})
    assert again['halt_until']==r['halt_until']

@pytest.mark.parametrize('ctx',[{'last_sell':NOW-1},{'sell_count':100},{'halt_until':NOW+999999},{'data_complete':False}])
def test_risk_exit_bypasses_entry_throttles(ctx):
    sig=TradeSignal('SELL',100,'risk',80,intent='risk_exit')
    assert apply_policy(sig,market(atr_pct=100),80,NOW,CFG,ctx).position_pct==80

def test_unknown_order_blocks_even_risk_exit_to_avoid_double_sale():
    sig=TradeSignal('SELL',100,'risk',80,intent='risk_exit')
    assert apply_policy(sig,market(),80,NOW,CFG,{'pending_order':True}).action=='HOLD'

def test_risk_budget_includes_friction():
    sig=TradeSignal('BUY',90,'trend',30,intent='trend_entry',stop_distance_pct=4)
    out=apply_policy(sig,market(),10,NOW,CFG,{'equity':1000})
    assert out.position_pct==pytest.approx(.25*100/4.25)

def test_trend_entry_can_exceed_old_sell_price_but_grid_cannot():
    m=market(last_sell_price=90,last_sell_time=NOW-3600000)
    sig=TradeSignal('BUY',90,'trend',5,intent='trend_entry')
    assert apply_policy(sig,m,10,NOW,CFG,{}).action=='BUY'
    assert apply_policy(replace(sig,intent='grid'),replace(m,regime='SIDEWAYS'),10,NOW,CFG,{}).action=='HOLD'

def test_expired_grid_sell_reference_does_not_lock_entry():
    m=market(regime='SIDEWAYS',last_sell_price=90,last_sell_time=NOW-5*3600000)
    assert apply_policy(TradeSignal('BUY',90,'grid',5),m,10,NOW,CFG,{}).action=='BUY'

def test_account_halt_blocks_only_new_risk():
    ctx={'halt_until':NOW+10000}
    assert apply_policy(TradeSignal('BUY',90,'trend',5,intent='trend_entry'),market(),10,NOW,CFG,ctx).action=='HOLD'

def test_profit_sales_preserve_bull_target():
    sig=TradeSignal('SELL',90,'profit',10)
    assert apply_policy(sig,market(),40,NOW,CFG,{}).action=='HOLD'
    assert apply_policy(sig,market(),45,NOW,CFG,{}).position_pct==5

def test_cost_accounts_for_base_fee():
    qty,cost=inventory([fill()])
    assert qty==pytest.approx(.999)
    assert cost==pytest.approx(100/.999)

def test_fill_reconciliation_is_idempotent_and_splits_count_once():
    state=fresh();client=SimpleNamespace()
    fs=[fill(),fill('e2',q='.5')]
    ss,orders,events=reconcile(state,'SOLUSDT',fs,client)
    assert len(events)==2 and len(orders)==1
    assert orders[0]['qty']==1.5
    assert reconcile(state,'SOLUSDT',fs,client)[2]==[]
    ctx=context_from_orders(orders,NOW,CFG,1000)
    assert ctx['buy_count']==1 and ctx['buy_volume']==150

def test_partial_fills_across_days_only_charge_current_day_volume():
    orders=aggregate_fills([fill(ts=NOW-86400000),fill('e2',q='.5')])
    ctx=context_from_orders(orders,NOW,CFG,1000)
    assert ctx['buy_volume']==50

def test_accepted_order_is_not_a_fill_and_survives_restart(tmp_path):
    store=RuntimeStore('test',tmp_path/'state.db');assert store.acquire()
    state=store.load()
    client=SimpleNamespace(place_order=lambda **kw:{'retCode':0,'result':{'orderId':'o1'}})
    order={'symbol':'SOLUSDT','side':'Buy','orderType':'Limit','qty':'1','price':'100'}
    out=submit(store,state,client,'SOLUSDT',TradeSignal('BUY',90,'entry',5),order,CFG,NOW)
    assert out['status']=='SUBMITTED'
    assert state['symbols']=={}
    store.close()
    store=RuntimeStore('test',tmp_path/'state.db');assert store.acquire()
    assert len(store.load()['orders'])==1
    assert store.connection.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0]==2
    store.close()

def test_timeout_keeps_durable_unknown_intent(tmp_path):
    store=RuntimeStore('test',tmp_path/'state.db');store.acquire();state=store.load()
    def fail(**kw):raise TimeoutError()
    client=SimpleNamespace(place_order=fail)
    order={'symbol':'SOLUSDT','side':'Buy','orderType':'Limit','qty':'1','price':'100'}
    assert submit(store,state,client,'SOLUSDT',TradeSignal('BUY',90,'entry',5),order,CFG,NOW)['status']=='UNKNOWN'
    assert list(store.load()['orders'].values())[0]['phase']=='UNKNOWN'
    store.close()

def test_partial_cancel_waits_for_actual_execution(tmp_path):
    state=fresh();state['orders']['v7_SOLUSDT_test']={'symbol':'SOLUSDT','phase':'SUBMITTED','intent':'grid'}
    client=SimpleNamespace(get_order_status=lambda *a:{'retCode':0,'result':{'list':[{'orderStatus':'Cancelled','cumExecQty':'1','orderId':'o1'}]}})
    reconcile(state,'SOLUSDT',[],client)
    assert state['orders']['v7_SOLUSDT_test']['phase']=='PENDING'
    reconcile(state,'SOLUSDT',[fill()],client)
    assert state['orders']['v7_SOLUSDT_test']['phase']=='Cancelled'

def test_account_lease_excludes_second_process(tmp_path):
    a=RuntimeStore('a',tmp_path/'s.db');b=RuntimeStore('a',tmp_path/'s.db')
    assert a.acquire();assert not b.acquire()
    a.release();assert b.acquire()
    with pytest.raises(RuntimeError):a.save(fresh())
    a.close();b.close()

def test_state_and_fill_event_commit_atomically(tmp_path):
    s=RuntimeStore('a',tmp_path/'s.db');s.acquire()
    state=s.load();event={'event_id':'fill:e1','ts_ms':NOW,'kind':'fill'}
    s.save(state,[event]);s.save(state,[event])
    assert s.connection.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0]==1
    bad=deepcopy(state);bad['bad']=float('nan')
    with pytest.raises(ValueError):s.save(bad,[{'event_id':'no','ts_ms':NOW}])
    assert 'bad' not in s.load()
    s.close()

def test_stop_exit_ignores_recent_buy_and_stop_cooldown():
    strategy=create_smart_strategy(CFG)
    sig=strategy._check_stop_loss(market(last_price=90,cost_price=100,last_stop_loss_time=NOW-1,last_buy_time=NOW-1),20,100)
    assert sig.action=='SELL' and sig.intent=='risk_exit' and sig.position_pct==20

def test_stop_counter_changes_only_on_fill_and_persists():
    strategy=create_smart_strategy(CFG);strategy.on_risk_fill(90,NOW)
    recovered=create_smart_strategy(CFG);recovered.restore_state(json.loads(json.dumps(strategy.export_state())))
    assert recovered._daily_stop_loss_count==1 and recovered._last_stop_loss_price==90

def test_regime_confirmation_survives_restart():
    first=create_smart_strategy(CFG)
    assert first._apply_regime_hysteresis('BULL',.55)[0]=='SIDEWAYS'
    second=create_smart_strategy(CFG);second.restore_state(first.export_state())
    assert second._apply_regime_hysteresis('BULL',.55)[0]=='BULL'

def test_trend_rebuild_and_bear_reduction():
    strategy=create_smart_strategy(CFG)
    strategy.config['trend_confirmation_hours']=1
    assert strategy._trend_rebuild(market(long_term_trend_pct=5),10).intent=='trend_entry'
    assert strategy._trend_rebuild(market(regime='BEAR'),60).position_pct==45

def test_size_order_uses_total_equity_percentage_for_sale():
    pos=calculate_position(6,400,100);filters=InstrFilters(.01,.0001,.0001,5)
    od=size_order(TradeSignal('SELL',100,'exit',20,intent='risk_exit'),pos,100,(99.99,100.01),filters,CFG,'SOLUSDT')
    assert float(od['qty'])==pytest.approx(2)

def test_spot_precision_and_minimum_amount():
    f=parse_instr_filters({'result':{'list':[{'priceFilter':{'tickSize':'.00001'},'lotSizeFilter':{'basePrecision':'.001','minOrderAmt':'5','minOrderQty':'.001'}}]}})
    assert f.qty_step==.001 and f.min_notional==5

def test_unclosed_candle_never_enters_features():
    rows=[[str(NOW+i*900000),str(100+i*.01),str(101+i*.01),str(99+i*.01),str(100+i*.01),'100','10000'] for i in range(202)]
    cutoff=NOW+201*900000
    a=features(rows,15,cutoff,200)
    rows[-1][4]='999999'
    assert features(rows,15,cutoff,200)==a
    assert a['bar_ms']==NOW+200*900000 and a['RSI14']==100

def test_hourly_features_require_full_15_day_history():
    rows=[[str(NOW+i*3600000),'100','101','99','100','100','10000'] for i in range(100)]
    with pytest.raises(ValueError):features(rows,60,NOW+100*3600000,400)

def test_missing_history_page_is_not_silently_accepted():
    from cost import fetch_execs
    client=SimpleNamespace(get_trade_history=lambda **kw:{'retCode':10001})
    with pytest.raises(RuntimeError):fetch_execs(client,'SOLUSDT',1,1000)

def test_account_exposure_caps_multi_symbol_buy():
    sig=TradeSignal('BUY',100,'trend',10,intent='trend_entry')
    ctx={'equity':500,'account_equity':1000,'account_position_pct':64}
    out=apply_policy(sig,market(),10,NOW,CFG,ctx)
    assert out.position_pct==2

def test_full_tick_does_not_resubmit_unconfirmed_order(tmp_path,monkeypatch):
    import sys
    import types
    import execution_engine as engine
    fake_db=types.ModuleType('db');fake_db.log_trade=lambda *a,**k:None
    fake_db.log_signal=lambda *a,**k:None;fake_db.set_meta=lambda *a,**k:None
    monkeypatch.setitem(sys.modules,'db',fake_db)
    monkeypatch.setattr(engine.time,'time',lambda:NOW/1000)
    def forced(strategy,m,pos,now,cfg,ctx):
        return apply_policy(TradeSignal('BUY',90,'test trend',5,intent='trend_entry'),m,pos.position_pct,now,cfg,ctx)
    monkeypatch.setattr(engine,'evaluate',forced)
    class Client:
        base='test';api_key='not-a-real-key';submissions=0
        def get_trade_history(self,**kw):
            rows=[fill(ts=NOW-3600000)] if kw['start_ms']<=NOW-3600000<=kw['end_ms'] else []
            return {'retCode':0,'result':{'list':rows}}
        def get_kline(self,symbol,interval,limit):
            step=int(interval)*60000
            rows=[[str(NOW-i*step),'100','101','99','100','100','10000'] for i in range(limit)]
            return {'retCode':0,'result':{'list':rows}}
        def get_orderbook(self,*a,**k):return {'retCode':0,'result':{}}
        def extract_best_prices(self,*a):return (99.99,100.01)
        def get_ticker(self,*a):return {'retCode':0,'result':{'list':[{'lastPrice':'100'}]}}
        def get_wallet_balance(self):return {'retCode':0,'time':NOW,'result':{'list':[{'totalEquity':'999.9','coin':[{'coin':'SOL','walletBalance':'.999','equity':'.999','usdValue':'99.9'},{'coin':'USDT','walletBalance':'900','equity':'900','usdValue':'900'}]}]}}
        def get_transaction_log(self,start,end,cursor=None,currency=None):
            rows=[{'id':'tx','tradeId':'e1','type':'TRADE','side':'Buy','transactionTime':str(NOW-3600000),'currency':'SOL','change':'.999','cashFlow':'1','cashBalance':'.999','tradePrice':'100','qty':'1'}] if start<=NOW-3600000<=end else []
            return {'retCode':0,'result':{'list':rows}}
        def get_order_status(self,*a):return {'retCode':0,'result':{'list':[{'orderStatus':'New','cumExecQty':'0','orderId':'new'}]}}
        def get_instruments_info(self,*a):return {'retCode':0,'result':{'list':[{'priceFilter':{'tickSize':'.01'},'lotSizeFilter':{'basePrecision':'.0001','minOrderAmt':'5'}}]}}
        def place_order(self,**kw):self.submissions+=1;return {'retCode':0,'result':{'orderId':'new'}}
    client=Client();store=RuntimeStore('a',tmp_path/'s.db');cfg=deepcopy(CFG);cfg['enable_trading']=True
    first=engine.run_step(client,cfg,'SOLUSDT',store=store)
    assert first['placed']['status']=='SUBMITTED'
    second=engine.run_step(client,cfg,'SOLUSDT',store=store)
    assert second['decision']=='HOLD' and client.submissions==1
    store.close()
