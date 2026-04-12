#!/usr/bin/env python3
"""STA 趋势策略回测引擎"""
import sys, math, time, datetime, json, csv
from typing import List, Dict
from backtest import fetch_klines, compute_indicators, aggregate_to_1h, _compute_adx, ema, sma
from strategy_trend import TrendFollowStrategy, TrendState, Signal, DEFAULT_CONFIG

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 365
INITIAL = 1000.0
FEE = 0.001

class Portfolio:
    def __init__(self, usdt):
        self.usdt = usdt
        self.sol = 0.0
        self.cost = 0.0
        self.trades = []
        self.peak_value = usdt
        self.max_dd = 0.0

    def value(self, price): return self.usdt + self.sol * price

    def buy(self, price, pct, ts):
        tv = self.value(price)
        amount = min(tv * pct / 100, self.usdt * 0.99)
        if amount < 1: return
        fee = amount * FEE
        qty = (amount - fee) / price
        old = self.sol * self.cost
        self.usdt -= amount
        self.sol += qty
        self.cost = (old + amount - fee) / self.sol if self.sol > 0 else price
        self.trades.append({"type": "BUY", "ts": ts, "price": price, "qty": qty, "usdt": amount, "fee": fee})

    def sell_all(self, price, ts):
        if self.sol < 0.0001: return
        proceeds = self.sol * price * (1 - FEE)
        fee = self.sol * price * FEE
        self.trades.append({"type": "SELL", "ts": ts, "price": price, "qty": self.sol, "usdt": proceeds, "fee": fee})
        self.usdt += proceeds
        self.sol = 0
        self.cost = 0

    def update_dd(self, price):
        v = self.value(price)
        self.peak_value = max(self.peak_value, v)
        dd = (self.peak_value - v) / self.peak_value * 100
        self.max_dd = max(self.max_dd, dd)

    def pos_pct(self, price):
        v = self.value(price)
        return (self.sol * price / v * 100) if v > 0 else 0


def run(config_override=None):
    cfg = {**DEFAULT_CONFIG, **(config_override or {})}

    print(f"\n📥 Fetching {DAYS}-day data...")
    sol = fetch_klines('SOLUSDT', '15', DAYS + 5)
    btc = fetch_klines('BTCUSDT', '15', DAYS + 5)
    print("📊 Computing indicators...")
    sol_ind = compute_indicators(sol)
    btc_ind = compute_indicators(btc)
    sol_1h = aggregate_to_1h(sol)
    sol_1h_ind = compute_indicators(sol_1h)
    sol_1h_by_ts = {c['ts']: i for i, c in enumerate(sol_1h)}
    btc_by_ts = {c['ts']: i for i, c in enumerate(btc)}

    def g(ind, idx, key, default=0.0):
        v = ind[key][idx]
        return default if math.isnan(v) else v

    strategy = TrendFollowStrategy(cfg)
    portfolio = Portfolio(INITIAL)
    WARMUP = 200
    equity_curve = []
    daily_returns = []
    last_day_val = INITIAL
    last_day = ""

    print(f"🔄 Running on {len(sol)-WARMUP} candles...")
    for i in range(WARMUP, len(sol)):
        c = sol[i]
        price = c['close']
        ts = c['ts']

        # FIX #18: 用上一个完成的1H candle（避免look-ahead）
        prev_h1_ts = ((ts // 3600000) - 1) * 3600000
        h1_idx = sol_1h_by_ts.get(prev_h1_ts)
        # FIX #12: 用 is not None 替代 if h1_idx（0是有效索引）
        # FIX #13: BTC也用前一小时
        prev_btc_ts = prev_h1_ts
        btc_idx = btc_by_ts.get(ts)
        if btc_idx is None:
            btc_idx = btc_by_ts.get(prev_btc_ts, 0)

        # FIX #14: 简单体制检测（用15天趋势近似）
        lt = g(sol_ind, i, 'long_term_trend')
        regime = "BEAR" if lt < -15 else ("BULL" if lt > 10 else "SIDEWAYS")

        state = TrendState(
            price=price,
            h1_trend=g(sol_1h_ind, h1_idx, 'trend_score') if h1_idx is not None else 0,
            trend_15m=g(sol_ind, i, 'trend_score'),
            atr_pct=g(sol_ind, i, 'atr14') / price * 100 if price > 0 else 0,
            long_term_pct=lt,
            h1_sma7=g(sol_1h_ind, h1_idx, 'sma7') if h1_idx is not None else 0,
            h1_sma24=g(sol_1h_ind, h1_idx, 'sma24') if h1_idx is not None else 0,
            adx=g(sol_1h_ind, h1_idx, 'adx', 25) if h1_idx is not None else 25,
            rsi14=g(sol_ind, i, 'rsi14', 50),
            volume_ratio=g(sol_ind, i, 'vol_ratio', 1.0),
            btc_trend=g(btc_ind, btc_idx, 'trend_score') if btc_idx is not None else 0,
            regime=regime,
        )

        pos = portfolio.pos_pct(price)
        sig = strategy.analyze(state, pos, ts)

        if sig.action == "BUY" and sig.pct > 0:
            portfolio.buy(price, sig.pct, ts)
        elif sig.action == "SELL" and pos > 2:
            portfolio.sell_all(price, ts)

        portfolio.update_dd(price)
        equity_curve.append({"ts": ts, "value": portfolio.value(price)})

        dt = datetime.datetime.fromtimestamp(ts/1000)
        day = dt.strftime("%Y-%m-%d")
        if day != last_day and last_day:
            v = portfolio.value(price)
            if last_day_val > 0:
                daily_returns.append((v - last_day_val) / last_day_val)
            last_day_val = v
        last_day = day

        # Progress
        pct_done = (i - WARMUP) / (len(sol) - WARMUP) * 100
        if int(pct_done) % 10 == 0 and abs(pct_done - int(pct_done)) < 0.05:
            v = portfolio.value(price)
            print(f"  {pct_done:5.0f}% | ${price:.0f} | ${v:.0f} ({(v/INITIAL-1)*100:+.1f}%)")

    # Results
    final = portfolio.value(sol[-1]['close'])
    ret = (final / INITIAL - 1) * 100
    start_p = sol[WARMUP]['close']
    bh_ret = (sol[-1]['close'] / start_p - 1) * 100
    ann = ((1 + ret / 100) ** (365.0 / max(DAYS, 1)) - 1) * 100
    buys = [t for t in portfolio.trades if t['type'] == 'BUY']
    sells = [t for t in portfolio.trades if t['type'] == 'SELL']
    total_fees = sum(t['fee'] for t in portfolio.trades)

    # Sharpe/Sortino
    sharpe = sortino = 0
    if daily_returns:
        avg_r = sum(daily_returns) / len(daily_returns)
        std_r = math.sqrt(sum((r-avg_r)**2 for r in daily_returns) / len(daily_returns))
        sharpe = (avg_r / std_r * math.sqrt(365)) if std_r > 0 else 0
        neg = [r for r in daily_returns if r < 0]
        if neg:
            ds = math.sqrt(sum(r**2 for r in neg) / len(neg))
            sortino = (avg_r / ds * math.sqrt(365)) if ds > 0 else 0
    pf_pos = sum(r for r in daily_returns if r > 0)
    pf_neg = abs(sum(r for r in daily_returns if r < 0))
    pf = pf_pos / pf_neg if pf_neg > 0 else 0
    win_days = len([r for r in daily_returns if r > 0])
    trade_days = len([r for r in daily_returns if r != 0])
    win_rate = win_days / trade_days * 100 if trade_days > 0 else 0

    # Monthly
    monthly = {}
    for eq in equity_curve:
        m = datetime.datetime.fromtimestamp(eq['ts']/1000).strftime('%Y-%m')
        if m not in monthly: monthly[m] = {"s": eq['value'], "e": eq['value']}
        monthly[m]['e'] = eq['value']

    print(f"\n{'='*60}")
    print(f"📊 STA 趋势策略回测 ({DAYS}天)")
    print(f"{'='*60}")
    print(f"  总收益:      {ret:+.2f}%")
    print(f"  年化:        {ann:+.2f}%")
    print(f"  Buy&Hold:    {bh_ret:+.2f}%")
    print(f"  Alpha:       {ret-bh_ret:+.2f}%")
    print(f"  最大回撤:    {portfolio.max_dd:.2f}%")
    print(f"  Sharpe:      {sharpe:.2f}")
    print(f"  Sortino:     {sortino:.2f}")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  交易:        {len(portfolio.trades)} ({len(buys)}买 {len(sells)}卖)")
    print(f"  胜率(日):    {win_rate:.1f}%")
    print(f"  手续费:      ${total_fees:.2f}")
    print(f"  最终SOL:     {portfolio.sol:.4f} (${portfolio.sol*sol[-1]['close']:.2f})")
    print(f"  最终USDT:    ${portfolio.usdt:.2f}")
    print(f"\n📅 月度:")
    for m in sorted(monthly):
        v = monthly[m]
        r = (v['e']-v['s'])/v['s']*100 if v['s']>0 else 0
        print(f"  {m}  ${v['s']:>8.2f} → ${v['e']:>8.2f}  {r:>+6.2f}%")

    # Save trades
    with open('backtest_trend_trades.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['type','ts','price','qty','usdt','fee'])
        w.writeheader()
        for t in portfolio.trades:
            w.writerow(t)

    return ret, portfolio.max_dd, sharpe, len(portfolio.trades)


if __name__ == "__main__":
    run()
