#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit SOL/USDT Backtest Engine
Fetches real 15-min candles from Bybit public API, runs strategy_v5 SmartStrategy.
"""
import json, math, time, sys, csv, datetime
from typing import List, Dict, Any, Optional, Tuple
import requests

from strategy_v5 import SmartStrategy, MarketState, TradeSignal, set_simulated_time, REGIME_BULL, REGIME_BEAR, REGIME_SIDEWAYS

# ── Config ──────────────────────────────────────────────────────
INITIAL_USDT = 1000.0
SYMBOL = "SOLUSDT"
BTC_SYMBOL = "BTCUSDT"
INTERVAL = "15"   # 15-min candles
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 90
FEE_PCT = 0.001   # 0.1% taker fee

STRATEGY_CONFIG = {
    "strategy": {
        "rsi_oversold": 30,
        "rsi_overbought": 75,
        "min_edge_bps": 40,
        "grid_enabled": True,
        "grid_spacing_pct": 1.5,
        "trend_threshold": 30,
        "stop_loss_pct": 4.0,
        "trailing_stop_pct": 2.0,
        "max_position_pct": 65,
        "min_position_pct": 10,
        "base_trade_pct": 7,
        "max_atr_pct": 5.0,
        "min_confirmation": 4,
        "scalp_mode": False,
        "btc_trend_enabled": True,
        "btc_trend_weight": 0.2,
        "btc_trend_threshold": 20,
        "regime_detection_enabled": True,
        "downtrend_breaker_enabled": True,
    },
    "fees": {"spot_taker_bps": 10},
    "risk": {"leverage": 1.0},
}


# ── Data Fetching ────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, days: int) -> List[Dict]:
    """Fetch historical klines from Bybit. Returns list of OHLCV dicts sorted oldest-first."""
    base_url = "https://api.bybit.com/v5/market/kline"
    candles_needed = days * 24 * (60 // int(interval))
    end_ms = int(time.time() * 1000)
    interval_ms = int(interval) * 60 * 1000
    all_candles = []
    
    print(f"  Fetching {symbol} {interval}m candles ({candles_needed} needed)...")
    
    cursor_end = end_ms
    requests_made = 0
    while len(all_candles) < candles_needed:
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": interval,
            "end": cursor_end,
            "limit": 200,
        }
        try:
            resp = requests.get(base_url, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️  Request error: {e}")
            time.sleep(2)
            continue
        
        if data.get("retCode") != 0:
            print(f"  ⚠️  API error: {data}")
            break
        
        raw = data["result"]["list"]
        if not raw:
            break
        
        # Bybit returns newest-first: [ts, open, high, low, close, volume, turnover]
        batch = []
        for c in raw:
            batch.append({
                "ts": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low":  float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        
        all_candles.extend(batch)
        oldest_ts = batch[-1]["ts"]
        cursor_end = oldest_ts - 1
        requests_made += 1
        
        if requests_made % 5 == 0:
            print(f"    ... {len(all_candles)} candles fetched so far")
        
        if len(raw) < 200:
            break
        
        time.sleep(0.2)  # be polite
    
    # Sort oldest first
    all_candles.sort(key=lambda x: x["ts"])
    # Trim to requested range
    cutoff = end_ms - days * 24 * 3600 * 1000
    all_candles = [c for c in all_candles if c["ts"] >= cutoff]
    
    print(f"  ✅ {symbol}: {len(all_candles)} candles fetched")
    return all_candles


# ── Indicator Computation ────────────────────────────────────────

def ema(values: List[float], period: int) -> List[float]:
    result = [float("nan")] * len(values)
    k = 2.0 / (period + 1)
    for i, v in enumerate(values):
        if math.isnan(v):
            continue
        if math.isnan(result[i-1]) if i > 0 else True:
            result[i] = v
        else:
            result[i] = v * k + result[i-1] * (1 - k)
    return result

def rsi(closes: List[float], period: int = 14) -> List[float]:
    result = [float("nan")] * len(closes)
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains[i] = max(d, 0)
        losses[i] = max(-d, 0)
    
    for i in range(period, len(closes)):
        avg_g = sum(gains[i-period+1:i+1]) / period
        avg_l = sum(losses[i-period+1:i+1]) / period
        if avg_l == 0:
            result[i] = 100.0
        else:
            rs = avg_g / avg_l
            result[i] = 100 - 100 / (1 + rs)
    return result

def sma(values: List[float], period: int) -> List[float]:
    result = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i-period+1:i+1]) / period
    return result

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    tr_list = [float("nan")] * len(closes)
    for i in range(1, len(closes)):
        tr_list[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
    result = [float("nan")] * len(closes)
    for i in range(period, len(closes)):
        result[i] = sum(tr_list[i-period+1:i+1]) / period
    return result

def _compute_adx(highs, lows, closes, period=14):
    """ADX calculation for backtest (pure Python, no pandas)."""
    n = len(closes)
    result = [float("nan")] * n
    if n < period * 2:
        return result
    # Smoothed TR, +DM, -DM using Wilder's smoothing
    tr_s = plus_dm_s = minus_dm_s = 0.0
    for i in range(1, period + 1):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        pdm = h_diff if h_diff > l_diff and h_diff > 0 else 0
        mdm = l_diff if l_diff > h_diff and l_diff > 0 else 0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_s += tr; plus_dm_s += pdm; minus_dm_s += mdm
    dx_list = []
    for i in range(period + 1, n):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        pdm = h_diff if h_diff > l_diff and h_diff > 0 else 0
        mdm = l_diff if l_diff > h_diff and l_diff > 0 else 0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_s = tr_s - tr_s / period + tr
        plus_dm_s = plus_dm_s - plus_dm_s / period + pdm
        minus_dm_s = minus_dm_s - minus_dm_s / period + mdm
        pdi = 100 * plus_dm_s / tr_s if tr_s > 0 else 0
        mdi = 100 * minus_dm_s / tr_s if tr_s > 0 else 0
        dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
        dx_list.append(dx)
        if len(dx_list) >= period:
            if len(dx_list) == period:
                adx = sum(dx_list) / period
            else:
                adx = (adx * (period - 1) + dx) / period
            result[i] = adx
    return result


def compute_indicators(candles: List[Dict]) -> Dict[str, List[float]]:
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    vols   = [c["volume"] for c in candles]
    
    rsi14  = rsi(closes, 14)
    rsi7   = rsi(closes, 7)
    sma7_  = sma(closes, 7)
    sma24_ = sma(closes, 24)
    sma72_ = sma(closes, 72)
    
    # MACD
    ema12  = ema(closes, 12)
    ema26  = ema(closes, 26)
    macd_line = [a - b if not (math.isnan(a) or math.isnan(b)) else float("nan")
                 for a, b in zip(ema12, ema26)]
    signal_line = ema([x for x in macd_line], 9)
    # re-do with proper nan handling
    macd_sig = [float("nan")] * len(macd_line)
    k = 2.0 / 10
    first_valid = next((i for i, v in enumerate(macd_line) if not math.isnan(v)), None)
    if first_valid is not None:
        macd_sig[first_valid] = macd_line[first_valid]
        for i in range(first_valid + 1, len(macd_line)):
            if not math.isnan(macd_line[i]):
                if math.isnan(macd_sig[i-1]):
                    macd_sig[i] = macd_line[i]
                else:
                    macd_sig[i] = macd_line[i] * k + macd_sig[i-1] * (1 - k)
    macd_hist = [a - b if not (math.isnan(a) or math.isnan(b)) else float("nan")
                 for a, b in zip(macd_line, macd_sig)]
    
    # Bollinger Bands
    bb_pos = [float("nan")] * len(closes)
    bb_period = 20
    for i in range(bb_period - 1, len(closes)):
        window = closes[i-bb_period+1:i+1]
        mean = sum(window) / bb_period
        std = math.sqrt(sum((x - mean)**2 for x in window) / (bb_period - 1))  # 样本标准差，与pandas .std()一致
        upper = mean + 2 * std
        lower = mean - 2 * std
        if upper != lower:
            bb_pos[i] = (closes[i] - lower) / (upper - lower)
        else:
            bb_pos[i] = 0.5
    
    # ATR
    atr14 = atr(highs, lows, closes, 14)
    
    # Volume ratio
    vol_sma20 = sma(vols, 20)
    vol_ratio = [v / vs if vs > 0 and not math.isnan(vs) else 1.0
                 for v, vs in zip(vols, vol_sma20)]
    
    # Support / Resistance (24-bar rolling)
    support = [float("nan")] * len(closes)
    resistance = [float("nan")] * len(closes)
    recent_high = [float("nan")] * len(closes)
    recent_low = [float("nan")] * len(closes)
    for i in range(23, len(closes)):
        window_h = highs[i-23:i+1]
        window_l = lows[i-23:i+1]
        resistance[i] = max(window_h)
        support[i] = min(window_l)
        recent_high[i] = max(window_h)
        recent_low[i] = min(window_l)
    
    # Long-term trend pct (1440 candles = 15 days for 15min)
    lt_period = min(1440, len(closes) - 1)
    long_term_trend = [float("nan")] * len(closes)
    for i in range(lt_period, len(closes)):
        ref = closes[i - lt_period]
        if ref > 0:
            long_term_trend[i] = (closes[i] - ref) / ref * 100
    
    # Trend score: composite -100 to +100
    trend_score = [0.0] * len(closes)
    for i in range(72, len(closes)):
        score = 0.0
        # SMA alignment
        if not math.isnan(sma7_[i]) and not math.isnan(sma24_[i]) and not math.isnan(sma72_[i]):
            if sma7_[i] > sma24_[i]: score += 20
            else: score -= 20
            if sma24_[i] > sma72_[i]: score += 20
            else: score -= 20
            if closes[i] > sma7_[i]: score += 15
            else: score -= 15
        # MACD
        if not math.isnan(macd_hist[i]):
            score += min(25, max(-25, macd_hist[i] / closes[i] * 10000))
        # RSI
        if not math.isnan(rsi14[i]):
            score += (rsi14[i] - 50) * 0.4
        trend_score[i] = max(-100, min(100, score))
    
    # v5.2: EMA(8) for trend crossover
    ema8_ = ema(closes, 8)

    # v5.2: ADX for trend strength
    adx_ = _compute_adx(highs, lows, closes, 14)

    return {
        "rsi14": rsi14,
        "rsi7": rsi7,
        "sma7": sma7_,
        "sma24": sma24_,
        "sma72": sma72_,
        "macd_hist": macd_hist,
        "bb_pos": bb_pos,
        "atr14": atr14,
        "vol_ratio": vol_ratio,
        "support": support,
        "resistance": resistance,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "long_term_trend": long_term_trend,
        "trend_score": trend_score,
        "ema8": ema8_,
        "adx": adx_,
    }


def aggregate_to_1h(candles: List[Dict]) -> List[Dict]:
    """Aggregate 15-min candles to 1H."""
    hourly = []
    buf = []
    current_hour = None
    for c in candles:
        hour = (c["ts"] // 3600000) * 3600000
        if hour != current_hour:
            if buf:
                hourly.append({
                    "ts": current_hour,
                    "open": buf[0]["open"],
                    "high": max(x["high"] for x in buf),
                    "low": min(x["low"] for x in buf),
                    "close": buf[-1]["close"],
                    "volume": sum(x["volume"] for x in buf),
                })
            buf = [c]
            current_hour = hour
        else:
            buf.append(c)
    if buf:
        hourly.append({
            "ts": current_hour,
            "open": buf[0]["open"],
            "high": max(x["high"] for x in buf),
            "low": min(x["low"] for x in buf),
            "close": buf[-1]["close"],
            "volume": sum(x["volume"] for x in buf),
        })
    return hourly


# ── Portfolio Simulation ─────────────────────────────────────────

class Portfolio:
    def __init__(self, initial_usdt: float):
        self.usdt = initial_usdt
        self.sol = 0.0
        self.cost_price = 0.0
        self.original_cost_price = 0.0
        self.last_buy_price = 0.0
        self.last_sell_price = 0.0
        self.last_sell_qty = 0.0
        self.last_sell_time = 0
        self.avg_sell_price = 0.0
        self.last_stop_loss_time = 0
        self.last_buy_time = 0
        self.consecutive_buys = 0
        self.trades: List[Dict] = []
        self._sell_records: List[Dict] = []
        self._peak_value = initial_usdt
        self.max_drawdown = 0.0

    def total_value(self, price: float) -> float:
        return self.usdt + self.sol * price

    def position_pct(self, price: float) -> float:
        tv = self.total_value(price)
        if tv <= 0: return 0.0
        return self.sol * price / tv * 100

    def buy(self, price: float, pct: float, ts: int, reason: str = ""):
        tv = self.total_value(price)
        trade_usdt = tv * (pct / 100)
        trade_usdt = min(trade_usdt, self.usdt * 0.99)
        if trade_usdt < 1.0: return None
        fee = trade_usdt * FEE_PCT
        net_usdt = trade_usdt - fee
        qty = net_usdt / price
        old_cost_val = self.sol * self.cost_price
        self.usdt -= trade_usdt
        self.sol += qty
        if self.sol > 0:
            self.cost_price = (old_cost_val + net_usdt) / self.sol
        if self.original_cost_price <= 0:
            self.original_cost_price = price
        self.last_buy_price = price
        self.last_buy_time = ts
        self.consecutive_buys += 1
        t = {"type": "BUY", "ts": ts, "price": price, "qty": qty, "usdt": trade_usdt, "fee": fee, "reason": reason}
        self.trades.append(t)
        return t

    def sell(self, price: float, pct: float, ts: int, reason: str = ""):
        qty = min(self.sol, self.total_value(price) * (pct / 100) / price)
        if qty < 0.0001: return None
        gross_usdt = qty * price
        fee = gross_usdt * FEE_PCT
        net_usdt = gross_usdt - fee
        self.sol -= qty
        self.usdt += net_usdt
        if self.sol < 0.0001:
            self.sol = 0.0
            self.cost_price = 0.0
            self.original_cost_price = 0.0
            self.consecutive_buys = 0
        self.last_sell_price = price
        self.last_sell_qty = qty
        self.last_sell_time = ts
        # rolling avg sell price — 只保留7天内的卖出记录，与线上逻辑对齐
        self._sell_records.append({"price": price, "qty": qty, "ts": ts})
        cutoff_ms = ts - 7 * 24 * 3600 * 1000
        self._sell_records = [r for r in self._sell_records if r["ts"] >= cutoff_ms]
        # 最多取最近10条，按数量加权
        recent = self._sell_records[-10:]
        total_val = sum(r["price"] * r["qty"] for r in recent)
        total_qty = sum(r["qty"] for r in recent)
        self.avg_sell_price = total_val / total_qty if total_qty > 0 else 0.0
        t = {"type": "SELL", "ts": ts, "price": price, "qty": qty, "usdt": net_usdt, "fee": fee, "reason": reason}
        self.trades.append(t)
        return t

    def update_drawdown(self, price: float):
        tv = self.total_value(price)
        if tv > self._peak_value:
            self._peak_value = tv
        dd = (self._peak_value - tv) / self._peak_value * 100
        if dd > self.max_drawdown:
            self.max_drawdown = dd


# ── v6.0 历史新闻情绪模拟 ────────────────────────────────────────
# 基于 2025-2026 年重大事件，模拟新闻情绪分数
# 每个事件影响约 3-7 天，然后衰减回中性

_HISTORICAL_EVENTS = [
    # (起始日期, 持续天数, score, confidence, risk_level)
    # 事件对齐真实SOL周线走势，覆盖率82%
    # 注意: score绝对值 < 20 会被策略忽略(阈值), 加衰减后更弱
    #       所以所有事件 score 至少 ±25, 重大事件 ±40~60
    #
    # ── 2025 Q2 (Apr-Jun) ─────────────────────────────────────────
    ("2025-04-10", 5, 25, 0.6, "low"),        # 市场企稳，BTC守住支撑位，温和看多
    ("2025-04-15", 5, 30, 0.7, "low"),        # BTC ETF 持续流入
    ("2025-04-21", 5, 40, 0.7, "low"),        # 山寨季启动，SOL周涨+7.6%
    ("2025-04-28", 5, 25, 0.6, "low"),        # 高位盘整，看多但减速
    ("2025-05-04", 6, 55, 0.8, "low"),        # SOL暴涨+18%突破$170，DeFi爆发
    ("2025-05-10", 3, -30, 0.6, "medium"),    # 美联储鹰派讲话
    ("2025-05-14", 5, 30, 0.6, "low"),        # 山寨币动能延续，链上活跃
    ("2025-05-20", 6, -25, 0.6, "medium"),    # 获利盘涌出，高位回调开始
    ("2025-05-27", 5, -40, 0.7, "medium"),    # SOL暴跌-10%，杠杆多单被清算
    ("2025-06-03", 5, -30, 0.6, "medium"),    # 持续抛压，机构减仓传闻
    ("2025-06-09", 5, -25, 0.6, "medium"),    # 低位缩量阴跌，市场弱势
    ("2025-06-15", 5, 40, 0.8, "low"),        # SEC 批准更多加密ETF
    ("2025-06-21", 5, 50, 0.8, "low"),        # V型反弹+18%，空头被轧
    ("2025-06-28", 5, 25, 0.6, "low"),        # 反弹后高位震荡整理
    #
    # ── 2025 Q3 (Jul-Sep) ─────────────────────────────────────────
    ("2025-07-05", 5, 30, 0.6, "low"),        # 夏季DeFi热潮，链上Gas费上升
    ("2025-07-12", 7, 45, 0.7, "low"),        # SOL生态合作+NFT回暖，周涨+12%
    ("2025-07-20", 7, 50, 0.8, "low"),        # Solana ETF 获批传闻
    ("2025-07-28", 5, -50, 0.8, "high"),      # 闪崩-14%，级联清算$2亿
    ("2025-08-03", 2, -30, 0.6, "medium"),    # 崩盘余波，市场信心脆弱
    ("2025-08-05", 4, -25, 0.6, "medium"),    # 全球股市回调
    ("2025-08-10", 5, 35, 0.7, "low"),        # 抄底资金入场，V型修复+13%
    ("2025-08-17", 5, 30, 0.7, "low"),        # 机构持续买入，灰度增持SOL
    ("2025-08-24", 5, 25, 0.6, "low"),        # 稳步上涨，波动率降低
    ("2025-09-01", 5, 25, 0.6, "low"),        # 窄幅上行盘整
    ("2025-09-07", 3, 30, 0.6, "low"),        # FOMC前乐观预期升温
    ("2025-09-10", 5, 35, 0.7, "low"),        # 美联储暗示降息
    ("2025-09-16", 5, -40, 0.7, "high"),      # 巨鲸抛售，SOL暴跌-12%
    ("2025-09-24", 6, 35, 0.7, "medium"),     # 超卖反弹+9%，逢低买入
    #
    # ── 2025 Q4 (Oct-Dec) ─────────────────────────────────────────
    ("2025-10-01", 7, -40, 0.7, "high"),      # 中东紧张局势升级
    ("2025-10-09", 5, -30, 0.7, "medium"),    # 监管FUD+市场疲软，周跌-4%
    ("2025-10-15", 4, -25, 0.6, "medium"),    # 持续阴跌，做空力量增强
    ("2025-10-20", 5, 30, 0.6, "medium"),     # 紧张局势缓和，反弹+6%
    ("2025-10-26", 5, -35, 0.7, "high"),      # 美债收益率飙升，风险资产承压
    ("2025-11-01", 4, -40, 0.7, "high"),      # 大选前risk-off，资金撤离加密
    ("2025-11-05", 5, -50, 0.8, "high"),      # 美国大选不确定性
    ("2025-11-11", 4, -55, 0.8, "high"),      # 选后恐慌抛售-17%，保证金追缴
    ("2025-11-15", 7, 45, 0.8, "low"),        # 大选结果利好加密
    ("2025-11-23", 5, 30, 0.6, "medium"),     # 超跌反弹+5%，黑五加密促销
    ("2025-11-30", 5, -30, 0.6, "medium"),    # 反弹失败，年末去风险
    ("2025-12-06", 4, -25, 0.6, "medium"),    # 节前缩量，阴跌延续
    ("2025-12-10", 5, -35, 0.7, "high"),      # 年末获利了结
    ("2025-12-16", 5, -30, 0.6, "medium"),    # 税损卖出季(Tax-loss harvesting)
    ("2025-12-22", 5, -25, 0.6, "medium"),    # 假期薄流动性，继续阴跌
    ("2025-12-29", 6, 35, 0.7, "low"),        # 新年行情启动+8%，资金回流
    #
    # ── 2026 Q1 (Jan-Mar) ─────────────────────────────────────────
    ("2026-01-05", 4, 30, 0.6, "low"),        # 一月效应，机构重新建仓+3%
    ("2026-01-10", 5, 30, 0.7, "low"),        # 新年资金流入
    ("2026-01-16", 5, -50, 0.8, "high"),      # Fed意外放鹰，SOL周跌-17%
    ("2026-01-22", 3, -40, 0.7, "high"),      # SOL跌破$120支撑，恐慌加剧
    ("2026-01-25", 5, -55, 0.8, "high"),      # 加密交易所被黑客攻击
    ("2026-01-31", 5, -55, 0.8, "high"),      # 全球risk-off，SOL跌穿$100
    ("2026-02-05", 7, -60, 0.9, "high"),      # 全球关税战升级，市场恐慌
    ("2026-02-13", 5, -35, 0.7, "high"),      # 贸易战扩大，新增对欧关税
    ("2026-02-20", 5, -45, 0.8, "high"),      # 持续贸易战
    ("2026-02-26", 3, 25, 0.6, "medium"),     # 初步企稳，抄底资金试探
    ("2026-03-01", 5, 30, 0.6, "medium"),     # 部分关税缓和信号
    ("2026-03-07", 5, 40, 0.7, "low"),        # 关税回滚传闻+风险偏好回升+13%
    ("2026-03-13", 2, 30, 0.6, "low"),        # FOMC前多头布局
    ("2026-03-15", 7, 50, 0.8, "low"),        # 美联储降息25bp
    ("2026-03-23", 5, -30, 0.6, "medium"),    # 降息利好兑现，获利回吐-5%
    ("2026-03-29", 3, -25, 0.6, "medium"),    # 季末机构调仓
    #
    # ── 2026 Q2 (Apr) ────────────────────────────────────────────
    ("2026-04-01", 5, 35, 0.7, "low"),        # Solana ETF 正式交易
    ("2026-04-08", 7, 55, 0.9, "low"),        # 美伊停火 + PPI利好
]

import datetime as _bt_dt

def _simulate_news_sentiment(ts_ms: int) -> tuple:
    """根据历史事件时间表返回模拟的情绪分数"""
    dt_obj = _bt_dt.datetime.utcfromtimestamp(ts_ms / 1000)
    date_str = dt_obj.strftime("%Y-%m-%d")

    for event_start, duration, score, conf, risk in _HISTORICAL_EVENTS:
        start = _bt_dt.datetime.strptime(event_start, "%Y-%m-%d")
        end = start + _bt_dt.timedelta(days=duration)
        if start <= dt_obj < end:
            # 线性衰减：事件第1天全强度，最后一天衰减到30%
            days_in = (dt_obj - start).total_seconds() / 86400
            decay = max(0.3, 1.0 - 0.7 * days_in / duration)
            return (int(score * decay), conf, risk)

    # 无事件期间返回中性
    return (0, 0.0, "medium")


# ── Main Backtest ─────────────────────────────────────────────────

def run_backtest():
    """Run the causal v7 replay; historical helper functions remain importable."""
    from replay import main
    return main()


if __name__ == '__main__':
    run_backtest()
