"""Causal market inputs used unchanged by live trading and replay."""
import math
from indicators import klines_to_df, enrich_indicators
from strategy_v5 import MarketState

def closed_rows(rows, interval_minutes, now_ms):
    duration = interval_minutes*60000
    unique = {int(r[0]): r for r in rows if int(r[0])+duration <= now_ms}
    return [unique[t] for t in sorted(unique)]

def features(rows, interval_minutes, now_ms, limit):
    rows = closed_rows(rows, interval_minutes, now_ms)[-limit:]
    if len(rows)<100:
        raise ValueError('Insufficient closed candle history')
    if int(rows[-1][0])+interval_minutes*60000 < now_ms-interval_minutes*60000:
        raise ValueError('Stale closed candle history')
    if any(int(b[0])-int(a[0])!=interval_minutes*60000 for a,b in zip(rows,rows[1:])):
        raise ValueError('Candle history contains a gap')
    df = enrich_indicators(klines_to_df({'result':{'list':rows}}))
    last=df.iloc[-1]
    def val(key, fallback=0):
        v=float(last.get(key,fallback))
        return v if math.isfinite(v) else fallback
    close=float(last['close'])
    result={k:val(k) for k in ('RSI14','RSI7','MACD_Hist','BB_Position','Trend','ATR14','ATR_Pct','Volume_Ratio','SMA7','SMA24','SMA72','EMA8','ADX')}
    result.update(MACD_hist=result['MACD_Hist'],BB_position=result['BB_Position'],ATR_pct=result['ATR_Pct'])
    result.update(close=close,bar_ms=int(rows[-1][0]),high=float(df['high'].iloc[-20:].max()),low=float(df['low'].iloc[-20:].min()),support=float(df['low'].iloc[-24:].min()),resistance=float(df['high'].iloc[-24:].max()))
    # Exact elapsed periods; no substitution of a shorter range for "15 days".
    def change(periods):
        if len(rows)<=periods:raise ValueError('Insufficient trend lookback')
        return (close/float(rows[-1-periods][4])-1)*100
    if interval_minutes==60:
        result.update(change_24h=change(24),change_15d=change(360))
    return result

def make_market(f15, h1, btc_h1, price, cost, base, cash, orders=(), last_risk_exit=0, news=None):
    total=base*price+cash
    ordered=sorted(orders,key=lambda x:x['ts'])
    buys=[o for o in ordered if o['side']=='Buy'];sells=[o for o in ordered if o['side']=='Sell']
    buy=buys[-1] if buys else {};sell=sells[-1] if sells else {}
    consecutive=0
    for o in reversed(ordered):
        if o['side']!='Buy':break
        consecutive+=1
    recent=[o for o in sells[-10:] if o['ts']>=f15['bar_ms']-7*86400000]
    sold_qty=sum(o['qty'] for o in recent)
    avg_sell=sum(o['qty']*o['price'] for o in recent)/sold_qty if sold_qty else 0
    return MarketState(last_price=price,cost_price=cost,original_cost_price=cost,
        rsi14=f15['RSI14'],rsi7=f15['RSI7'],macd_hist=f15['MACD_hist'],
        bb_position=f15['BB_position'],trend_score=f15['Trend'],atr_pct=f15['ATR_pct'],
        volume_ratio=f15['Volume_Ratio'],support=f15['support'],resistance=f15['resistance'],
        sma7=f15['SMA7'],sma24=f15['SMA24'],sma72=f15['SMA72'],recent_high=f15['high'],recent_low=f15['low'],
        last_buy_time=buy.get('ts',0),last_sell_time=sell.get('ts',0),last_sell_price=sell.get('price',0),
        last_sell_qty=sell.get('qty',0),avg_sell_price=avg_sell,usdt_balance=cash,base_balance=base,
        usdt_pct=cash/total*100 if total else 100,base_pct=base*price/total*100 if total else 0,
        long_term_trend_pct=h1['change_15d'],btc_long_term_trend_pct=btc_h1['change_15d'],
        btc_change_24h_pct=btc_h1['change_24h'],btc_trend_score=btc_h1['Trend'],
        h1_trend_score=h1['Trend'],h1_sma7=h1['SMA7'],h1_sma24=h1['SMA24'],h1_sma72=h1['SMA72'],
        h1_rsi14=h1['RSI14'],h1_ema8=h1['EMA8'],adx=h1['ADX'],consecutive_buys=consecutive,
        last_stop_loss_time=last_risk_exit,closed_bar_ms=f15['bar_ms'],
        sell_execs_raw=[{'price':o['price'],'qty':o['qty'],'time_ms':o['ts']} for o in recent],**(news or {}))
