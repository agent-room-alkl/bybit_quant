"""Pure risk/size policy shared by live trading and historical replay."""
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math

VERSION = '7.0.0'

def trading_day(ts_ms, timezone_name='Pacific/Auckland'):
    return datetime.fromtimestamp(ts_ms/1000, timezone.utc).astimezone(ZoneInfo(timezone_name)).strftime('%Y%m%d')

def update_equity_risk(old, equity, cashflow, now, cfg):
    """Unitize external flows; persistent halt survives midnight/restart.

    The first observation of a new day is its baseline, explicitly not a guessed
    midnight valuation. Daily flows are marked at observation-time prices.
    """
    if not math.isfinite(equity) or equity <= 0 or not math.isfinite(cashflow):
        raise ValueError('Invalid account equity/cash flow')
    r = dict(old)
    day = trading_day(now, cfg.get('timezone','Pacific/Auckland'))
    if not r:
        r = {'units': equity, 'last_equity': equity, 'day': day, 'day_start_nav':1.0, 'day_peak_nav':1.0, 'halt_until':0, 'baseline_ms':now}
    else:
        before_flow = equity-cashflow
        if cashflow:
            if before_flow <= 0:
                raise ValueError('Cash flow cannot be unitized against nonpositive equity')
            r['units'] *= equity/before_flow
        nav = equity/r['units']
        if day != r['day']:
            r.update(day=day,day_start_nav=nav,day_peak_nav=nav,baseline_ms=now)
    nav = equity/r['units']
    r['day_peak_nav'] = max(r['day_peak_nav'],nav)
    r['daily_pnl_pct'] = (nav/r['day_start_nav']-1)*100
    r['intraday_drawdown_pct'] = (1-nav/r['day_peak_nav'])*100
    breach = r['daily_pnl_pct'] <= -float(cfg.get('max_daily_loss_pct',3)) or r['intraday_drawdown_pct'] >= float(cfg.get('max_intraday_drawdown_pct',5))
    if breach and now >= r.get('halt_until',0):
        r['halt_until'] = now+int(float(cfg.get('halt_hours',24))*3600000)
    r.update(last_equity=equity,last_ms=now)
    return r

def hold(signal, reason):
    return replace(signal,action='HOLD',position_pct=0,reason=reason)

def apply_policy(signal, market, position_pct, now, cfg, context):
    """Returns a clipped signal. A risk exit bypasses ordinary trade throttles."""
    if signal.action not in ('BUY','SELL'):
        return signal
    risk = cfg.get('risk',{}); strategy = cfg.get('strategy',{})
    if not math.isfinite(market.last_price) or market.last_price <= 0:
        return hold(signal,'价格无效')
    if context.get('pending_order'):
        return hold(signal,'待确认订单尚未完成对账')
    if signal.action == 'SELL' and signal.intent == 'risk_exit':
        return replace(signal,position_pct=min(position_pct,max(0,signal.position_pct)))
    if signal.action == 'BUY':
        if context.get('account_pending_buy'):return hold(signal,'账户另有待确认买单，暂停新增风险')
        if context.get('risk_exits_today',0)>=int(risk.get('max_risk_exits_per_day',4)):
            return hold(signal,'当日风险退出次数达到上限，暂停增加风险')
        if not context.get('data_complete',True):return hold(signal,'账户/成本/资金流水数据不完整，暂停增加风险')
        if now < context.get('halt_until',0):return hold(signal,'账户净值熔断，暂停增加风险')
        if market.btc_change_24h_pct <= -float(strategy.get('btc_crash_pct',3)):
            return hold(signal,'BTC 24小时跌幅门控')
        if now-market.last_stop_loss_time < float(risk.get('cooldown_after_loss_min',60))*60000 and market.last_stop_loss_time:
            return hold(signal,'风险退出后的买入冷却')
        if market.atr_pct > float(strategy.get('max_atr_pct',5)):
            return hold(signal,'入场波动率过高')
        if market.news_confidence>=0.7 and market.news_sentiment<=-50:
            return hold(signal,'高置信度新闻风险，暂停增加风险')
        if signal.intent != 'trend_entry':
            if strategy.get('trend_rebuild_enabled',False) and market.regime!='SIDEWAYS':
                return hold(signal,'网格仅在震荡状态入场')
            ttl=float(strategy.get('grid_sell_reference_hours',4))*3600000
            if market.last_sell_price>0 and market.last_sell_time and 0<=now-market.last_sell_time<ttl:
                required=(2*float(cfg.get('fees',{}).get('spot_taker_bps',10))+float(risk.get('slippage_bps',5))+float(strategy.get('min_edge_bps',40)))/10000
                if market.last_price>market.last_sell_price*(1-required):
                    return hold(signal,'有效期内网格回补价差不足以覆盖费用和净收益门槛')
    side=signal.action.lower()
    cooldown=float(risk.get('cooldown_min',10))*60000
    if signal.intent=='trend_entry':
        cooldown=max(cooldown,float(strategy.get('trend_entry_cooldown_min',60))*60000)
    if context.get('last_'+side,0) and now-context['last_'+side]<cooldown:
        return hold(signal,'同方向成交冷却')
    other='sell' if side=='buy' else 'buy'
    if context.get('last_'+other,0) and now-context['last_'+other]<float(risk.get('reverse_cooldown_min',15))*60000:
        return hold(signal,'反向成交冷却')
    if context.get(side+'_count',0)>=int(risk.get('max_orders_per_symbol_per_day',15)):
        return hold(signal,'当日该方向成交订单数达到上限')
    pct=min(signal.position_pct,float(risk.get('max_single_trade_pct',5)))
    if signal.action=='BUY':
        maxpos=float(strategy.get('max_position_pct',65))
        if strategy.get('trend_rebuild_enabled',False):
            maxpos=min(maxpos,float(strategy.get('bull_target_position_pct',40) if market.regime=='BULL' else strategy.get('sideways_max_position_pct',35) if market.regime=='SIDEWAYS' else strategy.get('bear_max_position_pct',15)))
        pct=min(pct,max(0,maxpos-position_pct))
        sub_equity=context.get('equity',0)
        if sub_equity>0:
            account_room=max(0,float(risk.get('max_account_position_pct',65))-context.get('account_position_pct',position_pct))
            pct=min(pct,account_room*context.get('account_equity',sub_equity)/sub_equity)
        # Percent-of-equity risk / fractional stop distance => % equity position.
        stop_pct=float(signal.stop_distance_pct or strategy.get('stop_loss_pct',4))
        friction=(2*float(cfg.get('fees',{}).get('spot_taker_bps',10))+float(risk.get('slippage_bps',5)))/100
        if stop_pct<=0:return hold(signal,'止损距离无效')
        pct=min(pct,float(risk.get('risk_per_trade_pct',0.25))*100/(stop_pct+friction))
        maxvol=float(risk.get('max_daily_volume_pct',80))
        equity=context.get('equity',0)
        if maxvol>0 and equity>0:
            pct=min(pct,max(0,maxvol-context.get('buy_volume',0)/equity*100))
    else:
        floor=float(strategy.get('min_position_pct',0))
        if strategy.get('trend_rebuild_enabled',False) and market.regime=='BULL':
            floor=max(floor,float(strategy.get('bull_target_position_pct',40)))
        pct=min(pct,max(0,position_pct-floor))
    if not math.isfinite(pct) or pct<=0:return hold(signal,'风险预算或目标仓位无可用额度')
    return replace(signal,position_pct=pct)
