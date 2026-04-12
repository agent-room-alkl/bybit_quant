# -*- coding: utf-8 -*-
"""
STA 纯趋势跟踪策略 v1.1 (bug-fixed)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, Optional
import math, logging

log = logging.getLogger("trend_strategy")

@dataclass
class TrendState:
    price: float
    h1_trend: float
    trend_15m: float
    atr_pct: float
    long_term_pct: float
    h1_sma7: float
    h1_sma24: float
    adx: float
    rsi14: float
    volume_ratio: float
    btc_trend: float
    regime: str

@dataclass
class Signal:
    action: str
    pct: float
    reason: str

DEFAULT_CONFIG = {
    "h1_entry_threshold": 20,
    "h1_exit_threshold": -12,
    "trend_confirm_15m": 10,
    "adx_min": 18,
    "base_position_pct": 6,
    "max_position_pct": 45,
    "pyramid_interval_hours": 4,
    "pyramid_max_layers": 6,
    "atr_stop_mult": 2.5,
    "trailing_drawback_pct": 5.0,   # 固定回撤百分比（不再用%-of-%）
    "profit_lock_pct": 10,
    "bear_regime_block": True,
    "btc_crash_threshold": -40,
    "max_daily_trades": 8,
    "cooldown_minutes": 45,
}


class TrendFollowStrategy:
    def __init__(self, config: Dict[str, Any] = None):
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        for k, v in cfg.items():
            setattr(self, k, v)
        self._layers = 0
        self._entry_price = 0.0
        self._highest_price = 0.0
        self._peak_profit = 0.0
        self._last_trade_ts = 0
        self._daily_trades = 0
        self._last_trade_date = ""
        self._in_trend = False

    def analyze(self, s: TrendState, pos: float, ts_ms: int = 0) -> Signal:
        import datetime

        # NaN保护
        if math.isnan(s.price) or s.price <= 0:
            return Signal("HOLD", 0, "价格无效")

        # 重置每日计数
        today = datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d") if ts_ms > 0 else ""
        if today and today != self._last_trade_date:
            self._daily_trades = 0
            self._last_trade_date = today

        # ── FIX #1: 状态追踪（消除2-3%死区）──
        if pos > 2 and s.price > 0:
            self._highest_price = max(self._highest_price, s.price)
            if self._entry_price > 0:
                self._peak_profit = max(self._peak_profit,
                    (s.price - self._entry_price) / self._entry_price * 100)
        else:
            # pos <= 2%: 完全清零
            self._layers = 0
            self._entry_price = 0
            self._highest_price = 0
            self._peak_profit = 0
            self._in_trend = False

        # 趋势判断
        h1 = s.h1_trend if not math.isnan(s.h1_trend) else 0
        t15 = s.trend_15m if not math.isnan(s.trend_15m) else 0
        adx = s.adx if not math.isnan(s.adx) else 0

        trend_bullish = h1 > self.h1_entry_threshold and t15 > self.trend_confirm_15m and adx > self.adx_min
        trend_bearish = h1 < self.h1_exit_threshold

        # ── FIX #5+#6: 退出逻辑不受冷却和日限制 ──
        if pos > 2:
            exit_sig = self._check_exits(s, pos, ts_ms, trend_bearish, h1)
            if exit_sig:
                return exit_sig

        # 冷却只限制买入（FIX #5）
        if ts_ms > 0 and self._last_trade_ts > 0:
            minutes_since = (ts_ms - self._last_trade_ts) / 60000
            if minutes_since < self.cooldown_minutes:
                return Signal("HOLD", 0, f"冷却中({minutes_since:.0f}min)")

        # 日限制只限制买入（FIX #6）
        if self._daily_trades >= self.max_daily_trades:
            return Signal("HOLD", 0, f"今日已交易{self._daily_trades}次")

        # ── 入场逻辑 ──
        if trend_bullish and pos < self.max_position_pct:
            if self.bear_regime_block and s.regime == "BEAR":
                return Signal("HOLD", 0, "熊市禁止买入")
            if s.btc_trend < self.btc_crash_threshold:
                return Signal("HOLD", 0, f"BTC崩盘: {s.btc_trend:.0f}")

            if not self._in_trend:
                self._in_trend = True
                self._layers = 1
                self._entry_price = s.price
                self._highest_price = s.price
                self._peak_profit = 0
                return self._make_buy(self.base_position_pct, ts_ms,
                    f"趋势入场: H1={h1:.0f} 15m={t15:.0f} ADX={adx:.0f}")

            elif self._layers < self.pyramid_max_layers:
                if s.price > self._entry_price * 1.01:
                    pct = self.base_position_pct * max(0.5, 1.0 - self._layers * 0.1)
                    self._layers += 1
                    return self._make_buy(pct, ts_ms,
                        f"加仓#{self._layers}: H1={h1:.0f}, +{(s.price/self._entry_price-1)*100:.1f}%")

        # ── FIX #3: DCA建仓设置entry_price ──
        if pos < 15 and h1 > 5 and t15 > 0 and s.regime != "BEAR":
            if not self._in_trend:
                self._in_trend = True
                self._entry_price = s.price
                self._highest_price = s.price
                self._peak_profit = 0
                self._layers = 1
                pct = self.base_position_pct * 0.5
                return self._make_buy(pct, ts_ms,
                    f"趋势DCA: H1={h1:.0f} 15m={t15:.0f}, 试探建仓")

        return Signal("HOLD", 0, f"H1={h1:.0f} 15m={t15:.0f} ADX={adx:.0f} pos={pos:.0f}%")

    def _check_exits(self, s: TrendState, pos: float, ts_ms: int,
                     trend_bearish: bool, h1: float) -> Optional[Signal]:
        """退出检查（独立于冷却和日限制）"""

        # 1. 趋势转负 → 清仓
        if trend_bearish:
            self._in_trend = False
            return self._make_sell(pos, ts_ms,
                f"趋势退出: H1={h1:.0f}<{self.h1_exit_threshold}")

        # 2. FIX #7: 追踪止盈（固定回撤，不是%-of-%）
        if self._peak_profit > self.profit_lock_pct and self._entry_price > 0:
            curr = (s.price - self._entry_price) / self._entry_price * 100
            drawback = self._peak_profit - curr
            if drawback > self.trailing_drawback_pct:
                self._in_trend = False
                return self._make_sell(pos, ts_ms,
                    f"追踪止盈: 峰值{self._peak_profit:.1f}%→{curr:.1f}%, 回撤{drawback:.1f}%")

        # 3. ATR硬止损
        if self._entry_price > 0 and s.atr_pct > 0 and not math.isnan(s.atr_pct):
            stop = self._entry_price * (1 - s.atr_pct * self.atr_stop_mult / 100)
            if s.price < stop:
                self._in_trend = False
                loss = (self._entry_price - s.price) / self._entry_price * 100
                return self._make_sell(pos, ts_ms,
                    f"ATR止损: ${s.price:.2f}<${stop:.2f}, -{loss:.1f}%")

        # 4. 长期趋势深跌
        if s.long_term_pct < -12 and h1 < 10:
            self._in_trend = False
            return self._make_sell(pos, ts_ms,
                f"长期保护: 15d={s.long_term_pct:+.1f}%")

        return None

    def _make_buy(self, pct: float, ts_ms: int, reason: str) -> Signal:
        self._last_trade_ts = ts_ms
        self._daily_trades += 1
        return Signal("BUY", pct, reason)

    def _make_sell(self, pos: float, ts_ms: int, reason: str) -> Signal:
        self._last_trade_ts = ts_ms
        self._daily_trades += 1
        return Signal("SELL", pos, reason)
