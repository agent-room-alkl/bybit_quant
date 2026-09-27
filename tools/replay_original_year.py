"""Frozen pre-optimization strategy: original backtester and causal comparison."""
import contextlib
import argparse
import gzip
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import replay
from execution_policy import hold


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def pack(run):
    summary=replay.metrics(run['curve'],run['initial'])
    summary.update(annualized_return_pct=summary['return_pct'],final_equity=run['curve'][-1]['equity'],
                   orders=len(run['orders']),fees_usdt=run['fees'],unfilled_attempts=run['unfilled_attempts'])
    return summary


def monthly(run):
    rows=[];previous=run['initial'];groups={}
    for row in run['curve']:
        month=datetime.fromtimestamp((row['ts']-1)/1000,timezone.utc).strftime('%Y-%m')
        groups.setdefault(month,[]).append(row)
    for month,items in groups.items():
        rows.append({'month':month,**replay.metrics(items,previous)});previous=items[-1]['equity']
    return rows


def run_unified_cash():
    """Same causal fills as the -17.45% comparison, but start from 100% USDT."""
    logging.disable(logging.CRITICAL)
    start,end=map(replay.timestamp,('2025-09-27','2026-09-27'))
    frozen=ROOT/'data/original_3d6e134'
    original=load('frozen_strategy_v5',frozen/'strategy_v5.py')
    cfg=json.loads((frozen/'config.json').read_text(encoding='utf-8'))
    cfg['enable_trading']=False
    cache=ROOT/'data/market_cache';out=ROOT/'data/replay_original_year_cash';out.mkdir(exist_ok=True)
    prepared=[]
    for name in ('features_1758931200000_1774915200000_bbf4446d12bc.json.gz',
                 'features_1774915200000_1790467200000_9e81d3a1f033.json.gz'):
        with gzip.open(cache/name,'rt',encoding='utf-8') as f:prepared.extend(json.load(f))
    if [int(r['bar'][0]) for r in prepared]!=list(range(start,end,900000)):
        raise ValueError('Incomplete annual feature coverage')
    class LegacyRunner:
        _daily_stop_loss_count=0
        _last_stop_loss_date=''
        def on_risk_fill(self,price,now):pass
    def legacy_evaluate(unused,market,position,now,config,ctx):
        original.set_simulated_time(now)
        try:
            strategy=original.create_smart_strategy(config)
            signal=strategy.analyze(market,position.position_pct,ctx.get('last_buy_price',0),position.total_value_usdt)
            signal.intent='risk_exit' if signal.action=='SELL' and any(w in signal.reason for w in ('止损','趋势减仓','趋势清仓','再平衡')) else 'grid'
            signal.stop_distance_pct=0
            if signal.action not in ('BUY','SELL'):return signal
            side=signal.action.lower();stop=signal.action=='SELL' and '触发止损' in signal.reason
            risk=config['risk'];sc=config['strategy'];cooldown=risk.get('cooldown_min',10)
            if side=='buy':
                p=position.position_pct
                cooldown=max(cooldown,30 if p>70 else 20 if p>50 else 15 if p>30 else 0)
            elif not stop:cooldown=max(cooldown,8)
            if now-ctx['last_'+side]<cooldown*60000:return hold(signal,'Original fill cooldown')
            cap=risk.get('max_orders_per_symbol_per_day',15)
            if ctx[side+'_count']>=cap or ctx['buy_count']+ctx['sell_count']>=2*cap:return hold(signal,'Original daily count')
            ok,_=original.should_trade_gate(signal.action.title(),market.last_price,market.cost_price,market.rsi14,
                    market.sma24,market.sma72,market.atr_pct/100,sc,config['fees']['spot_taker_bps'],sc['min_edge_bps'],
                    position.position_pct,market.avg_sell_price,market.usdt_pct,is_stop_loss=stop,regime=market.regime)
            if not ok:return hold(signal,'Original outer trade gate')
            if side=='buy':
                if market.last_sell_price>0 and market.last_price>market.last_sell_price*1.001:return hold(signal,'Original last-sell price gate')
                signal.position_pct=min(signal.position_pct,max(0,sc['max_position_pct']-position.position_pct))
                daily_limit=risk.get('max_daily_volume_pct',80)
                proposed=min(position.usdt_balance,position.total_value_usdt*signal.position_pct/100)
                if ctx['buy_volume']+proposed>position.total_value_usdt*daily_limit/100:return hold(signal,'Original daily volume gate')
            return signal
        finally:original.set_simulated_time(0)
    saved=(replay.create_smart_strategy,replay.evaluate)
    print('Causal original strategy from 100% USDT',flush=True)
    try:
        replay.create_smart_strategy=lambda config:LegacyRunner()
        replay.evaluate=legacy_evaluate
        legacy=replay.simulate(prepared,cfg,initial_position_pct=0)
    finally:
        replay.create_smart_strategy,replay.evaluate=saved
    abcd_cfg=json.loads((ROOT/'configs/trend_candidate.json').read_text(encoding='utf-8'))
    abcd_cfg['enable_trading']=False
    print('ABCD trend candidate from 100% USDT',flush=True)
    abcd=replay.simulate(prepared,abcd_cfg,initial_position_pct=0)
    first=prepared[0]['f15']['close'];initial=1000
    sol_hold=[{'equity':initial*float(item['bar'][4])/first,'position_pct':100} for item in prepared]
    native=json.loads((out/'backtest_results.json').read_text(encoding='utf-8'))['summary']
    report={'start_utc':'2025-09-27','end_utc_exclusive':'2026-09-27','days':365,'initial_equity':initial,
            'initial_sol_pct':0,
            'native_original_backtester_cash':native,
            'causal_original_strategy_cash':{**pack(legacy),'monthly':monthly(legacy)},
            'abcd_trend_candidate_cash':{**pack(abcd),'monthly':monthly(abcd)},
            'sol_price_hold':replay.metrics(sol_hold,initial),
            'comparison_note':'Unified execution model. Both strategies start in 100% USDT, matching the original backtester default. The previously reported -17.45% and -9.59% used a 40% SOL opening inventory.'}
    (out/'unified_cash_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    brief={name:{k:report[name][k] for k in ('return_pct','max_drawdown_pct','final_equity','orders','fees_usdt','average_position_pct')}
           for name in ('causal_original_strategy_cash','abcd_trend_candidate_cash')}
    brief['sol_price_hold']=report['sol_price_hold']
    print(json.dumps(brief,ensure_ascii=False,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-cash-only',action='store_true')
    parser.add_argument('--unified-cash',action='store_true',help='Causal original and ABCD from 100 percent USDT')
    args=parser.parse_args()
    if args.unified_cash:
        run_unified_cash();return
    logging.disable(logging.CRITICAL)
    start,end=map(replay.timestamp,('2025-09-27','2026-09-27'))
    frozen=ROOT/'data/original_3d6e134';frozen.mkdir(exist_ok=True)
    for name in ('strategy_v5.py','backtest.py','vercel/lib/config.json'):
        (frozen/Path(name).name).write_bytes(subprocess.check_output(['git','show','3d6e134:'+name],cwd=ROOT))
    original=load('frozen_strategy_v5',frozen/'strategy_v5.py')
    current=sys.modules['strategy_v5'];sys.modules['strategy_v5']=original
    try:bt=load('frozen_backtest',frozen/'backtest.py')
    finally:sys.modules['strategy_v5']=current
    cfg=json.loads((frozen/'config.json').read_text(encoding='utf-8'))
    cfg['enable_trading']=False
    cache=ROOT/'data/market_cache';out=ROOT/('data/replay_original_year_cash' if args.native_cash_only else 'data/replay_original_year');out.mkdir(exist_ok=True)
    prepared=[]
    for name in ('features_1758931200000_1774915200000_bbf4446d12bc.json.gz',
                 'features_1774915200000_1790467200000_9e81d3a1f033.json.gz'):
        with gzip.open(cache/name,'rt',encoding='utf-8') as f:prepared.extend(json.load(f))
    if [int(r['bar'][0]) for r in prepared]!=list(range(start,end,900000)):
        raise ValueError('Incomplete annual feature coverage')
    # Exact 200 warmup bars followed by the requested 365-day window.
    sol={}
    for name in ('SOLUSDT_15_1758750300000_1774915200000.json','SOLUSDT_15_1774734300000_1790467200000.json'):
        for row in json.loads((cache/name).read_text()):sol[int(row[0])]=row
    solrows=[sol[t] for t in sorted(sol) if start-200*900000<=t<end]
    btcrows=replay.fetch_candles('BTCUSDT',15,start-200*900000,end,cache)
    def convert(rows):
        return [dict(ts=int(r[0]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=float(r[5])) for r in rows]
    data={'SOLUSDT':convert(solrows),'BTCUSDT':convert(btcrows)}
    bt.fetch_klines=lambda symbol,interval,days:data[symbol]
    bt.DAYS=365
    first=prepared[0]['f15']['close']
    original_portfolio=bt.Portfolio
    class InitialPortfolio(original_portfolio):
        def __init__(self,initial):
            super().__init__(initial)
            self.usdt=initial*.6;self.sol=initial*.4/first
            self.cost_price=first;self.original_cost_price=first
    bt.Portfolio=original_portfolio if args.native_cash_only else InitialPortfolio
    print('Running frozen original backtester (known lookahead retained)',flush=True)
    cwd=Path.cwd()
    try:
        os.chdir(out)
        with (out/'original_console.log').open('w',encoding='utf-8') as log,contextlib.redirect_stdout(log):
            native=bt.run_backtest()
    finally:os.chdir(cwd)
    print(json.dumps({'original_backtester':native['summary']}),flush=True)
    if args.native_cash_only:
        return

    # Preserve original per-tick strategy reconstruction and external gates while
    # sharing causal inputs, portfolio accounting and fill assumptions with v7.
    class LegacyRunner:
        _daily_stop_loss_count=0
        _last_stop_loss_date=''
        def on_risk_fill(self,price,now):pass
    def legacy_evaluate(unused,market,position,now,config,ctx):
        original.set_simulated_time(now)
        try:
            strategy=original.create_smart_strategy(config)
            signal=strategy.analyze(market,position.position_pct,ctx.get('last_buy_price',0),position.total_value_usdt)
            signal.intent='risk_exit' if signal.action=='SELL' and any(w in signal.reason for w in ('止损','趋势减仓','趋势清仓','再平衡')) else 'grid'
            signal.stop_distance_pct=0
            if signal.action not in ('BUY','SELL'):return signal
            side=signal.action.lower();stop=signal.action=='SELL' and '触发止损' in signal.reason
            risk=config['risk'];sc=config['strategy'];cooldown=risk.get('cooldown_min',10)
            if side=='buy':
                p=position.position_pct
                cooldown=max(cooldown,30 if p>70 else 20 if p>50 else 15 if p>30 else 0)
            elif not stop:cooldown=max(cooldown,8)
            if now-ctx['last_'+side]<cooldown*60000:return hold(signal,'Original fill cooldown')
            cap=risk.get('max_orders_per_symbol_per_day',15)
            if ctx[side+'_count']>=cap or ctx['buy_count']+ctx['sell_count']>=2*cap:return hold(signal,'Original daily count')
            ok,_=original.should_trade_gate(signal.action.title(),market.last_price,market.cost_price,market.rsi14,
                    market.sma24,market.sma72,market.atr_pct/100,sc,config['fees']['spot_taker_bps'],sc['min_edge_bps'],
                    position.position_pct,market.avg_sell_price,market.usdt_pct,is_stop_loss=stop,regime=market.regime)
            if not ok:return hold(signal,'Original outer trade gate')
            if side=='buy':
                if market.last_sell_price>0 and market.last_price>market.last_sell_price*1.001:return hold(signal,'Original last-sell price gate')
                signal.position_pct=min(signal.position_pct,max(0,sc['max_position_pct']-position.position_pct))
                daily_limit=risk.get('max_daily_volume_pct',80)
                proposed=min(position.usdt_balance,position.total_value_usdt*signal.position_pct/100)
                if ctx['buy_volume']+proposed>position.total_value_usdt*daily_limit/100:return hold(signal,'Original daily volume gate')
            return signal
        finally:original.set_simulated_time(0)
    replay.create_smart_strategy=lambda config:LegacyRunner()
    replay.evaluate=legacy_evaluate
    print('Running original strategy with causal common execution assumptions',flush=True)
    run=replay.simulate(prepared,cfg)
    (out/'causal_original_strategy.json').write_text(json.dumps(run,ensure_ascii=False),encoding='utf-8')
    summary=replay.metrics(run['curve'],run['initial'])
    summary.update(annualized_return_pct=summary['return_pct'],final_equity=run['curve'][-1]['equity'],orders=len(run['orders']),fees_usdt=run['fees'])
    report={'original_commit':'3d6e134','start_utc':'2025-09-27','end_utc_exclusive':'2026-09-27','days':365,
            'initial_equity':1000,'initial_sol_pct':40,'original_backtester':native['summary'],
            'causal_original_strategy':summary,
            'frozen_source_hashes':{n:hashlib.sha256((frozen/n).read_bytes()).hexdigest() for n in ('strategy_v5.py','backtest.py','config.json')},
            'limitations':{
                'original_backtester':['Original hour-candle lookahead retained.','Same-close fills and no slippage.','Sell percentages use fraction of SOL holdings.','Synthetic historical news retained.','Original buy-cost calculation and minimum sizes retained.','Initial allocation and exact date window supplied externally.'],
                'causal_original_strategy':['Original strategy is unmodified; per-tick reconstruction and legacy outer gates are adapted from original live code.','Shared corrected causal features, FIFO accounting, exchange minimums and IOC fill model differ from original live input/accounting defects.','No v7 risk budget or account-equity halt is applied; original daily PnL gate effectively remains zero as in old runtime.','No historical news stream; neutral news supplied.','This is a controlled approximation, not an exact historical live-account reconstruction.']}}
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'causal_original_strategy':summary}),flush=True)


if __name__=='__main__':main()
