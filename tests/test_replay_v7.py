from copy import deepcopy
from dataclasses import replace
import pytest
import replay
from strategy_v5 import TradeSignal
from market_data import features

NOW=1785585600000
CFG={'risk':{'max_single_trade_pct':10,'risk_per_trade_pct':.25,'slippage_bps':5},'strategy':{'stop_loss_pct':4},'fees':{'spot_taker_bps':10},'min_usdt_per_buy':5}

def prepared(open_price=100):
    r15=[[str(NOW-i*900000),'100','101','99','100','100','10000'] for i in range(201)]
    rh=[[str(NOW-i*3600000),'100','101','99','100','100','10000'] for i in range(401)]
    return [{'bar':[str(NOW),str(open_price),'103','98','101','100','10000'],
             'f15':features(r15,15,NOW,200),'h1':features(rh,60,NOW,400),'btc':features(rh,60,NOW,400)}]

def test_next_open_changes_fills_not_decision_inputs(monkeypatch):
    prices=[]
    def decide(strategy,m,*args):
        prices.append(m.last_price)
        return TradeSignal('BUY',100,'test',5,intent='trend_entry')
    monkeypatch.setattr(replay,'evaluate',decide)
    a=replay.simulate(prepared(100),CFG);b=replay.simulate(prepared(102),CFG)
    assert prices==[100,100]
    assert len(a['orders'])==1 and len(b['orders'])==0
    assert b['unfilled_attempts']==1

def test_partial_fill_scenario_changes_inventory_and_fee(monkeypatch):
    monkeypatch.setattr(replay,'evaluate',lambda *a:TradeSignal('BUY',100,'test',5,intent='trend_entry'))
    full=replay.simulate(prepared(),CFG,fill_fraction=1)
    half=replay.simulate(prepared(),CFG,fill_fraction=.5)
    assert half['orders'][0]['qty']==pytest.approx(full['orders'][0]['qty']/2,abs=.0001)
    assert half['fees']==pytest.approx(full['fees']/2,abs=.0001)

def test_hourly_future_values_are_excluded():
    rows=[[str(NOW-i*3600000),'100','101','99','100','100','10000'] for i in range(401)]
    prior=features(rows,60,NOW+900000,400)
    rows[0][4]='1000000'
    assert features(rows,60,NOW+900000,400)==prior

def test_shadow_client_cannot_post():
    from shadow import ReadOnlyClient
    client=ReadOnlyClient(api_key='test',api_secret='test')
    with pytest.raises(RuntimeError):client.place_order('SOLUSDT','Buy','Limit','1','100')
