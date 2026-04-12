# Bybit Quant Strategy v5.0 - Improvements Analysis

**Created by:** Atlas (AI Assistant)
**Date:** 2026-03-22
**Based on:** Robin's bybit_quant project (5,651 lines, SOL/USDT trading)

---

## Executive Summary

The original strategy (v3.0/v4.3) is well-engineered but has critical weaknesses in bear market conditions. The grid trading approach accumulates heavy losses during sustained downtrends. My v5.0 improvements focus on **capital preservation first, profits second**.

---

## Root Cause Analysis

### Why the bot is losing money:

1. **Grid Trading in Downtrend** — Grid bots buy repeatedly as price falls, accumulating positions at increasingly bad prices.

2. **Slow Regime Switching** — The 2-confirmation hysteresis delay means the bot continues buying for several candles before recognizing a BEAR market.

3. **Stop-Loss Too Loose** — 2.5% stop-loss with 0.6% grid spacing = 4 grid buys before hitting stop. That's 4x position accumulation in a falling market.

4. **No Daily Drawdown Circuit Breaker** — If portfolio drops 10% in a day, the bot should halt, but it doesn't.

5. **BTC Correlation as Signal, Not Gate** — BTC crashing should DISABLE grid buying, not just reduce signal weight.

---

## v5.0 Improvements

### 1. Emergency Daily Drawdown Circuit Breaker

```python
# NEW: If portfolio drops >5% in one day, halt ALL trading for 24 hours
DAILY_DRAWDOWN_HALT_PCT = 5.0
HALT_DURATION_HOURS = 24

def _check_daily_drawdown_halt(self, s: MarketState) -> Optional[str]:
    today_pnl_pct = self._get_today_pnl_pct()
    if today_pnl_pct < -self.DAILY_DRAWDOWN_HALT_PCT:
        return f"EMERGENCY HALT: Portfolio down {abs(today_pnl_pct):.1f}% today. Halting for 24h."
    return None
```

### 2. Faster Regime Switching

```python
# CHANGE: Lower confidence threshold for immediate switching
# OLD: threshold = 1 if new_confidence > 0.8 else 2
# NEW: threshold = 1 if new_confidence > 0.6 else 2
def _apply_regime_hysteresis(self, ...):
    threshold = 1 if new_confidence > 0.6 else 2  # Changed from 0.8
```

### 3. Tighter Stop-Loss in Bear Market

```python
# CHANGE: Dynamic stop-loss based on regime
# OLD: stop_loss_pct = 2.5 (fixed)
# NEW: BEAR=1.5%, SIDEWAYS=2.0%, BULL=2.5%
def _get_regime_params(self, regime: str, confidence: float):
    if regime == REGIME_BEAR:
        base["stop_loss_pct_raw"] = 1.5  # Tighter in bear market
    elif regime == REGIME_SIDEWAYS:
        base["stop_loss_pct_raw"] = 2.0
    else:
        base["stop_loss_pct_raw"] = 2.5
```

### 4. Reduced Max Position in Bear Market

```python
# CHANGE: Much lower max position in bear market
# OLD: max_position_pct = 45% in BEAR
# NEW: max_position_pct = 25% in BEAR
if regime == REGIME_BEAR:
    base["max_position_pct"] = min(25, self._base_max_position_pct)  # Was 45
```

### 5. BTC Correlation as Primary Gate

```python
# NEW: BTC crash disables ALL grid buying
def _check_btc_crash_gate(self, s: MarketState) -> Optional[str]:
    if s.btc_long_term_trend_pct < -3.0:
        return f"BTC CRASH GATE: BTC down {abs(s.btc_long_term_trend_pct):.1f}% on day. Grid buying disabled."
    return None
```

### 6. Consecutive Buy Limit Reduction

```python
# CHANGE: Reduce consecutive buys from 3 to 2
# OLD: MAX_CONSECUTIVE_BUYS = 3
# NEW: MAX_CONSECUTIVE_BUYS = 2
MAX_CONSECUTIVE_BUYS = 2
```

### 7. Enhanced Death Spiral Protection

```python
# CHANGE: After stop-loss, require larger price drop before re-entry
# OLD: STOP_LOSS_PRICE_DROP_PCT = 1.5%
# NEW: STOP_LOSS_PRICE_DROP_PCT = 3.0% (doubled)
STOP_LOSS_PRICE_DROP_PCT = 3.0

# CHANGE: Shorter memory expiry
# OLD: STOP_LOSS_MEMORY_HOURS = 4
# NEW: STOP_LOSS_MEMORY_HOURS = 2 (faster expiry, more agile)
STOP_LOSS_MEMORY_HOURS = 2
```

---

## Summary of Changes

| Parameter | v4.3 (Original) | v5.0 (Improved) | Rationale |
|-----------|-----------------|-----------------|-----------|
| Regime switch threshold | 0.8 | 0.6 | Faster reaction to market change |
| BEAR max position | 45% | 25% | Less exposure in downtrends |
| BEAR stop-loss | 2.5% | 1.5% | Cut losses faster |
| MAX_CONSECUTIVE_BUYS | 3 | 2 | Less averaging down |
| STOP_LOSS_PRICE_DROP_PCT | 1.5% | 3.0% | Avoid re-entry too soon |
| Daily drawdown halt | None | -5% | Emergency circuit breaker |
| BTC crash gate | Signal only | Primary gate | Disable buying when BTC crashes |

---

## Implementation Priority

1. **CRITICAL** — Daily drawdown circuit breaker
2. **CRITICAL** — BTC crash gate
3. **HIGH** — Faster regime switching
4. **HIGH** — Reduced BEAR max position
5. **MEDIUM** — Tighter BEAR stop-loss
6. **MEDIUM** — Reduced consecutive buys

---

## Expected Results

- Fewer losing trades in bear markets
- Faster exit when market turns down
- Smaller maximum drawdown
- May reduce profits in sideways/bull markets slightly (acceptable tradeoff)

---

## Next Steps

1. Implement v5.0 changes to strategy.py
2. Run backtests on 2025-2026 historical data
3. Paper trade for 2-4 weeks
4. Deploy to live with reduced capital first
