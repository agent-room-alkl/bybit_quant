# -*- coding: utf-8 -*-
"""
智能量化交易策略模块 v5.0
核心策略：网格交易 + 趋势跟踪 + 多指标确认

v5.0 改进 (Atlas, 2026-03-22)：
- 新增每日回撤熔断器：当日亏损>5%暂停交易24小时
- 新增BTC崩盘门控：BTC日跌>3%禁止网格买入
- 加速市场状态切换：置信度>0.6即可立即切换(原0.8)
- 熊市止损收紧：2.5%→1.5%
- 熊市最大仓位收紧：45%→25%
- 连续买入上限减少：3→2
- 止损后再入场价格门槛提高：1.5%→3.0%
"""
from __future__ import annotations
from typing import Dict, Any, Tuple, Optional, List
from dataclasses import dataclass
import time, math, logging

log = logging.getLogger("strategy")


def bps(x: float) -> float:
    return x * 10000.0

def pct(x: float) -> float:
    return x * 100.0

# v4.3: 支持回测模式的时间函数
_simulated_time_ms: int = 0  # 0=实盘模式, >0=回测模式

def set_simulated_time(ts_ms: int):
    """回测引擎调用此函数设置当前模拟时间"""
    global _simulated_time_ms
    _simulated_time_ms = ts_ms

def now_ms() -> int:
    if _simulated_time_ms > 0:
        return _simulated_time_ms
    return int(time.time() * 1000)

def safe_float(x, default=float("nan")) -> float:
    try:
        return float(x)
    except Exception:
        return default


# ── 数据类 ──────────────────────────────────────────────────────

# ── 市场状态常量 ──
REGIME_BULL = "BULL"
REGIME_BEAR = "BEAR"
REGIME_SIDEWAYS = "SIDEWAYS"


@dataclass
class MarketState:
    """市场状态数据"""
    last_price: float
    cost_price: float
    rsi14: float
    rsi7: float
    macd_hist: float
    bb_position: float  # 0-1, 价格在布林带中的位置
    trend_score: float  # -100 to +100
    atr_pct: float
    volume_ratio: float
    support: float
    resistance: float
    sma7: float
    sma24: float
    sma72: float
    recent_high: float = 0.0
    recent_low: float = 0.0
    last_sell_price: float = 0.0
    last_sell_qty: float = 0.0
    last_sell_time: int = 0
    avg_sell_price: float = 0.0
    original_cost_price: float = 0.0
    usdt_balance: float = 0.0
    base_balance: float = 0.0
    usdt_pct: float = 0.0
    base_pct: float = 0.0
    long_term_trend_pct: float = 0.0
    btc_trend_score: float = 0.0
    btc_long_term_trend_pct: float = 0.0
    last_stop_loss_time: int = 0
    last_buy_time: int = 0
    regime: str = "SIDEWAYS"
    regime_confidence: float = 0.5
    # ── v3.1: 多时间框架趋势 ──
    h1_trend_score: float = 0.0       # 1H级别趋势分 (-100 ~ +100)
    h1_sma7: float = 0.0              # 1H SMA7
    h1_sma24: float = 0.0             # 1H SMA24
    h1_sma72: float = 0.0             # 1H SMA72
    h1_rsi14: float = 50.0            # 1H RSI14
    consecutive_buys: int = 0         # 连续买入次数（无卖出间隔）
    # ── v5.2: 新增指标 ──
    h1_ema8: float = 0.0             # 1H EMA(8)，趋势金叉/死叉
    adx: float = 25.0                # ADX 趋势强度 (0-100, >25=趋势中)
    # ── v5.3: 卖出记录 ──
    sell_execs_raw: list = None       # 原始卖出执行记录
    # ── v6.0: 新闻情绪 ──
    news_sentiment: int = 0           # Claude分析的新闻情绪 (-100~+100)
    news_confidence: float = 0.0      # 情绪判断的信心 (0~1)
    news_risk_level: str = "medium"   # 风险级别 (low/medium/high)
    news_action: str = "hold"         # 建议动作


@dataclass
class TradeSignal:
    """交易信号"""
    action: str  # BUY, SELL, HOLD
    confidence: float  # 0-100
    reason: str
    position_pct: float
    price_target: Optional[float] = None
    stop_loss: Optional[float] = None


@dataclass
class ShadowDecision:
    """Shadow-only diagnostics; never changes live trade behavior."""
    risk_score: float
    risk_level: str
    shadow_action: str
    sell_rebuy_block: bool
    bull_exit_mode: str
    daily_drawdown_shadow: str
    accumulation_signal: bool
    adaptive_exit: Dict[str, Any] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "risk_score": round(self.risk_score, 3),
            "risk_level": self.risk_level,
            "shadow_action": self.shadow_action,
            "sell_rebuy_block": self.sell_rebuy_block,
            "bull_exit_mode": self.bull_exit_mode,
            "daily_drawdown_shadow": self.daily_drawdown_shadow,
            "accumulation_signal": self.accumulation_signal,
            "adaptive_exit": self.adaptive_exit or {},
        }


# ── 主策略类 ──────────────────────────────────────────────────────

class SmartStrategy:
    SELL_COOLDOWN_MIN = 8.0
    BATCH_INTERVAL_MIN = 12.0
    BATCH_RATIOS = [0.4, 0.3, 0.3]
    MIN_PROFIT_PCT = 0.8
    MIN_PROFIT_FIRST_PCT = 0.5
    MAX_BATCH_HOURS = 12
    STOP_LOSS_COOLDOWN_MIN = 60.0   # v5.0: 45→60分钟，防止止损后立即回补
    # v5.0: 增强死亡螺旋保护
    MAX_DAILY_STOP_LOSSES = 4       # v5.1: 3→4次，配合更宽止损
    STOP_LOSS_PRICE_DROP_PCT = 2.0  # v5.1: 3.0%→2.0%，允许更快再入场
    STOP_LOSS_MEMORY_HOURS = 2      # v5.0: 4→2小时，更快过期
    MIN_SAME_SIDE_INTERVAL_MIN = 15.0  # v5.0: 12→15分钟
    BUY_SELL_COOLDOWN_MIN = 15.0       # v5.4: 买入后15分钟内不触发常规卖出
    SELL_BUY_COOLDOWN_MIN = 10.0       # v5.5 P1: 卖出后10分钟内不触发趋势买入(避免反复高频)
    RECENT_BUY_EXPIRE_MIN = 120        # v5.5 P2: 近期最高买入价追踪窗口(120min过期)
    RECENT_BUY_PROFIT_BUFFER_PCT = 0.3 # v5.5 P2: 卖价需高于近期最高买入价*(1+手续费+此缓冲%)
    SELL_SELL_COOLDOWN_MIN = 15.0      # v5.5.4 Fix2: 常规止盈后15min冷却,避免BULL期频繁小额止盈(止损除外)
    MAX_CONSECUTIVE_BUYS = 2        # v5.0: 3→2，减少连续买入
    # v5.0 新增：每日回撤熔断
    DAILY_DRAWDOWN_HALT_PCT = 5.0   # 当日亏损超过5%暂停交易
    DAILY_DRAWDOWN_HALT_HOURS = 24  # 暂停24小时
    # v5.0 新增：BTC崩盘门控
    BTC_CRASH_GATE_PCT = 3.0        # BTC日跌>3%禁止买入

    def __init__(self, config: Dict[str, Any]):
        self.rsi_oversold = float(config.get("rsi_oversold", 25))
        self.rsi_overbought = float(config.get("rsi_overbought", 75))
        self.min_edge_bps = float(config.get("min_edge_bps", 40))
        self.fee_bps = float(config.get("fee_bps", 10))
        self.grid_enabled = bool(config.get("grid_enabled", True))
        self.grid_spacing_pct = float(config.get("grid_spacing_pct", 0.6))
        self.trend_threshold = float(config.get("trend_threshold", 30))
        self.trailing_stop_pct = float(config.get("trailing_stop_pct", 2.0))
        self.max_position_pct = float(config.get("max_position_pct", 90))
        self.min_position_pct = float(config.get("min_position_pct", 10))
        self.base_trade_pct = float(config.get("base_trade_pct", 8))
        self.max_atr_pct = float(config.get("max_atr_pct", 5.0))
        self.min_confirmation = int(config.get("min_confirmation", 2))
        self.scalp_mode = bool(config.get("scalp_mode", False))
        self.downtrend_breaker_enabled = bool(config.get("downtrend_breaker_enabled", True))
        # 杠杆参数
        self.leverage = float(config.get("leverage", 1.0))
        # 止损阈值：config中的stop_loss_pct代表本金最大亏损%
        # 杠杆下价格只需变动 stop_loss/leverage 即可达到等量本金亏损
        _raw_stop = float(config.get("stop_loss_pct", 2.5))
        self.stop_loss_pct = _raw_stop / self.leverage
        # 杠杆利息成本（年化约10%即每日~2.7bps，用于边际检查）
        self.interest_bps_daily = 3.0 if self.leverage > 1 else 0.0
        # BTC趋势参数
        self.btc_trend_enabled = bool(config.get("btc_trend_enabled", False))
        self.btc_trend_weight = float(config.get("btc_trend_weight", 0.25))
        self.btc_trend_threshold = float(config.get("btc_trend_threshold", 20))
        # 自适应市场状态
        self.regime_detection_enabled = bool(config.get("regime_detection_enabled", True))
        self._last_regime = REGIME_SIDEWAYS
        self._pending_regime: Optional[str] = None
        self._pending_count = 0
        self._regime_params: Dict[str, Any] = {}
        # 保存原始配置值用于恢复
        self._base_rsi_oversold = self.rsi_oversold
        self._base_rsi_overbought = self.rsi_overbought
        self._base_min_confirmation = self.min_confirmation
        self._base_stop_loss_pct = self.stop_loss_pct
        self._base_max_position_pct = self.max_position_pct
        self._base_base_trade_pct = self.base_trade_pct
        self._base_grid_spacing_pct = self.grid_spacing_pct
        self._base_trend_threshold = self.trend_threshold
        self._raw_stop_loss_pct = float(config.get("stop_loss_pct", 2.5))
        # v4.3: 死亡螺旋追踪器
        self._daily_stop_loss_count = 0        # 当天止损次数
        self._last_stop_loss_date = ""         # 上次止损日期（用于重置计数）
        self._last_stop_loss_price = 0.0       # 上次止损时的价格（价格记忆）
        self._last_stop_loss_ts_ms = 0         # 上次止损时间戳（价格记忆过期用）
        # v5.5 P2: 最近买入最高价追踪 (避免"低位盈利止盈"卖掉高位刚买入的单子)
        self._recent_max_buy_price = 0.0       # 最近窗口内的最高买入价
        self._recent_max_buy_ts_ms = 0         # 最近买入时间戳
        self._last_seen_buy_ts_ms = 0          # 上次看到的 s.last_buy_time,用于检测新买入

    # ── 辅助方法 ──────────────────────────────────────────────

    def _ref_cost(self, s: MarketState) -> float:
        # v3.5: 阈值与_try_special_buy对齐（scalp=50%,非scalp=60%）
        # 避免特殊买入路径进入但edge check用错误的参考价
        threshold = 50 if self.scalp_mode else 60
        if s.usdt_pct > threshold and s.avg_sell_price > 0:
            return s.avg_sell_price
        return s.cost_price

    def _actual_cost(self, s: MarketState) -> float:
        return s.cost_price

    def _pct_diff(self, price: float, base: float) -> float:
        if base <= 0 or price <= 0:
            return 0.0
        return (price - base) / base * 100

    # ── 市场状态识别 ──────────────────────────────────────────

    def _detect_regime(self, s: MarketState) -> Tuple[str, float]:
        """识别当前市场状态：BULL/BEAR/SIDEWAYS + 置信度(0~1)"""
        bull_score = 0.0
        bear_score = 0.0

        # 信号1: SMA排列 (权重30%)
        alignment = 0
        if s.sma7 > s.sma24: alignment += 1
        else: alignment -= 1
        if s.sma24 > s.sma72: alignment += 1
        else: alignment -= 1
        if s.last_price > s.sma7: alignment += 1
        else: alignment -= 1
        if alignment >= 2:
            bull_score += 30 * (alignment / 3.0)
        elif alignment <= -2:
            bear_score += 30 * (abs(alignment) / 3.0)

        # 信号2: trend_score (权重25%)
        if s.trend_score > 20:
            bull_score += min(25, s.trend_score * 0.25)
        elif s.trend_score < -20:
            bear_score += min(25, abs(s.trend_score) * 0.25)

        # 信号3: 15天长期趋势 (权重15% - 降低权重因为滞后太大)
        if s.long_term_trend_pct > 5:
            bull_score += min(15, s.long_term_trend_pct * 1.0)
        elif s.long_term_trend_pct < -5:
            bear_score += min(15, abs(s.long_term_trend_pct) * 1.0)
        else:
            if s.long_term_trend_pct > 0:
                bull_score += s.long_term_trend_pct * 0.5
            else:
                bear_score += abs(s.long_term_trend_pct) * 0.5

        # 信号4: 价格相对SMA72位置 (权重10% - 新增，更直接反映趋势)
        if s.sma72 > 0:
            above_sma72 = (s.last_price - s.sma72) / s.sma72 * 100
            if above_sma72 > 2:
                bull_score += min(10, above_sma72 * 1.5)
            elif above_sma72 < -2:
                bear_score += min(10, abs(above_sma72) * 1.5)

        # 信号5: BTC趋势 (权重10%)
        if s.btc_trend_score > 20:
            bull_score += min(10, s.btc_trend_score * 0.1)
        elif s.btc_trend_score < -20:
            bear_score += min(10, abs(s.btc_trend_score) * 0.1)

        # 信号6: RSI位置 (权重10%)
        if s.rsi14 > 55:
            bull_score += min(10, (s.rsi14 - 50) * 0.3)
        elif s.rsi14 < 45:
            bear_score += min(10, (50 - s.rsi14) * 0.3)

        # 分类
        total = bull_score + bear_score
        if total < 15:
            return (REGIME_SIDEWAYS, 0.5)

        net = bull_score - bear_score
        if net > 20:
            return (REGIME_BULL, min(1.0, net / 60.0))
        elif net < -20:
            return (REGIME_BEAR, min(1.0, abs(net) / 60.0))
        else:
            return (REGIME_SIDEWAYS, max(0.3, 1.0 - abs(net) / 20.0))

    def _apply_regime_hysteresis(self, new_regime: str, new_confidence: float) -> Tuple[str, float]:
        """防抖：连续2次确认才切换状态，高置信度可立即切换"""
        if new_regime == self._last_regime:
            self._pending_regime = None
            self._pending_count = 0
            return (new_regime, new_confidence)

        if new_regime == self._pending_regime:
            self._pending_count += 1
        else:
            self._pending_regime = new_regime
            self._pending_count = 1

        threshold = 1 if new_confidence > 0.6 else 2  # v5.0: 0.8→0.6，更快切换
        if self._pending_count >= threshold:
            self._last_regime = new_regime
            self._pending_regime = None
            self._pending_count = 0
            return (new_regime, new_confidence)

        return (self._last_regime, new_confidence * 0.7)

    def _get_regime_params(self, regime: str, confidence: float,
                            s: Optional[MarketState] = None) -> Dict[str, Any]:
        """根据市场状态和置信度返回自适应参数 — v3.1: 熊市大幅收紧"""
        base = {
            "rsi_buy": self._base_rsi_oversold,
            "rsi_sell": self._base_rsi_overbought,
            "min_confirmation": self._base_min_confirmation,
            "grid_spacing_pct": self._base_grid_spacing_pct,
            "stop_loss_pct_raw": self._raw_stop_loss_pct,
            "max_position_pct": self._base_max_position_pct,
            "base_trade_pct": self._base_base_trade_pct,
            "max_above_cost_buy_pct": 0,
            "trend_threshold": self._base_trend_threshold,
            "downtrend_breaker_pct": 5.0,
            "sma_filter_tol": 1.02,
            "sell_dampen": 1.0,
            "buy_above_cost_allowed": False,
        }
        if regime == REGIME_BEAR:
            # v5.0 熊市：大幅收紧，资本保护优先
            base["rsi_buy"] = max(20, self._base_rsi_oversold - 8)     # v5.0: RSI更低才买
            base["min_confirmation"] = max(3, self._base_min_confirmation + 1)  # v5.1: 多1个确认（was +2）
            base["grid_spacing_pct"] = self._base_grid_spacing_pct * 2.0  # v5.0: 网格间距2倍
            base["base_trade_pct"] = self._base_base_trade_pct * 0.5    # v5.1: 交易量减至50%（was 40%）
            base["max_position_pct"] = min(40, self._base_max_position_pct)  # v5.4: 仓位上限40% (was 25%，减少无谓再平衡)
            base["stop_loss_pct_raw"] = 4.0  # v5.2: 熊市止损放宽到4%（was 2.5%，SOL常见波动3-5%）
            base["downtrend_breaker_pct"] = 2.5  # v5.0: 下跌2.5%熔断 (was 3.5%)
            base["trend_threshold"] = max(20, self._base_trend_threshold)  # v5.0: 保守趋势判断
            base["sell_dampen"] = 0.7  # v5.0: 熊市更快止盈
            log.info(f"[REGIME] BEAR detected (conf={confidence:.0%}), v5.0 DEFENSIVE MODE")
        elif regime == REGIME_BULL:
            # 牛市：适度放宽
            base["rsi_sell"] = min(80, self._base_rsi_overbought + 5)  # RSI更高才卖
            base["max_above_cost_buy_pct"] = 2.0  # 允许高于成本2%买入
            base["buy_above_cost_allowed"] = True
            base["downtrend_breaker_pct"] = 8.0  # 放宽熔断
            base["sell_dampen"] = 0.5  # 压制卖出
            # v5.5.9 优化6: BULL 体制加大交易量 (7天复盘 100% 胜率 +1.21% 均盈)
            # v5.5.10 修正: 高位过滤 — 15天累计涨幅 ≥ 3% 时不放大,避免追涨杀跌
            # 实盘证据: 04-23 02:45-05:21 SOL 已涨3.5%还追3单,被套-1.5% 合计-$3.8
            if s is None or s.long_term_trend_pct < 3.0:
                base["base_trade_pct"] = self._base_base_trade_pct * 2.0
            # else: 过热期(15d涨>=3%)保持基础交易量, 不追涨
        return base

    def _apply_adaptive_params(self, rp: Dict[str, Any]):
        """将自适应参数临时覆写到实例属性"""
        self.rsi_oversold = rp["rsi_buy"]
        self.rsi_overbought = rp["rsi_sell"]
        self.min_confirmation = int(rp["min_confirmation"])
        self.grid_spacing_pct = rp["grid_spacing_pct"]
        self.stop_loss_pct = rp["stop_loss_pct_raw"] / self.leverage
        self.max_position_pct = rp["max_position_pct"]
        self.base_trade_pct = rp["base_trade_pct"]
        self.trend_threshold = rp["trend_threshold"]
        self._regime_params = rp

    # ── 熔断与保护 ──────────────────────────────────────────

    # ── v6.0 新闻情绪调整 ──────────────────────────────────────
    def _apply_news_sentiment(self, s: MarketState, buy_score: float, sell_score: float,
                               buy_sigs: list, sell_sigs: list) -> dict:
        """
        根据 Claude 分析的新闻情绪调整买卖分数。

        规则：
        - 强利好 (score >= 50, conf >= 0.7): 买入加权20%, 卖出减权10%
        - 轻度利好 (score 20~49): 买入加权10%
        - 强利空 (score <= -50, conf >= 0.7): 卖出加权20%, 买入减权15%
        - 轻度利空 (score -49~-20): 卖出加权10%
        - 高风险 (risk=high): 额外压制买入15%
        - 中性 (-20~20): 不调整
        """
        sentiment = s.news_sentiment
        conf = s.news_confidence
        risk = s.news_risk_level

        adj_buy = buy_score
        adj_sell = sell_score
        news_tag = ""

        # 利好：放大买入，压制卖出
        if sentiment >= 50 and conf >= 0.7:
            adj_buy = buy_score * 1.20 + sentiment * 0.3   # 乘法+加法，确保零分也有效
            adj_sell = sell_score * 0.90
            news_tag = f"强利好({sentiment},conf={conf:.0%})"
            buy_sigs = buy_sigs + [(f"新闻{news_tag}", sentiment * 0.3)]
        elif sentiment >= 20:
            adj_buy = buy_score * 1.10 + sentiment * 0.15
            news_tag = f"轻度利好({sentiment})"
            if conf >= 0.6:
                buy_sigs = buy_sigs + [(f"新闻{news_tag}", sentiment * 0.15)]

        # 利空：放大卖出，压制买入
        elif sentiment <= -50 and conf >= 0.7:
            adj_sell = sell_score * 1.20 + abs(sentiment) * 0.3  # 零分卖出也能被激活
            adj_buy = buy_score * 0.85
            news_tag = f"强利空({sentiment},conf={conf:.0%})"
            sell_sigs = sell_sigs + [(f"新闻{news_tag}", abs(sentiment) * 0.3)]
        elif sentiment <= -20:
            adj_sell = sell_score * 1.10 + abs(sentiment) * 0.15
            adj_buy = buy_score * 0.95
            news_tag = f"轻度利空({sentiment})"
            if conf >= 0.6:
                sell_sigs = sell_sigs + [(f"新闻{news_tag}", abs(sentiment) * 0.15)]

        # 高风险附加：压制买入（不管买卖方向）
        if risk == "high":
            adj_buy = adj_buy * 0.85
            news_tag += "+高风险"

        if news_tag:
            log.info(f"[NEWS] 情绪调整: {news_tag} | "
                     f"buy {buy_score:.0f}→{adj_buy:.0f}, sell {sell_score:.0f}→{adj_sell:.0f}")

        return {
            "buy_score": adj_buy,
            "sell_score": adj_sell,
            "buy_sigs": buy_sigs,
            "sell_sigs": sell_sigs,
        }

    # v5.0 新增：BTC崩盘门控
    def _check_btc_crash_gate(self, s: MarketState) -> Optional[str]:
        """v5.0: BTC日跌超过阈值时禁止网格买入"""
        if s.btc_long_term_trend_pct < -self.BTC_CRASH_GATE_PCT:
            return (f"v5.0 BTC崩盘门控：BTC日跌{abs(s.btc_long_term_trend_pct):.1f}%"
                    f"(>{self.BTC_CRASH_GATE_PCT}%)，禁止网格买入，等待企稳")
        return None

    def _check_downtrend_breaker(self, s: MarketState) -> Optional[str]:
        """v4.2下跌熔断器：SMA死叉+长期趋势双重保护，防止在下跌中持续买入"""
        if s.last_price <= 0:
            return None

        # ── 核心保护1：15min SMA死叉 + 趋势下行 → 停买 ──
        if s.sma7 > 0 and s.sma24 > 0:
            if s.sma7 < s.sma24 and s.trend_score < -15:
                return (f"v4.2熔断：SMA7({s.sma7:.1f})<SMA24({s.sma24:.1f})"
                        f"且趋势{s.trend_score:.0f}<-15，等待反转确认后再买")
            # 完全死叉 = 强下跌趋势，无条件停买
            if s.sma72 > 0 and s.sma7 < s.sma24 < s.sma72:
                return (f"v4.2熔断：完全死叉SMA7<SMA24<SMA72，禁止买入")

        # ── 核心保护2：SMA72偏离检查 ──
        if s.sma72 > 0:
            below_sma72 = (s.sma72 - s.last_price) / s.sma72 * 100
            breaker_pct = self._regime_params.get("downtrend_breaker_pct", 5.0) if self._regime_params else 5.0
            secondary_pct = breaker_pct * 0.6
            if below_sma72 >= breaker_pct:
                return (f"v4.2熔断：价格低于72均线{below_sma72:.1f}%（>{breaker_pct:.0f}%），"
                        f"禁止买入，等待企稳")
            if below_sma72 >= secondary_pct and s.trend_score < 0:
                return (f"v4.2熔断：价格低于72均线{below_sma72:.1f}%（>{secondary_pct:.0f}%）"
                        f"且趋势{s.trend_score:.0f}<0，禁止买入")

        # ── 核心保护3：15天长期趋势熔断 ──
        if s.long_term_trend_pct < -5.0 and s.trend_score < 10:
            return (f"v4.2熔断：15天趋势{s.long_term_trend_pct:+.1f}%（<-5%），"
                    f"市场处于下跌周期，禁止买入")

        # ── 1H级别死亡交叉检测（仅实盘有数据） ──
        if s.h1_sma7 > 0 and s.h1_sma24 > 0 and s.h1_sma72 > 0:
            if s.h1_sma7 < s.h1_sma24 < s.h1_sma72 and s.last_price < s.h1_sma7:
                return (f"1H死亡交叉熔断：价格在所有1H均线下方，禁止买入")

        # ── 连续买入保护 ──
        if s.consecutive_buys >= self.MAX_CONSECUTIVE_BUYS:
            return (f"连续买入{s.consecutive_buys}笔无卖出(上限{self.MAX_CONSECUTIVE_BUYS})，暂停买入等待方向确认")

        return None

    def _pullback(self, s: MarketState) -> float:
        if s.recent_high > 0 and s.last_price > 0:
            return (s.recent_high - s.last_price) / s.recent_high * 100
        return 0.0

    def _recovery(self, s: MarketState) -> float:
        if s.recent_low > 0 and s.last_price > s.recent_low:
            return (s.last_price - s.recent_low) / s.recent_low * 100
        return 0.0

    # ── v4.0 DGT反弹确认 ──────────────────────────────────────
    def _dgt_buy_confirmed(self, s: MarketState) -> Tuple[bool, str]:
        """DGT反弹确认：不在信号触发时立刻买入，需要价格反转确认
        确认条件（满足任意2个）：
        1. RSI7 > RSI14（短期动能回升）
        2. 价格从近期低点反弹 >= 0.5%
        3. MACD柱状图转正或收窄
        4. 价格站上SMA7
        """
        confirms = 0
        reasons = []

        # 1. RSI拐头
        if s.rsi7 > s.rsi14:
            confirms += 1
            reasons.append("RSI拐头向上")

        # 2. 从低点反弹
        recovery = self._recovery(s)
        if recovery >= 0.5:
            confirms += 1
            reasons.append(f"从低点反弹{recovery:.1f}%")

        # 3. MACD转强
        if s.macd_hist > -0.001:
            confirms += 1
            reasons.append("MACD转强")

        # 4. 价格站上SMA7
        if s.sma7 > 0 and s.last_price > s.sma7:
            confirms += 1
            reasons.append("价格>SMA7")

        if confirms >= 1:
            return True, f"反弹确认({', '.join(reasons[:2])})"
        return False, f"等待反弹确认(当前{confirms}/1: 无反转信号)"

    def _dgt_sell_confirmed(self, s: MarketState) -> Tuple[bool, str]:
        """DGT回调确认：卖出需要确认价格开始走弱
        确认条件（满足任意1个即可）：
        1. RSI7 < RSI14（短期动能减弱）
        2. 价格从近期高点回调 >= 0.5%
        3. MACD柱状图转负或收窄
        4. 价格跌破SMA7
        """
        confirms = 0
        reasons = []

        if s.rsi7 < s.rsi14:
            confirms += 1
            reasons.append("RSI拐头向下")

        pullback = self._pullback(s)
        if pullback >= 0.5:
            confirms += 1
            reasons.append(f"从高点回调{pullback:.1f}%")

        if s.macd_hist < 0.001:
            confirms += 1
            reasons.append("MACD转弱")

        if s.sma7 > 0 and s.last_price < s.sma7:
            confirms += 1
            reasons.append("价格<SMA7")

        if confirms >= 1:
            return True, f"回调确认({', '.join(reasons[:2])})"
        return False, f"等待回调确认(当前{confirms}/1: 无走弱信号)"

    def _time_since_min(self, ts_ms: int) -> float:
        if ts_ms <= 0:
            return float('inf')
        return (now_ms() - ts_ms) / 60000.0

    def _profit_margin(self, sell_price: float, buy_price: float) -> float:
        if sell_price <= 0 or buy_price <= 0:
            return 0.0
        return (sell_price - buy_price) / sell_price * 100

    def _using_sell_avg(self, s: MarketState) -> bool:
        threshold = 50 if self.scalp_mode else 60
        return s.usdt_pct > threshold and s.avg_sell_price > 0

    def _actual_profit_pct(self, s: MarketState) -> float:
        ac = self._actual_cost(s)
        return self._pct_diff(s.last_price, ac) if ac > 0 else 0

    def _usdt_usage_pct(self, profit_margin: float, usdt_pct: float) -> float:
        if profit_margin >= 5.0:
            base = 75
        elif profit_margin >= 3.0:
            base = 60
        elif profit_margin >= 2.0:
            base = 40
        else:
            base = 25
        mult = 1.0 if usdt_pct >= 80 else (0.9 if usdt_pct >= 70 else 0.8)
        return base * mult

    def _bottom_signals(self, s: MarketState) -> Tuple[int, List[str]]:
        recovery = self._recovery(s)
        count, reasons = 0, []
        if recovery > 0.5:
            count += 1; reasons.append(f"从低点回升{recovery:.1f}%")
        if s.rsi14 < 35 and s.rsi7 > s.rsi14:
            count += 1; reasons.append("RSI超卖后回升")
        if s.macd_hist > -0.001:
            count += 1; reasons.append("MACD转强")
        if s.support > 0 and s.last_price > 0:
            d = (s.last_price - s.support) / s.last_price * 100
            if d < 1.0:
                count += 1; reasons.append("接近支撑位")
        return count, reasons

    def _calc_position(self, confidence: float, trend_m: float, depth_m: float,
                       bb: float, pullback: float, recovery: float,
                       is_buy: bool) -> float:
        base_m = 1 + (confidence - 50) / 100 if confidence > 50 else 0.7
        final = base_m * trend_m * depth_m

        # 价格优势调整 ±30%
        if is_buy:
            if bb < 0.2: pa = 1.3
            elif bb < 0.3: pa = 1.15
            elif bb > 0.5: pa = 0.9
            else: pa = 1.0
            if pullback >= 1.0: pa = max(pa, 1.2)
        else:
            if bb > 0.8: pa = 1.3
            elif bb > 0.7: pa = 1.15
            elif bb < 0.5: pa = 0.9
            else: pa = 1.0
            if recovery >= 1.0: pa = max(pa, 1.2)
        final *= pa

        max_single = min(12, self.base_trade_pct * 1.5)
        return max(self.base_trade_pct * 0.3, min(self.base_trade_pct * final, max_single))

    # ── Shadow 诊断：只记录，不影响交易 ─────────────────────────

    def classify_shadow(
        self,
        s: MarketState,
        pos: float,
        daily_pnl_pct: float = 0.0,
        early_warning_count_24h: int = 0,
    ) -> ShadowDecision:
        """
        Calculate risk/action diagnostics for shadow mode.
        This method is intentionally side-effect free: it must never change
        live BUY/SELL/HOLD decisions.
        """
        reasons = []
        score = 0.0
        atr = max(s.atr_pct, 0.01)

        if s.recent_high > 0 and s.last_price > 0:
            pullback_atr = ((s.recent_high - s.last_price) / s.last_price * 100) / atr
            if pullback_atr > 1.5:
                score += 0.30
                reasons.append("pullback_gt_1_5_atr")
            elif pullback_atr > 1.0:
                score += 0.15
                reasons.append("pullback_gt_1_atr")

        if s.volume_ratio > 2.0:
            score += 0.20
            reasons.append("volume_spike")
        elif s.volume_ratio > 1.5:
            score += 0.10
            reasons.append("volume_elevated")

        support_break_pct = 0.0
        if s.support > 0 and s.last_price > 0 and s.last_price < s.support:
            support_break_pct = (s.support - s.last_price) / s.support * 100
        if support_break_pct > 0.3 or (s.sma72 > 0 and s.last_price < s.sma72 and s.trend_score < -15):
            score += 0.20
            reasons.append("structure_break")

        if s.btc_long_term_trend_pct < -2.0 or s.btc_trend_score < -40:
            score += 0.15
            reasons.append("btc_pressure")

        if s.news_sentiment <= -50 and s.news_confidence >= 0.7:
            score += 0.15
            reasons.append("news_strong_bearish")
        elif s.news_risk_level == "high":
            score += 0.08
            reasons.append("news_high_risk")

        if early_warning_count_24h >= 5:
            score += 0.10
            reasons.append("warning_cluster")

        score = min(1.0, score)
        if score < 0.30:
            risk_level = "L1_NOISE"
            shadow_action = "hold"
        elif score < 0.60:
            risk_level = "L2_WEAKENING"
            shadow_action = "reduce_30" if pos > 40 else "hold"
        elif score < 0.85:
            risk_level = "L3_TRUE_RISK"
            shadow_action = "reduce_60" if pos > 25 else "hold"
        else:
            risk_level = "L4_BLACK_SWAN"
            shadow_action = "exit" if pos > 10 else "halt"

        sell_rebuy_block = False
        if s.last_sell_time > 0 and s.last_sell_price > 0 and s.last_price > 0:
            since_sell = self._time_since_min(s.last_sell_time)
            band_pct = max(0.6, atr)
            price_band_pct = abs(s.last_price - s.last_sell_price) / s.last_sell_price * 100
            trend_confirmed = s.trend_score > 30 and s.h1_trend_score > 20 and s.adx > 20
            sell_rebuy_block = since_sell < 180 and price_band_pct <= band_pct and not trend_confirmed

        bull_exit_mode = "normal"
        if s.regime == REGIME_BULL:
            true_risk = risk_level in ("L3_TRUE_RISK", "L4_BLACK_SWAN")
            bull_exit_mode = "atr_trailing_only" if not true_risk else "risk_override"

        daily_drawdown_shadow = "ok"
        if daily_pnl_pct <= -self.DAILY_DRAWDOWN_HALT_PCT:
            daily_drawdown_shadow = "halt_24h"
        elif daily_pnl_pct <= -self.DAILY_DRAWDOWN_HALT_PCT * 0.5:
            daily_drawdown_shadow = "warning"

        # Event study showed the first accumulation heuristic had too many
        # false positives. Keep the field but disable it until v2 has SMA200
        # and rolling-volume history available in MarketState.
        accumulation_signal = False

        adaptive_exit = {}
        try:
            from risk_modules import AdaptiveExitManager
            entry_price = s.cost_price if s.cost_price > 0 else s.last_price
            high_since_entry = max(s.recent_high or 0.0, s.last_price)
            initial_stop = entry_price * (1 - self.stop_loss_pct / 100.0) if entry_price > 0 else None
            adaptive_exit = AdaptiveExitManager().evaluate(
                last_price=s.last_price,
                entry_price=entry_price,
                high_since_entry=high_since_entry,
                atr_pct=s.atr_pct,
                initial_stop_price=initial_stop,
                is_bull_regime=s.regime == REGIME_BULL,
                risk_level=risk_level,
            ).as_dict()
        except Exception as e:
            adaptive_exit = {"error": str(e)}

        decision = ShadowDecision(
            risk_score=score,
            risk_level=risk_level,
            shadow_action=shadow_action,
            sell_rebuy_block=sell_rebuy_block,
            bull_exit_mode=bull_exit_mode,
            daily_drawdown_shadow=daily_drawdown_shadow,
            accumulation_signal=accumulation_signal,
            adaptive_exit=adaptive_exit,
        )
        log.debug("[SHADOW] %s | reasons=%s", decision.as_dict(), ",".join(reasons))
        return decision

    # ── analyze 主调度 ──────────────────────────────────────────

    def analyze(self, state: MarketState, current_position_pct: float,
                last_buy_price: float = 0.0, total_balance_usdt: float = 0.0) -> TradeSignal:
        s = state
        pos = current_position_pct

        if s.last_price <= 0 or math.isnan(s.last_price) or math.isnan(s.rsi14):
            return TradeSignal("HOLD", 0, "数据无效", 0)

        # v5.5 P2: 更新最近最高买入价追踪
        self._update_recent_max_buy(s, last_buy_price)

        # 0. 市场状态识别 & 自适应参数
        if self.regime_detection_enabled:
            raw_regime, raw_conf = self._detect_regime(s)
            regime, conf = self._apply_regime_hysteresis(raw_regime, raw_conf)
            s.regime = regime
            s.regime_confidence = conf
            rp = self._get_regime_params(regime, conf, s)
            self._apply_adaptive_params(rp)

        # 1. 信号生成
        buy_sigs, sell_sigs = self._generate_signals(s)

        # 2. 仓位/价差调整
        buy_sigs, sell_sigs = self._adjust_for_position(s, pos, buy_sigs, sell_sigs)

        buy_score = sum(sc for _, sc in buy_sigs)
        sell_score = sum(sc for _, sc in sell_sigs)
        buy_n = len(buy_sigs)
        sell_n = len(sell_sigs)

        # 2.5 新闻情绪调整（v6.0）
        if s.news_sentiment != 0 and s.news_confidence >= 0.5:
            news_adj = self._apply_news_sentiment(s, buy_score, sell_score, buy_sigs, sell_sigs)
            buy_score = news_adj["buy_score"]
            sell_score = news_adj["sell_score"]
            buy_sigs = news_adj["buy_sigs"]
            sell_sigs = news_adj["sell_sigs"]
            buy_n = len(buy_sigs)   # 重新计算，新闻信号计入确认数
            sell_n = len(sell_sigs)

        # v5.5 P0: 追踪止损逻辑已撤除 — 诊断显示在 SOL 下跌年里
        # regime 100% SIDEWAYS,strong BULL 条件极少满足(4.31%时间),
        # 且 83.9% 场景仓位<10%无法减仓,最大回撤仅 2.24%
        # 代码保留在 history 里,未来 BULL 行情中再考虑启用

        # 3. 止损检查
        stop = self._check_stop_loss(s, pos, last_buy_price)
        if stop:
            # v4.3: 更新死亡螺旋追踪器
            self._last_stop_loss_price = s.last_price
            self._last_stop_loss_ts_ms = now_ms()
            import datetime as _dt
            today = _dt.datetime.fromtimestamp(now_ms() / 1000).strftime("%Y-%m-%d")
            if today != self._last_stop_loss_date:
                self._daily_stop_loss_count = 0
                self._last_stop_loss_date = today
            self._daily_stop_loss_count += 1
            log.debug(f"v4.3止损追踪: 今日第{self._daily_stop_loss_count}次止损 @ ${s.last_price:.2f}")
            return stop

        # 3.5 v4.3增强：三重止损保护，防止死亡螺旋
        if buy_score > sell_score:
            # 保护A：时间冷却（20→60分钟）
            if s.last_stop_loss_time > 0:
                mins_since_stop = self._time_since_min(s.last_stop_loss_time)
                if mins_since_stop < self.STOP_LOSS_COOLDOWN_MIN:
                    return TradeSignal("HOLD", 0,
                        f"止损冷却中({mins_since_stop:.0f}min < {self.STOP_LOSS_COOLDOWN_MIN:.0f}min)，暂不买入", 0)

            # 保护B：价格记忆 — 止损后价格需再跌N%才允许重新买入
            if self._last_stop_loss_price > 0 and s.last_price > 0:
                # 过期检查：超过N小时后价格记忆失效
                hours_since = self._time_since_min(self._last_stop_loss_ts_ms) / 60 if self._last_stop_loss_ts_ms > 0 else 999
                # 回升检查：价格回升超过3%以上，清除记忆（市场已反转）
                rise_from_stop = (s.last_price - self._last_stop_loss_price) / self._last_stop_loss_price * 100
                if hours_since > self.STOP_LOSS_MEMORY_HOURS or rise_from_stop > 3.0:
                    self._last_stop_loss_price = 0.0  # 清除记忆
                else:
                    drop_from_stop = (self._last_stop_loss_price - s.last_price) / self._last_stop_loss_price * 100
                    if drop_from_stop < self.STOP_LOSS_PRICE_DROP_PCT:
                        return TradeSignal("HOLD", 0,
                            f"价格记忆保护：距上次止损价${self._last_stop_loss_price:.2f}仅跌{drop_from_stop:.1f}%"
                            f"(需>{self.STOP_LOSS_PRICE_DROP_PCT}%，{hours_since:.1f}h后过期)，等待更低价位", 0)

            # 保护C：每日止损次数限制
            import datetime as _dt
            today = _dt.datetime.fromtimestamp(now_ms() / 1000).strftime("%Y-%m-%d")
            if today != self._last_stop_loss_date:
                self._daily_stop_loss_count = 0
                self._last_stop_loss_date = today
            if self._daily_stop_loss_count >= self.MAX_DAILY_STOP_LOSSES:
                return TradeSignal("HOLD", 0,
                    f"当日已止损{self._daily_stop_loss_count}次(上限{self.MAX_DAILY_STOP_LOSSES})，今日暂停买入", 0)

        # 3.6 v5.0 BTC崩盘门控：BTC大跌时禁止买入
        if buy_score > sell_score:
            btc_gate = self._check_btc_crash_gate(s)
            if btc_gate:
                return TradeSignal("HOLD", 0, btc_gate, 0)

        # 3.7 全局下跌熔断器：下跌行情中禁止常规买入（v4.2可配置）
        # v5.2: 不直接返回HOLD，标记后让DCA/趋势跟踪有机会评估
        _breaker_active = False
        _breaker_msg = ""
        if self.downtrend_breaker_enabled and buy_score > sell_score:
            _breaker_msg = self._check_downtrend_breaker(s)
            if _breaker_msg:
                _breaker_active = True
                buy_score = 0  # 压制常规买入信号
                buy_n = 0

        # 4. 仓位再平衡（在波动率过滤之前，保护性卖出不应被阻止）
        regime_max = self.max_position_pct
        if pos > regime_max + 10:
            # v5.4: 分批买入进行中豁免再平衡 — 避免一边买一边卖
            _in_batch_seq = False
            if s.last_buy_time and s.last_buy_time > 0:
                _since_buy = self._time_since_min(s.last_buy_time)
                if _since_buy < self.BATCH_INTERVAL_MIN * 3:  # 36min内有买入，可能在分批序列中
                    _in_batch_seq = s.usdt_pct > 40  # 还有USDT待部署
            if _in_batch_seq:
                log.info(f"[REBALANCE] 仓位{pos:.0f}%超上限{regime_max:.0f}%，但分批买入进行中，豁免再平衡")
            else:
                # 成本价保护：卖价必须高于成本价+手续费，防止亏损卖出
                profit = self._actual_profit_pct(s)
                if profit <= 0:
                    log.info(f"[REBALANCE] 仓位{pos:.0f}%超上限{regime_max:.0f}%，但当前亏损{profit:.2f}%，不卖")
                else:
                    excess_pct = pos - regime_max
                    sell_pct = min(excess_pct * 0.5, 20)
                    reason = f"仓位再平衡：当前{pos:.0f}%超过体制上限{regime_max:.0f}%，减仓{sell_pct:.0f}%（盈利{profit:.2f}%）"
                    log.info(f"[REBALANCE] {reason}")
                    return TradeSignal("SELL", max(30, sell_score), reason, sell_pct)

        # 4.5 波动率过滤（只阻止买入，不阻止卖出）
        if s.atr_pct > self.max_atr_pct:
            if buy_score > sell_score:
                return TradeSignal("HOLD", 0, f"波动率过高({s.atr_pct:.1f}%>{self.max_atr_pct}%)，暂停买入", 0)

        # 5. 满仓盈利强制卖出
        if pos >= self.max_position_pct:
            profit = self._actual_profit_pct(s)
            if profit > self.MIN_PROFIT_PCT:
                if not any("满仓盈利" in sig[0] for sig in sell_sigs):
                    sell_sigs.append(("满仓盈利止盈", max(50, profit * 12)))
                sell_n = max(sell_n + 1, self.min_confirmation)
                sell_score = max(sell_score, sum(sc for _, sc in sell_sigs) + profit * 50 + 100)
                buy_score = -1.0

        # 6. 杠杆慢跌保护：长期下跌趋势中限制仓位上限，减少杠杆暴露
        #    仅在"持续阴跌"时触发，急跌后反弹（crash/V-recovery）不拦截
        if self.leverage > 1 and s.long_term_trend_pct < -3:
            recovery = self._recovery(s)
            # 反弹超过2%或短期趋势转正 → 非慢跌，跳过保护
            if recovery < 2.0 and s.trend_score < 0:
                # -3%→58%, -5%→50%, -10%→30%(下限30%）
                decline_max = max(30, 70 + s.long_term_trend_pct * 4)
                if pos >= decline_max and buy_score > sell_score:
                    return TradeSignal("HOLD", 0,
                        f"杠杆慢跌保护：15天趋势{s.long_term_trend_pct:+.1f}%，仓位{pos:.0f}%≥上限{decline_max:.0f}%", 0)

        # 6.5 v3.1: 多时间框架动态仓位上限
        # 1H级别下跌趋势中，动态降低最大仓位，防止在下跌中积累过重仓位
        self._dynamic_max_pos = self.max_position_pct
        dynamic_max_pos = self._dynamic_max_pos
        if s.h1_trend_score < -50:
            dynamic_max_pos = min(dynamic_max_pos, 30)
            self._dynamic_max_pos = dynamic_max_pos
        elif s.h1_trend_score < -30:
            dynamic_max_pos = min(dynamic_max_pos, 45)
            self._dynamic_max_pos = dynamic_max_pos
        elif s.h1_trend_score < -10:
            dynamic_max_pos = min(dynamic_max_pos, 60)
            self._dynamic_max_pos = dynamic_max_pos

        if buy_score > sell_score and pos >= dynamic_max_pos:
            return TradeSignal("HOLD", 0,
                f"1H趋势仓位限制：趋势{s.h1_trend_score:.0f}，仓位{pos:.0f}%≥动态上限{dynamic_max_pos:.0f}%", 0)

        # 7. 决策（v4.2：保持v3.4逻辑，下跌保护由熔断器负责）
        regime_tag = f"[{s.regime}/{s.regime_confidence:.0%}]" if self.regime_detection_enabled else ""

        if buy_score > sell_score and buy_n >= self.min_confirmation:
            if pos >= self.max_position_pct:
                profit = self._actual_profit_pct(s)
                if 0 < profit <= self.MIN_PROFIT_PCT:
                    return TradeSignal("HOLD", 0,
                        f"{regime_tag}满仓盈利不足{profit:.2f}%，需≥{self.MIN_PROFIT_PCT}%", 0)
                return TradeSignal("HOLD", 0,
                    f"{regime_tag}满仓亏损{abs(profit):.2f}%，等待止盈或止损", 0)
            sig = self._process_buy(s, pos, buy_score, buy_sigs, total_balance_usdt)
            if regime_tag and sig.action != "HOLD":
                sig.reason = f"{regime_tag} {sig.reason}"
            return sig

        if sell_score > buy_score and sell_n >= 1:
            if pos <= self.min_position_pct:
                # v5.2e: 极低仓位(<5%)且趋势不差时，让DCA建仓
                if pos < 5 and s.trend_score > 0 and s.long_term_trend_pct > -5:
                    pass  # 允许后面DCA/趋势跟踪执行
                else:
                    return TradeSignal("HOLD", 0, f"{regime_tag}仓位过低({pos:.1f}%)", 0)
            else:
                sig = self._process_sell(s, pos, sell_score, sell_sigs, last_buy_price)
                if regime_tag and sig.action != "HOLD":
                    sig.reason = f"{regime_tag} {sig.reason}"
                return sig

        # ── v5.2: 信号不明确时或极低仓位时，尝试趋势跟踪和Smart DCA ──

        # 趋势跟踪 = 主动交易 → 受止损冷却/BTC门控限制
        _trend_safe = True
        if s.last_stop_loss_time > 0 and self._time_since_min(s.last_stop_loss_time) < self.STOP_LOSS_COOLDOWN_MIN:
            _trend_safe = False
        if self.btc_trend_enabled and s.btc_long_term_trend_pct < -self.BTC_CRASH_GATE_PCT:
            _trend_safe = False
        if self._last_stop_loss_price > 0 and s.last_price > 0:
            drop = (self._last_stop_loss_price - s.last_price) / self._last_stop_loss_price * 100
            if drop < self.STOP_LOSS_PRICE_DROP_PCT:
                _trend_safe = False

        if _trend_safe:
            trend_sig = self._check_trend_follow(s, pos, last_buy_price)
            if trend_sig:
                if regime_tag and trend_sig.action != "HOLD":
                    trend_sig.reason = f"{regime_tag} {trend_sig.reason}"
                return trend_sig

        # DCA = 长期定投 → 不受止损冷却/BTC门控限制（DCA自身有独立安全检查）
        # 但下跌熔断时只允许低位DCA（价格<SMA72），不允许趋势DCA
        dca_sig = self._check_dca_buy(s, pos, breaker_active=_breaker_active)
        if dca_sig:
            if regime_tag:
                dca_sig.reason = f"{regime_tag} {dca_sig.reason}"
            return dca_sig

        return TradeSignal("HOLD", 0, f"{regime_tag}无明确信号", 0)

    # ── v5.5 P2: 最近最高买入价追踪 ─────────────────────────────

    def _update_recent_max_buy(self, s: MarketState, last_buy_price: float) -> None:
        """
        维护最近 RECENT_BUY_EXPIRE_MIN 分钟内的最高买入价。
        用于保护高位新买入不被低位盈利止盈卖掉。
        """
        # 过期重置
        if self._recent_max_buy_ts_ms > 0 and \
           self._time_since_min(self._recent_max_buy_ts_ms) > self.RECENT_BUY_EXPIRE_MIN:
            if self._recent_max_buy_price > 0:
                log.debug(f"[P2] 近期最高买价过期,清除 (原${self._recent_max_buy_price:.2f})")
            self._recent_max_buy_price = 0.0
            self._recent_max_buy_ts_ms = 0

        # 检测到新的买入(s.last_buy_time 变新)
        if s.last_buy_time > 0 and s.last_buy_time > self._last_seen_buy_ts_ms:
            # 有新买入
            buy_price = last_buy_price if last_buy_price > 0 else s.last_price
            if buy_price > self._recent_max_buy_price:
                log.debug(f"[P2] 更新近期最高买价: ${self._recent_max_buy_price:.2f} → ${buy_price:.2f}")
                self._recent_max_buy_price = buy_price
            self._recent_max_buy_ts_ms = s.last_buy_time
            self._last_seen_buy_ts_ms = s.last_buy_time

    # ── 信号生成 ──────────────────────────────────────────────

    def _generate_signals(self, s: MarketState) -> Tuple[List, List]:
        buy, sell = [], []

        # 1. RSI — v5.3: 历史数据显示SOL低点RSI平均53（不是超卖），降低RSI权重
        rsi_buy = self.rsi_oversold + (8 if self.scalp_mode else 0)
        rsi_sell = self.rsi_overbought - (5 if self.scalp_mode else 0)
        rsi_turning_up = (s.rsi7 > s.rsi14)
        h1_bearish = (s.h1_trend_score < -30)

        if s.rsi14 < rsi_buy:
            if rsi_turning_up and not h1_bearish:
                buy.append(("RSI超卖拐头", 20 + (rsi_buy - s.rsi14) * 0.7))  # v5.3: 权重降低（RSI不可靠）
            elif rsi_turning_up and h1_bearish:
                buy.append(("RSI超卖(1H看跌)", max(5, (rsi_buy - s.rsi14) * 0.2)))
        elif s.rsi14 > rsi_sell:
            sell.append(("RSI高位", 20 + (s.rsi14 - rsi_sell) * 0.7))  # v5.3: 权重降低

        # 2. 布林带 — v5.3: 历史数据显示低点BB平均0.53，BB不是有效底部信号
        bb_buy = 0.3 if self.scalp_mode else 0.15  # 放宽（原0.25/0.1）
        bb_sell = 0.8 if self.scalp_mode else 0.85  # 放宽（原0.85/0.9）
        if s.bb_position < bb_buy:
            buy.append(("布林带下轨", 15 + (bb_buy - s.bb_position) * 60))  # v5.3: 权重降低
        elif s.bb_position > bb_sell:
            sell.append(("布林带上轨", 15 + (s.bb_position - bb_sell) * 60))

        # 3. MACD
        if s.macd_hist > 0.001:
            buy.append(("MACD多头", min(20, abs(s.macd_hist) * 1000)))
        elif s.macd_hist < -0.001:
            sell.append(("MACD空头", min(20, abs(s.macd_hist) * 1000)))

        # 4. 趋势
        if s.trend_score > self.trend_threshold:
            buy.append(("上涨趋势", s.trend_score * 0.3))
        elif s.trend_score < -self.trend_threshold:
            sell.append(("下跌趋势", abs(s.trend_score) * 0.3))

        # 5. 价格vs成本（网格）v4.0: ATR动态网格间距
        # 波动大时自动拉宽间距，避免频繁交易；波动小时收窄
        dynamic_grid = max(self.grid_spacing_pct, s.atr_pct * 1.5) if s.atr_pct > 0 else self.grid_spacing_pct
        ref = self._ref_cost(s)
        if ref > 0 and s.last_price > 0:
            diff = self._pct_diff(s.last_price, ref)
            if diff < -dynamic_grid:
                label = "卖出均价" if self._using_sell_avg(s) else "成本价"
                buy.append((f"价格低于{label}", abs(diff) * 5))
        actual = self._actual_cost(s)
        if actual > 0 and s.last_price > 0:
            sd = self._pct_diff(s.last_price, actual)
            if sd > dynamic_grid:
                sell.append(("价格高于成本价", sd * 5))

        # 6. 支撑阻力位（区间>3%时生效，修复窄区间矛盾信号）
        if s.support > 0 and s.resistance > 0 and s.last_price > 0:
            range_pct = (s.resistance - s.support) / s.last_price * 100
            if range_pct > 3.0:
                d_sup = (s.last_price - s.support) / s.last_price * 100
                d_res = (s.resistance - s.last_price) / s.last_price * 100
                if d_sup < 1.5:
                    buy.append(("接近支撑位", 20))
                if d_res < 1.5:
                    sell.append(("接近阻力位", 20))

        # 7. 成交量（方向感知，修复原版不区分方向的bug）
        if s.volume_ratio > 1.5:
            is_weak = s.rsi14 < 45 or s.bb_position < 0.4
            is_strong = s.rsi14 > 55 or s.bb_position > 0.6
            if is_weak:
                buy = [(n + "(放量)", sc * 1.2) for n, sc in buy]
            elif is_strong:
                sell = [(n + "(放量)", sc * 1.2) for n, sc in sell]
            else:
                buy = [(n + "(放量)", sc * 1.1) for n, sc in buy]
                sell = [(n + "(放量)", sc * 1.1) for n, sc in sell]

        # 8. BTC趋势（修复：原版收集数据但从未使用）
        if self.btc_trend_enabled and abs(s.btc_trend_score) > self.btc_trend_threshold:
            score = abs(s.btc_trend_score) * self.btc_trend_weight * 0.3
            if s.btc_trend_score > 0:
                buy.append(("BTC上涨", score))
            else:
                sell.append(("BTC下跌", score))

        # 9. 趋势权重调整
        if s.trend_score > 30:
            sell = [(n, sc * 0.4) for n, sc in sell]
        elif s.trend_score > 15:
            sell = [(n, sc * 0.65) for n, sc in sell]
        # 下跌趋势中抑制买入信号 v3.4: scalp适度放宽，允许低吸
        if s.trend_score < -30:
            buy = [(n, sc * (0.5 if self.scalp_mode else 0.4)) for n, sc in buy]
        elif s.trend_score < -15:
            buy = [(n, sc * (0.8 if self.scalp_mode else 0.7)) for n, sc in buy]

        # 10. v5.3: 1H趋势压制
        if s.h1_trend_score < -60:
            buy = [(n + "(1H强跌)", sc * 0.3) for n, sc in buy]
        elif s.h1_trend_score < -40:
            buy = [(n + "(1H下跌)", sc * 0.5) for n, sc in buy]
        elif s.h1_trend_score < -25:
            buy = [(n + "(1H偏弱)", sc * 0.7) for n, sc in buy]  # 补上空隙

        if s.h1_trend_score > 60:
            sell = [(n, sc * 0.5) for n, sc in sell]

        # ── v5.3 基于历史转折点数据的信号 ──

        # 11. H1趋势 — 最强信号源（低点平均-43, 高点平均+49, 差距92点）
        #     H1趋势从深度负值回升 = 底部反转信号
        #     H1趋势到达高位 = 顶部风险信号
        if s.h1_trend_score > 30:
            buy.append(("H1趋势强势", s.h1_trend_score * 0.5))  # 最高+50分
        elif s.h1_trend_score > 0:
            buy.append(("H1趋势转正", 15))
        if s.h1_trend_score < -45:
            sell.append(("H1趋势弱势", abs(s.h1_trend_score) * 0.3))  # 极端下跌才加卖出分
        elif s.h1_trend_score < -15:
            sell.append(("H1趋势偏弱", 8))

        # 12. 底部反转信号：H1趋势从深度负值回升 + 高波动 + RSI拐头
        #     历史数据: 低点H1平均-43，需要在-40附近就开始捕捉
        h1_recovering = -45 < s.h1_trend_score < 10  # 从深度负值回升中
        high_vol = s.atr_pct > 0.55  # 高波动（底部特征，历史平均0.73）
        if h1_recovering and high_vol and s.rsi7 > s.rsi14:
            buy.append(("底部反转信号", 20))

        # 13. 顶部风险信号：H1趋势>45 + 15天涨幅>10% + 波动率不高
        #     历史数据: 高点H1趋势+49, 15天+12.4%, ATR平均0.57
        moderate_vol = s.atr_pct < 0.7  # 放宽（0.5→0.7，SOL正常波动就0.5+）
        if s.h1_trend_score > 45 and s.long_term_trend_pct > 10 and moderate_vol:
            sell.append(("顶部风险信号", 25))

        # 14. 放量+趋势确认
        if s.volume_ratio > 1.5 and s.trend_score > 15:
            buy.append(("放量趋势确认", 15))

        # 15. ADX 过滤：无趋势时降低买入信号
        if s.adx < 15:
            buy = [(n, sc * 0.7) for n, sc in buy]

        return buy, sell

    # ── 仓位/价差信号调整 ──────────────────────────────────────

    def _adjust_for_position(self, s: MarketState, pos: float,
                             buy_sigs: List, sell_sigs: List) -> Tuple[List, List]:
        buy, sell = list(buy_sigs), list(sell_sigs)
        buy_score = sum(sc for _, sc in buy)
        sell_score = sum(sc for _, sc in sell)

        ref = self._ref_cost(s)
        pdiff = self._pct_diff(s.last_price, ref) if ref > 0 else 0

        # RSI极值调整
        if s.rsi14 < 30:
            boost = (30 - s.rsi14) * 2
            buy.append(("RSI超卖买入", boost))
            sell = [(n, sc * (0.3 if pos < 70 else 0.7)) for n, sc in sell]
        elif s.rsi14 > 70:
            boost = (s.rsi14 - 70) * 2
            sell.append(("RSI超买卖出", boost))
            buy = [(n, sc * 0.3) for n, sc in buy]

        # USDT占比高 → 优先买入 v3.4: scalp降低门槛到70%
        usdt_high = 70 if self.scalp_mode else 80
        if s.usdt_pct > usdt_high:
            sell = [(n, sc * 0.3) for n, sc in sell]
            if not any("USDT" in n for n, _ in buy):
                buy.append(("USDT占比高，优先买入", 35))
        # 低仓位 → 优先买入 v3.4: scalp扩大低仓位范围到25%
        elif pos < (25 if self.scalp_mode else 20):
            sell_mult = 0.2 if pos < self.min_position_pct else 0.5
            sell = [(n, sc * sell_mult) for n, sc in sell]
            extra = abs(pdiff) * 15 if pdiff < 0 else (25 if self.scalp_mode else 20)
            if not any("低仓位" in n for n, _ in buy):
                buy.append(("低仓位补仓", extra))
        # 重仓盈利 → 优先卖出
        elif pos > 60 and pdiff > 0.5:
            buy = [(n, sc * 0.3) for n, sc in buy]
            if not any("盈利止盈" in n for n, _ in sell):
                sell.append(("盈利止盈", pdiff * 8))
        # 轻仓亏损 → 优先买入
        elif pos < 40 and pdiff < -0.5:
            sell = [(n, sc * 0.3) for n, sc in sell]
            if not any("低位加仓" in n for n, _ in buy):
                buy.append(("低位加仓", abs(pdiff) * 8))

        # 满仓盈利强化
        if pos >= self.max_position_pct:
            profit = self._actual_profit_pct(s)
            if profit > self.MIN_PROFIT_PCT:
                buy = [(n, sc * 0.1) for n, sc in buy]
                if not any("满仓盈利" in n for n, _ in sell):
                    sell.append(("满仓盈利止盈", profit * 12))

        return buy, sell

    # ── 止损检查 ──────────────────────────────────────────────

    def _check_stop_loss(self, s: MarketState, pos: float,
                         last_buy_price: float) -> Optional[TradeSignal]:
        actual = self._actual_cost(s)
        if actual <= 0 or s.last_price <= 0:
            return None
        loss = (actual - s.last_price) / actual * 100

        # v5.2d: 止损冷却 — 上次止损后60分钟内不再触发
        if s.last_stop_loss_time > 0:
            since_last_stop = self._time_since_min(s.last_stop_loss_time)
            if since_last_stop < self.STOP_LOSS_COOLDOWN_MIN:
                return None

        # 最小持仓时间保护：刚买入30分钟内，止损阈值翻倍
        min_hold_min = 30.0
        loss_multiplier = 1.0
        if s.last_buy_time > 0:
            hold_min = self._time_since_min(s.last_buy_time)
            if hold_min < min_hold_min:
                loss_multiplier = 2.0

        # v5.2d: 仓位太小不值得止损（防止微量递减循环）
        if pos < 5 or (s.base_balance * s.last_price) < 10:
            return None

        # 情况1：重仓 + 大亏损 + 强下跌 → 果断大幅减仓
        if pos > 50 and loss >= self.stop_loss_pct * loss_multiplier and s.trend_score < -40:
            # v5.2d: 一次性减到安全仓位，不要零敲碎打
            target_pos = max(20, self.min_position_pct)
            pct_sell = pos - target_pos
            return TradeSignal("SELL", 100,
                f"触发止损: 仓位{pos:.0f}%亏损{loss:.1f}%，减仓到{target_pos:.0f}%",
                pct_sell, stop_loss=actual * (1 - self.stop_loss_pct / 100))

        # 情况2：回调买入后成本上升
        if last_buy_price > 0:
            est_cost = s.cost_price * 0.6 + last_buy_price * 0.4
            if s.last_price < est_cost:
                loss2 = (est_cost - s.last_price) / est_cost * 100
                if loss2 >= self.stop_loss_pct * 2 * loss_multiplier and s.trend_score < -40:
                    target_pos = max(15, pos * 0.5)
                    pct_sell = pos - target_pos
                    if pct_sell > 3:
                        return TradeSignal("SELL", 100,
                            f"触发止损: 回调买入亏损{loss2:.1f}%，减仓{pct_sell:.0f}%",
                            pct_sell, stop_loss=actual * (1 - self.stop_loss_pct / 100))

        # 情况3：持续下跌 → 减仓到30%以下
        if loss >= self.stop_loss_pct * 1.5 * loss_multiplier and s.trend_score < -30 and pos > 30:
            target_pos = max(15, pos * 0.5)
            pct_sell = pos - target_pos
            if pct_sell > 3:
                return TradeSignal("SELL", 100,
                    f"触发止损: 持续下跌亏损{loss:.1f}%，减仓到{target_pos:.0f}%",
                    pct_sell, stop_loss=actual * (1 - self.stop_loss_pct / 100))

        # 情况4：快速下跌
        if s.recent_high > 0 and s.last_price > 0:
            drop = (s.recent_high - s.last_price) / s.recent_high * 100
            if drop >= 5.0 and loss >= self.stop_loss_pct * loss_multiplier and pos > 25:
                target_pos = max(15, pos * 0.6)
                pct_sell = pos - target_pos
                if pct_sell > 3:
                    return TradeSignal("SELL", 100,
                        f"触发止损: 高点下跌{drop:.1f}%亏损{loss:.1f}%，减仓{pct_sell:.0f}%",
                        pct_sell, stop_loss=actual * (1 - self.stop_loss_pct / 100))

        return None

    # ── v5.2 Smart DCA（定投进攻）──────────────────────────────

    def _check_dca_buy(self, s: MarketState, pos: float,
                        breaker_active: bool = False) -> Optional[TradeSignal]:
        """
        Smart DCA v5.2d：分两种模式——
        - 低位DCA：价格<SMA72时积极定投（抄底）
        - 趋势DCA：价格>SMA72但趋势向上时小额定投（跟涨）
        """
        # ── 基础条件 ──
        if s.consecutive_buys >= 8:
            return None
        if pos >= 60:
            return None
        if s.btc_trend_score < -40:
            return None
        # 距上次买入间隔要求
        min_interval = 240  # 默认4小时

        # ── 判断DCA模式 ──
        below_sma72 = s.sma72 > 0 and s.last_price < s.sma72

        if below_sma72:
            # 低位DCA：价格低于长期均线，积极抄底
            if s.rsi14 > 65:
                return None  # RSI太高不买
            if s.last_buy_time > 0 and self._time_since_min(s.last_buy_time) < min_interval:
                return None
            # 亏损暂停：持仓亏>3%需要反转信号
            if s.cost_price > 0 and pos > 5:
                unrealized_loss = (s.cost_price - s.last_price) / s.cost_price * 100
                if unrealized_loss > 3:
                    if not (s.rsi7 > s.rsi14 and s.last_price > s.recent_low * 1.02):
                        return None
            # 买入量
            base_pct = 2.0
            if s.rsi14 < 35:
                base_pct = 4.0
            if s.sma72 > 0:
                discount = (s.sma72 - s.last_price) / s.sma72 * 100
                if 5 < discount <= 15:
                    base_pct = min(5.0, base_pct * 1.3)
            mode = "低位"
        else:
            # 趋势DCA：价格在SMA72上方，小额跟涨
            if breaker_active:
                return None
            # 空仓或低仓位时放宽门槛（防止长期空仓踏空）
            trend_threshold = -10 if pos < 10 else 10
            if s.trend_score < trend_threshold:
                return None  # 趋势不明朗，不跟
            if s.rsi14 > 70:
                return None  # 超买不追
            if pos >= 40:
                return None  # 趋势DCA仓位上限更低
            min_interval = 480  # 趋势DCA间隔8小时（更保守）
            if s.last_buy_time > 0 and self._time_since_min(s.last_buy_time) < min_interval:
                return None
            # 趋势DCA量更小
            base_pct = 1.5
            if s.h1_trend_score > 30:
                base_pct = 2.0  # 1H确认上涨可以多买点
            mode = "趋势"

        # 连续买入缩量
        if s.consecutive_buys > 5:
            base_pct *= 0.3
        elif s.consecutive_buys > 3:
            base_pct *= 0.5
        if base_pct < 0.5:
            return None

        sma_tag = f"<SMA72(${s.sma72:.0f})" if below_sma72 else f">SMA72(${s.sma72:.0f})"
        reason = f"Smart DCA({mode}): ${s.last_price:.2f}{sma_tag}, RSI={s.rsi14:.0f}, 趋势={s.trend_score:.0f}, 投{base_pct:.1f}%"
        log.info(f"[DCA] {reason}")
        return TradeSignal("BUY", 40, reason, base_pct)

    # ── v5.2 趋势跟踪（捕获大行情）────────────────────────────

    def _check_trend_follow(self, s: MarketState, pos: float,
                             last_buy_price: float) -> Optional[TradeSignal]:
        """
        趋势跟踪：1H均线金叉+趋势确认时持续买入；死叉+有利润时退出。
        学自 Turtle Trading + EMA Slope 策略。
        """
        # --- 趋势买入（v5.2e: 更灵活的条件组合）---
        h1_golden = s.h1_sma7 > s.h1_sma24 > 0         # 1H均线金叉
        h1_positive = s.h1_trend_score > 0               # 1H趋势为正（不必>20）
        trend_up = s.trend_score > 15                    # 15分钟趋势向上
        lt_up = s.long_term_trend_pct > -3               # 15天趋势不是深跌
        not_bear = s.regime != "BEAR"
        adx_trend = s.adx > 20                           # 趋势存在
        rsi_ok = 30 < s.rsi14 < 65                       # RSI适中区间

        trend_score_count = sum([h1_golden, h1_positive, trend_up, lt_up, not_bear, adx_trend, rsi_ok])
        # 冷却2小时
        if s.last_buy_time > 0 and self._time_since_min(s.last_buy_time) < 120:
            return None
        # v5.5 P1: SELL→BUY 10分钟冷却,避免反复高频
        if s.last_sell_time > 0 and self._time_since_min(s.last_sell_time) < self.SELL_BUY_COOLDOWN_MIN:
            since_sell = self._time_since_min(s.last_sell_time)
            log.debug(f"[P1-COOLDOWN] 趋势跟踪买入被SELL冷却阻止: 距上次卖出{since_sell:.1f}min<{self.SELL_BUY_COOLDOWN_MIN}min")
            return None
        # v5.5.11 价差过滤
        if s.last_sell_time > 0 and s.last_sell_price > 0:
            hours_since_sell = self._time_since_min(s.last_sell_time) / 60
            if hours_since_sell < 4:
                retrace_pct = (s.last_sell_price - s.last_price) / s.last_sell_price * 100
                if retrace_pct < 0.5:
                    log.debug(f"[TREND-RETRACE] 距上次卖出{hours_since_sell:.1f}h+回撤{retrace_pct:.2f}%<0.5%")
                    return None
        if trend_score_count >= 4 and not_bear:  # 7条件满足4个+非熊市
            effective_max = getattr(self, '_dynamic_max_pos', self.max_position_pct)
            if pos < effective_max:
                buy_pct = min(6.0, effective_max - pos)  # 从8%降到6%
                reason = (f"趋势跟踪买入: 1H金叉(SMA7={s.h1_sma7:.2f}>SMA24={s.h1_sma24:.2f}), "
                          f"趋势={s.h1_trend_score:.0f}, ADX={s.adx:.0f}, 15d趋势={s.long_term_trend_pct:+.1f}%")
                log.info(f"[TREND] {reason}")
                return TradeSignal("BUY", 60, reason, buy_pct)

        # --- 趋势退出（1H趋势转负+有利润）---
        h1_death = s.h1_trend_score < -20 and s.trend_score < -15
        if h1_death and pos > 20:
            profit = self._actual_profit_pct(s)
            if profit > 1.0:
                sell_pct = min(15, pos * 0.3)
                reason = f"趋势退出: 1H死叉, 利润{profit:.1f}%, 减仓{sell_pct:.0f}%"
                log.info(f"[TREND] {reason}")
                return TradeSignal("SELL", 50, reason, sell_pct)

        return None

    # ── 买入决策 ──────────────────────────────────────────────

    def _process_buy(self, s: MarketState, pos: float, score: float,
                     sigs: List, total_bal: float) -> TradeSignal:
        confidence = min(100, score)
        reasons = [n for n, _ in sorted(sigs, key=lambda x: -x[1])[:3]]
        reason = "买入信号: " + ", ".join(reasons)

        # v5.5.4 Fix1: P1 冷却期扩展到主买入路径 (SELL→BUY 10分钟冷却)
        # 原来只有 _check_trend_follow 有 P1, 导致 _process_buy 主路径绕过冷却 —
        # 04-18 02:39→02:44, 02:50→02:54 的 4.8min 反复就是这个漏洞
        # 例外: 极低仓位(<8%)时允许买入,避免因单次止盈导致仓位归零无法参与行情
        if pos >= 8 and s.last_sell_time and s.last_sell_time > 0:
            _since_sell = self._time_since_min(s.last_sell_time)
            if _since_sell < self.SELL_BUY_COOLDOWN_MIN:
                log.info(f"[P1] 主买入路径被SELL冷却阻止: 距上次卖出{_since_sell:.1f}min<{self.SELL_BUY_COOLDOWN_MIN}min (仓位{pos:.0f}%)")
                return TradeSignal("HOLD", 0,
                    f"卖出后冷却中({_since_sell:.0f}/{self.SELL_BUY_COOLDOWN_MIN:.0f}min)，避免高频反复", 0)

        # v5.5.4 Fix3: 新闻强利空时禁止主动买入 (DCA/低位补仓路径不受限,下方 bottom_signals 仍可激活)
        # 04-19 12:06 新闻-40 时策略还买$86.29, 结果持续下跌到$83.5,就是这个问题
        if s.news_sentiment <= -30 and s.news_confidence >= 0.5:
            # 但极度超卖(RSI<25) + 多个底部信号时仍可逆势买 (机会大于风险)
            bottom_n, _ = self._bottom_signals(s)
            if bottom_n < 3 and s.rsi14 > 25:
                log.info(f"[P3] 新闻利空(score={s.news_sentiment},conf={s.news_confidence:.0%})禁止主动买入 (RSI{s.rsi14:.0f},bottom_n={bottom_n})")
                return TradeSignal("HOLD", 0,
                    f"新闻利空({s.news_sentiment}/conf{s.news_confidence:.0%})禁止主动买入", 0)

        ref = self._ref_cost(s)
        pdiff = self._pct_diff(s.last_price, ref) if ref > 0 else 0
        pullback = self._pullback(s)
        recovery = self._recovery(s)
        is_strong_up = s.trend_score > 30
        is_mod_up = s.trend_score > 15
        is_strong_down = s.trend_score < -30
        is_mod_down = s.trend_score < -15
        price_drop = abs(pdiff) if pdiff < 0 else 0
        price_rise = pdiff if pdiff > 0 else 0
        bottom_n, bottom_reasons = self._bottom_signals(s)
        is_bottom = bottom_n >= 2

        # 尝试特殊买入（回调/批次/USDT主导）
        special = self._try_special_buy(s, pos, ref, pdiff, total_bal, reason)
        if special:
            return special

        # 价格高于成本 → 检查是否允许
        trend_m, depth_m = 1.0, 1.0
        is_special_buy = False

        if is_strong_up and price_rise <= 1.0 and pullback >= 2.0:
            trend_m = 0.3 if price_rise <= 0.3 else 0.2
            depth_m = 0.3 if price_rise <= 0.3 else 0.2
            reason += f" | 强上涨回调{pullback:.1f}%，价格高于成本{price_rise:.1f}%"
            is_special_buy = True
        elif is_mod_up and price_rise <= 0.5 and pullback >= 1.5:
            trend_m = 0.25 if price_rise <= 0.2 else 0.15
            depth_m = 0.25 if price_rise <= 0.3 else 0.15
            reason += f" | 中等上涨回调{pullback:.1f}%，放宽买入"
            is_special_buy = True
        # 极低仓位建仓：仓位太低无法参与行情，允许追涨（非下跌趋势+价格不在高位）
        elif pos < 15 and not is_strong_down and not is_mod_down:
            max_rise = 10.0 if pos < 3 else (6.0 if pos < 5 else (3.0 if pos < 10 else 2.0))
            if price_rise <= max_rise and (s.bb_position < 0.55 or is_strong_up):
                trend_m = 0.4 if pos < 5 else 0.3
                depth_m = 0.4 if pos < 5 else 0.3
                reason += f" | 低仓位({pos:.1f}%)建仓"
                is_special_buy = True

        # 回调买入（从高点回调≥3%或15天趋势上涨+回调≥2%）
        is_lt_up = s.long_term_trend_pct > 0
        if not is_special_buy and price_rise > 0:
            can_pullback = ((is_strong_up or is_mod_up) and pullback >= 3.0) or (is_lt_up and pullback >= 2.0)
            if can_pullback:
                # 检查利润空间
                if pos < 20: est_cost = (s.cost_price + s.last_price) / 2
                elif pos < 50: est_cost = s.cost_price * 0.67 + s.last_price * 0.33
                else: est_cost = s.cost_price * 0.8 + s.last_price * 0.2
                max_loss = min(1.0, 0.3 + pullback * 0.15) + (0.2 if is_lt_up else 0)
                if s.last_price < est_cost and (est_cost - s.last_price) / est_cost * 100 > max_loss:
                    return TradeSignal("HOLD", 0,
                        f"回调买入利润空间不足，预估新成本${est_cost:.2f}", 0)
                trend_m, depth_m = 0.4, min(0.8, pullback / 4.0)
                reason += f" | 从高点回调{pullback:.1f}%"
                is_special_buy = True
            elif price_rise > 0 and not is_special_buy:
                # 牛市趋势买入：允许在成本之上一定范围内买入
                rp = self._regime_params if self._regime_params else {}
                max_above = rp.get("max_above_cost_buy_pct", 0)
                if max_above > 0 and price_rise <= max_above and (pullback >= 0.5 or s.bb_position < 0.6):
                    trend_m = 0.5 + 0.3 * (1.0 - price_rise / max_above)
                    depth_m = 0.4
                    reason += f" | 牛市趋势买入(高于成本{price_rise:.1f}%<{max_above:.1f}%)"
                    is_special_buy = True
                elif pos < 15 and confidence >= 40 and s.regime != "BEAR":
                    # STA: 低仓位+强信号+非熊 → 允许高于成本小额建仓
                    trend_m = 0.3
                    depth_m = 0.2
                    # 硬上限：高于成本建仓最多到10%
                    if pos >= 10:
                        return TradeSignal("HOLD", 0,
                            f"高于成本建仓已达10%上限({pos:.0f}%)", 0)
                    reason += f" | 信号建仓(高于成本{price_rise:.1f}%,仓位{pos:.0f}%)"
                    is_special_buy = True
                else:
                    return TradeSignal("HOLD", 0,
                        f"价格高于成本{price_rise:.1f}%，不允许买入", 0)

        # 下跌趋势保守策略
        if is_strong_down:
            if is_bottom:
                trend_m = 0.7 if price_drop >= 0.3 else 0.5
                reason += f" | 可能触底：{', '.join(bottom_reasons[:2])}"
            elif recovery > 0.3:
                trend_m = 0.6
                reason += f" | 从低点回升{recovery:.1f}%"
            elif price_drop < 0.5:
                return TradeSignal("HOLD", 0,
                    f"强下跌趋势中，跌幅仅{price_drop:.1f}%，等待更深回调", 0)
            else:
                trend_m = 0.5
        elif is_mod_down:
            if is_bottom:
                trend_m = 0.8 if price_drop >= 0.3 else 0.6
                reason += f" | 可能触底：{', '.join(bottom_reasons[:2])}"
            elif price_drop < 0.8:
                return TradeSignal("HOLD", 0,
                    f"下跌趋势中，跌幅仅{price_drop:.1f}%，等待更深回调", 0)
            else:
                trend_m = 0.7

        # 深度调整
        if price_drop > 0:
            if price_drop < 1.0: depth_m = 0.3
            elif price_drop < 2.0: depth_m = 0.5
            elif price_drop < 3.0: depth_m = 0.8
            else: depth_m = 1.0
        if pdiff > 0:
            depth_m *= 0.7

        # 计算仓位
        position_pct = self._calc_position(
            confidence, trend_m, depth_m, s.bb_position, pullback, recovery, True)

        # 上涨趋势 + 低仓位 → 适度加大买入（不激进追高）
        if is_strong_up and pos < 30:
            position_pct *= 1.3
        elif is_mod_up and pos < 20:
            position_pct *= 1.15

        position_pct = min(position_pct, self.max_position_pct - pos)

        # 价格位置检查（scalp跳过）
        if not self._price_ok_buy(s, is_bottom, is_special_buy, pdiff, pullback):
            return TradeSignal("HOLD", 0,
                f"买入信号已出现，但价格不在低点(bb={s.bb_position:.2f})", 0)

        # 边际检查
        if not self._check_edge(s, "BUY"):
            return TradeSignal("HOLD", 0, "利润空间不足", 0)

        return TradeSignal("BUY", confidence, reason, position_pct)

    # ── 特殊买入 ──────────────────────────────────────────────

    def _try_special_buy(self, s: MarketState, pos: float, ref: float,
                         pdiff: float, total_bal: float, base_reason: str) -> Optional[TradeSignal]:
        is_up = s.trend_score > 15

        # USDT主导 + 分批买入 v3.4: scalp降低USDT门槛，更快回补仓位
        usdt_threshold = 50 if self.scalp_mode else 60
        if s.avg_sell_price > 0 and s.usdt_pct > usdt_threshold and s.last_price < s.avg_sell_price:
            pm = self._profit_margin(s.avg_sell_price, s.last_price)
            batch = self._check_batch_buy(s, pos, total_bal)
            min_req = (0.4 if self.scalp_mode else self.MIN_PROFIT_FIRST_PCT) if not batch else (0.4 if self.scalp_mode else self.MIN_PROFIT_PCT)

            if pm >= min_req:
                if s.last_sell_time > 0 and self._time_since_min(s.last_sell_time) < self.SELL_COOLDOWN_MIN:
                    return TradeSignal("HOLD", 0,
                        f"USDT主导，套利空间{pm:.1f}%，但卖出冷却中", 0)

                if batch:
                    pct_buy = batch["position_pct"]
                    reason = batch["reason"]
                else:
                    usage = self._usdt_usage_pct(pm, s.usdt_pct)
                    if total_bal > 0 and s.last_price > 0:
                        buy_val = s.usdt_balance * (usage / 100)
                        target = (buy_val / total_bal) * 100
                        pct_buy = min(target * self.BATCH_RATIOS[0], self.max_position_pct - pos)
                    else:
                        pct_buy = self.base_trade_pct * 0.5
                    reason = f"分批买入第1批 | USDT占比{s.usdt_pct:.1f}%，套利空间{pm:.1f}%"

                if not self._check_edge(s, "BUY"):
                    return TradeSignal("HOLD", 0, "利润空间不足", 0)
                return TradeSignal("BUY", 70, reason, max(pct_buy, self.base_trade_pct * 0.3))
            else:
                return TradeSignal("HOLD", 0,
                    f"USDT占比{s.usdt_pct:.1f}%，套利空间{pm:.1f}%不足(需≥{min_req:.1f}%)", 0)

        # 回调买入（趋势中卖出后价格回调）v3.4: scalp放宽回调门槛
        if is_up and s.last_sell_price > 0 and s.last_price < s.last_sell_price:
            drop = (s.last_sell_price - s.last_price) / s.last_sell_price * 100
            pm = self._profit_margin(s.last_sell_price, s.last_price)
            if self.scalp_mode:
                min_req = max(0.8, self.MIN_PROFIT_PCT)
                min_drop = 0.8
            else:
                min_req = max(1.5, self.MIN_PROFIT_PCT)
                min_drop = 1.5
            if drop < min_drop * 1.5:
                min_req = max(min_req, min_req * 1.5)

            if drop >= min_drop and pm >= min_req:
                if s.last_sell_time == 0 and pm < 1.5:
                    return TradeSignal("HOLD", 0,
                        f"回调{drop:.1f}%但缺少卖出时间，需套利≥1.5%", 0)
                if s.last_sell_time > 0 and self._time_since_min(s.last_sell_time) < self.SELL_COOLDOWN_MIN:
                    return TradeSignal("HOLD", 0,
                        f"回调{drop:.1f}%但卖出冷却中", 0)

                # 等量买入
                if s.last_sell_qty > 0 and total_bal > 0 and s.last_price > 0:
                    buy_val = s.last_sell_qty * s.last_price
                    target = (buy_val / total_bal) * 100
                    pct_buy = min(target, self.max_position_pct - pos)
                else:
                    pct_buy = self.base_trade_pct * 0.7

                if not self._check_edge(s, "BUY"):
                    return TradeSignal("HOLD", 0, "利润空间不足", 0)
                trend_label = "强上涨" if s.trend_score > 30 else "中等上涨"
                return TradeSignal("BUY", 70,
                    f"{trend_label}趋势回调{drop:.1f}%，套利{pm:.1f}%，等量买入",
                    max(pct_buy, self.base_trade_pct * 0.3))

            elif drop >= 0.5:
                return TradeSignal("HOLD", 0,
                    f"回调{drop:.1f}%但套利空间{pm:.1f}%不足(需≥{min_req:.1f}%)", 0)

        # 非上涨趋势的卖出价买入
        if not is_up and s.last_sell_price > 0 and s.last_price < s.last_sell_price:
            pm = self._profit_margin(s.last_sell_price, s.last_price)
            drop_pct = (s.last_sell_price - s.last_price) / s.last_sell_price * 100
            min_req = max(1.5, self.MIN_PROFIT_PCT)
            if drop_pct < 2.5:
                min_req = max(2.5, min_req * 1.5)

            if pm >= min_req:
                if s.last_sell_time == 0 and pm < 2.0:
                    return TradeSignal("HOLD", 0,
                        f"价格低于卖出价但缺少时间记录，需套利≥2%", 0)
                if s.last_sell_time > 0 and self._time_since_min(s.last_sell_time) < self.SELL_COOLDOWN_MIN:
                    return TradeSignal("HOLD", 0,
                        f"价格低于卖出价但冷却中", 0)

                # 计算目标买入价
                if s.cost_price > 0:
                    profit_pct = (s.last_sell_price - s.cost_price) / s.cost_price * 100
                    if profit_pct > 5.0:
                        target = s.cost_price * 1.01 * 0.6 + s.last_sell_price * 0.99 * 0.4
                    elif profit_pct > 2.0:
                        target = (s.cost_price + s.last_sell_price) / 2
                    else:
                        target = s.cost_price * 0.4 + s.last_sell_price * 0.6
                    target = max(s.cost_price * 0.98, min(target, s.last_sell_price * 0.98))
                else:
                    target = s.last_sell_price * 0.98

                if s.last_price > target * 1.01:
                    return TradeSignal("HOLD", 0,
                        f"价格${s.last_price:.2f}高于目标买入价${target:.2f}", 0)

                if s.last_sell_qty > 0 and total_bal > 0:
                    buy_val = s.last_sell_qty * s.last_price
                    pct_buy = min((buy_val / total_bal) * 100, self.max_position_pct - pos)
                else:
                    pct_buy = self.base_trade_pct * 0.7

                if not self._check_edge(s, "BUY"):
                    return TradeSignal("HOLD", 0, "利润空间不足", 0)
                return TradeSignal("BUY", 60,
                    f"价格低于卖出价，套利{pm:.1f}%，等量买入",
                    max(pct_buy, self.base_trade_pct * 0.3))

        return None

    # ── 卖出决策 ──────────────────────────────────────────────

    def _process_sell(self, s: MarketState, pos: float, score: float,
                      sigs: List, last_buy_price: float = 0.0) -> TradeSignal:
        confidence = min(100, score)
        reasons = [n for n, _ in sorted(sigs, key=lambda x: -x[1])[:3]]
        reason = "卖出信号: " + ", ".join(reasons)

        actual = self._actual_cost(s)
        profit = self._pct_diff(s.last_price, actual) if actual > 0 else 0
        pullback = self._pullback(s)
        recovery = self._recovery(s)
        is_strong_up = s.trend_score > 30
        is_mod_up = s.trend_score > 15
        is_strong_down = s.trend_score < -30
        loss_pct = abs(profit) if profit < 0 else 0

        # ── 来回交易防护 ──
        # 如果最近买入价已知且卖出价低于买入价+手续费，拒绝卖出（止损除外）
        # 防止"买$87.60→卖$87.50"的来回亏损循环
        # v5.5 P2: 用 max(last_buy_price, 近期最高买价) 强化保护
        # v5.5.3: 新闻强利空豁免 — 避免 P2 挡掉新闻预警的卖出
        news_bearish_override = (s.news_sentiment <= -50 and s.news_confidence >= 0.7)
        protect_price = max(last_buy_price, self._recent_max_buy_price)
        if protect_price > 0 and profit < 5.0 and not news_bearish_override:
            fee_round_trip = self.fee_bps * 2 / 10000  # 0.002 = 0.2%
            buffer = self.RECENT_BUY_PROFIT_BUFFER_PCT / 100  # +0.3% 实质利润缓冲
            min_sell = protect_price * (1 + fee_round_trip + buffer)
            if s.last_price < min_sell:
                src = "近期最高买价" if self._recent_max_buy_price > last_buy_price else "最近买价"
                return TradeSignal("HOLD", 0,
                    f"卖价{s.last_price:.2f}<{src}{protect_price:.2f}+手续费+缓冲({min_sell:.2f})，防止来回亏损", 0)
        elif news_bearish_override and protect_price > 0:
            log.info(f"[P2] 新闻强利空(score={s.news_sentiment},conf={s.news_confidence:.0%})豁免近期买价保护")

        # v5.4: 买入后冷却 — 买入15分钟内不触发常规卖出（止损走独立路径不受影响）
        if s.last_buy_time and s.last_buy_time > 0:
            _since_buy = self._time_since_min(s.last_buy_time)
            if _since_buy < self.BUY_SELL_COOLDOWN_MIN:
                log.info(f"[COOLDOWN] 买入后{_since_buy:.0f}min < {self.BUY_SELL_COOLDOWN_MIN}min，暂不卖出")
                return TradeSignal("HOLD", 0,
                    f"买入后冷却中({_since_buy:.0f}/{self.BUY_SELL_COOLDOWN_MIN:.0f}min)，暂不卖出", 0)

        # v5.5.4 Fix2: 连续止盈冷却 — 上次止盈后 20min 内不再触发小额常规止盈
        # 解决 04-18 02:44-03:50 BULL期高频止盈再买回的问题 (每次0.2%手续费白送)
        # 例外: (1)浮盈≥5% — 大利润正常止盈让利润落袋 (2)强趋势转负 — 趋势结束该退
        # (3)新闻强利空 — 新闻预警不受限 (4)止损 — 走独立路径不经过这里
        if s.last_sell_time and s.last_sell_time > 0:
            _since_sell = self._time_since_min(s.last_sell_time)
            if _since_sell < self.SELL_SELL_COOLDOWN_MIN:
                # 豁免条件
                big_profit = profit >= 5.0   # 浮盈够大,正常止盈
                trend_flipped = s.h1_trend_score < -30 and s.trend_score < -15  # 趋势明显转弱
                news_strong_bear = (s.news_sentiment <= -50 and s.news_confidence >= 0.7)
                if not (big_profit or trend_flipped or news_strong_bear):
                    log.info(f"[P3] 止盈冷却中: 距上次卖出{_since_sell:.1f}min<{self.SELL_SELL_COOLDOWN_MIN}min (浮盈{profit:.1f}%)")
                    return TradeSignal("HOLD", 0,
                        f"止盈冷却({_since_sell:.0f}/{self.SELL_SELL_COOLDOWN_MIN:.0f}min)+浮盈{profit:.1f}%<5%,等待更好时机", 0)

        # 修正reason中不准确的描述
        if profit <= 0 and "价格高于成本价" in reason:
            reason = reason.replace("价格高于成本价, ", "").replace(", 价格高于成本价", "").replace("价格高于成本价", "")

        trend_m, depth_m = 1.0, 1.0

        # 上涨趋势仓位保护（强趋势中低利润不卖，让利润奔跑）
        effective_min = self.min_position_pct
        # 杠杆+长期上涨：更积极持仓（杠杆放大上涨收益，不要轻易卖）
        if self.leverage > 1 and s.long_term_trend_pct > 5:
            effective_min = max(effective_min, 25)
        if is_strong_up and profit < 8.0:
            effective_min = max(effective_min, 20 if self.leverage <= 1 else 30)
        if pos <= effective_min:
            return TradeSignal("HOLD", 0,
                f"上涨趋势保护仓位>{effective_min:.0f}%(当前{pos:.0f}%，盈利{profit:.1f}%)", 0)

        # 亏损处理
        if profit <= 0:
            if self.scalp_mode and loss_pct <= 0.3 and not is_strong_down:
                trend_m, depth_m = 0.4, 0.3
                reason = f"高频模式微亏止损：亏损{loss_pct:.2f}%≤0.3%"
            elif is_strong_down and loss_pct >= self.stop_loss_pct:
                # V型反弹保护：价格已从低点反弹1%+则暂不止损
                rec = self._recovery(s)
                if rec > 1.0:
                    return TradeSignal("HOLD", 0,
                        f"亏损{loss_pct:.1f}%但已反弹{rec:.1f}%，暂不止损", 0)
                trend_m, depth_m = 0.5, 0.6
                reason = f"触发止损: 强下跌趋势，亏损{loss_pct:.1f}%，止损卖出"
            elif is_strong_down and loss_pct < self.stop_loss_pct:
                return TradeSignal("HOLD", 0,
                    f"强下跌中亏损{loss_pct:.2f}%不足{self.stop_loss_pct}%，暂不止损", 0)
            else:
                return TradeSignal("HOLD", 0,
                    f"当前亏损{loss_pct:.2f}%，不允许卖出", 0)
        else:
            # 盈利状态
            # 早期下跌预警
            early_n, early_reasons = self._early_bearish(s, pullback)
            is_full_profit = pos >= self.max_position_pct and profit > self.MIN_PROFIT_PCT
            if s.regime == REGIME_SIDEWAYS:
                # v5.5.15: SIDEWAYS 减摩擦 — early_warning 在震荡里多为噪音，
                # 抬高门槛减少碎卖与回补摩擦。BULL/BEAR 不动。
                early_required_n = 4
                early_min_profit = 1.5
            else:
                early_required_n = 2
                early_min_profit = 0.3

            if early_n >= early_required_n and profit > early_min_profit:
                # 预警卖出
                if profit >= 2.0 and early_n >= 3:
                    depth_m, trend_m = 0.6, 0.7
                elif profit >= 1.5:
                    depth_m, trend_m = 0.4, 0.6
                elif profit >= 0.8:
                    depth_m, trend_m = 0.3, 0.5
                else:
                    depth_m, trend_m = 0.2, 0.4
                reason = f"提前预警：{', '.join(early_reasons[:3])}，盈利{profit:.2f}%"
                if is_strong_up:
                    depth_m *= 0.8; trend_m *= 0.8

            elif pullback >= 2.0 and profit > 0:
                # 从高点回调止盈
                if pullback >= 4.0: depth_m, trend_m = 0.8, 0.8
                elif pullback >= 3.0: depth_m, trend_m = 0.6, 0.7
                else: depth_m, trend_m = 0.4, 0.6
                reason += f" | 从高点回调{pullback:.1f}%，止盈"

            # 刚买入后反弹保护（自适应）v4.1: 平衡利润和机会
            if s.regime == REGIME_BULL:
                min_sell_profit = 1.5   # 牛市：让利润跑但不贪
            elif s.regime == REGIME_BEAR:
                min_sell_profit = 0.5   # 熊市：有利润就锁定
            else:
                min_sell_profit = 0.8   # 横盘：0.8%即可
            if profit > 0 and recovery > 0.5 and not is_full_profit:
                if profit < min_sell_profit:
                    return TradeSignal("HOLD", 0,
                        f"刚买入盈利{profit:.2f}%，等待更高价位(目标>{min_sell_profit}%)", 0)
                elif profit < 2.5:
                    trend_m = min(trend_m, 0.3)
                    depth_m = min(depth_m, 0.4)

            # 上涨趋势查表
            trend_m = self._uptrend_sell_mult(s, profit, pullback, trend_m, is_full_profit)
            if trend_m <= 0:
                return TradeSignal("HOLD", 0,
                    f"上涨趋势中盈利{profit:.2f}%不足，继续持有", 0)

            # 深度调整
            if profit > 0:
                if pullback >= 2.0:
                    if profit < 1.0: depth_m = max(depth_m, 0.3)
                    elif profit < 2.0: depth_m = max(depth_m, 0.5)
                    elif profit < 3.0: depth_m = max(depth_m, 0.8)
                    else: depth_m = max(depth_m, 1.0)
                else:
                    if profit < 1.5: depth_m = min(depth_m, 0.4)
                    elif profit < 2.0: depth_m = min(depth_m, 0.5)
                    elif profit < 3.0: depth_m = min(depth_m, 0.8)
                    # profit >= 3.0 → depth_m stays

        # 计算仓位
        position_pct = self._calc_position(
            confidence, trend_m, depth_m, s.bb_position, pullback, recovery, False)
        position_pct = min(position_pct, pos - effective_min)

        # 价格位置检查（止损/预警/scalp跳过）
        is_stop = "触发止损" in reason or "止损卖出" in reason
        is_warn = "提前预警" in reason
        is_pb_profit = "从高点回调" in reason and profit > 0

        if not (is_stop or is_warn or is_pb_profit):
            if not self._price_ok_sell(s, profit, recovery):
                return TradeSignal("HOLD", 0,
                    f"卖出信号已出现，但价格不在高点(bb={s.bb_position:.2f})", 0)

        # 边际检查（止损跳过）
        if not is_stop and not self._check_edge(s, "SELL"):
            return TradeSignal("HOLD", 0, "利润空间不足", 0)

        return TradeSignal("SELL", confidence, reason, max(position_pct, self.base_trade_pct * 0.3))

    # ── 上涨趋势卖出乘数查表 ──────────────────────────────────

    def _uptrend_sell_mult(self, s: MarketState, profit: float, pullback: float,
                           current_m: float, is_full_profit: bool) -> float:
        is_strong = s.trend_score > 30
        is_mod = s.trend_score > 15

        if not (is_strong or is_mod):
            return current_m

        # (盈利范围, 有回调≥2%, 无回调)
        if is_strong:
            min_p = 1.5 if self.scalp_mode else 2.0
            table = [
                (min_p, None, None),  # 强上涨中低利润不卖
                (5.0, 0.7, 0.5),      # 中等利润小卖
                (8.0, 0.9, 0.7),      # 高利润正常卖
                (999, 1.0, 0.9),      # 超高利润大卖
            ]
            min_profit = min_p
        else:
            min_p = 0.8 if self.scalp_mode else 1.0
            table = [
                (min_p, None, None),  # 中等上涨中低利润不卖
                (4.0, 0.8, 0.6),
                (7.0, 0.95, 0.8),
                (999, 1.0, 0.95),
            ]
            min_profit = min_p

        if profit < min_profit:
            if (pullback >= 1.0 and profit > 0) or is_full_profit:
                m = 0.5 if is_strong else 0.6
                return min(current_m, m)
            return 0  # 信号：应HOLD

        for threshold, with_pb, no_pb in table:
            if profit < threshold and with_pb is not None:
                return min(current_m, with_pb if pullback >= 2.0 else no_pb)

        return current_m

    # ── 早期下跌信号 ──────────────────────────────────────────

    def _early_bearish(self, s: MarketState, pullback: float) -> Tuple[int, List[str]]:
        n, reasons = 0, []
        if s.macd_hist < -0.001:
            n += 1; reasons.append("MACD转负")
        if s.rsi14 > 65 and s.rsi7 < s.rsi14:
            n += 1; reasons.append("RSI高位回落")
        if pullback > 1.5 and s.trend_score < 10:
            n += 1; reasons.append(f"从高点回调{pullback:.1f}%")
        if -25 < s.trend_score < 0:
            n += 1; reasons.append("趋势转弱")
        if s.sma7 > 0 and s.last_price < s.sma7:
            n += 1; reasons.append("跌破SMA7")
        return n, reasons

    # ── 价格位置检查 ──────────────────────────────────────────

    def _price_ok_buy(self, s: MarketState, is_bottom: bool,
                      is_special: bool, pdiff: float, pullback: float) -> bool:
        if is_special or is_bottom:
            return True
        if self.scalp_mode:
            return True
        if s.bb_position < 0.5:
            return True
        if pullback >= 0.2:
            return True
        if s.support > 0:
            d = (s.last_price - s.support) / s.last_price * 100
            if 0 <= d < 3.0:
                return True
        if pdiff < 0:
            return True
        return False

    def _price_ok_sell(self, s: MarketState, profit: float, recovery: float) -> bool:
        if self.scalp_mode:
            return True
        if s.bb_position > 0.5:
            return True
        if recovery >= 0.2:
            return True
        if s.resistance > 0:
            d = (s.resistance - s.last_price) / s.last_price * 100
            if 0 <= d < 3.0:
                return True
        if profit > 0:
            return True
        return False

    # ── 边际检查 ──────────────────────────────────────────────

    def _check_edge(self, s: MarketState, action: str) -> bool:
        ref = self._ref_cost(s)
        if ref <= 0 or s.last_price <= 0:
            return True

        edge_mult = 0.3 if self.scalp_mode else 1.0
        # 杠杆下提高最低边际（杠杆放大手续费影响，scalp最低0.5）
        if self.leverage > 1:
            edge_mult = max(edge_mult, 0.5)
        # 杠杆+无明确趋势：进一步提高边际要求
        if self.leverage > 1 and abs(s.trend_score) < self.trend_threshold:
            edge_mult *= min(self.leverage * 0.5, 2.0)
        is_strong_up = s.trend_score > 30
        is_strong_down = s.trend_score < -30
        fee_pct = self.fee_bps * 2 / 10000 * 100  # 双边手续费百分比

        if action == "BUY":
            pdiff = self._pct_diff(s.last_price, ref)
            # 上涨趋势中允许小幅高于成本买入（需有回调且严格限制幅度）
            _lev_ambig = self.leverage > 1 and abs(s.long_term_trend_pct) < 3
            _min_pdiff = fee_pct * 2  # 至少高于成本 2倍手续费才值得买
            pullback = self._pullback(s)
            # 牛市自适应：允许更大的价格偏离
            rp = self._regime_params if self._regime_params else {}
            max_above = rp.get("max_above_cost_buy_pct", 0)
            if max_above > 0 and pdiff > 0 and pdiff <= max_above:
                if pullback >= 0.3 or s.bb_position < 0.6:
                    return True
            # 强上涨 + 有回调：最多高于成本1.5%
            if s.trend_score > 30 and pullback >= 1.5 and _min_pdiff < pdiff <= (0.8 if _lev_ambig else 1.5):
                return True
            # 中等上涨 + 有回调：最多高于成本0.8%
            if s.trend_score > 15 and pullback >= 1.0 and _min_pdiff < pdiff <= (0.5 if _lev_ambig else 0.8):
                return True
            # v3.4 scalp低仓位回补：USDT占多数+回调时允许略高于成本买入
            if self.scalp_mode and s.usdt_pct > 70 and pdiff > 0 and pdiff <= 1.2 and pullback >= 1.0:
                return True
            # 价格低于或等于成本：正常边际检查（含杠杆利息成本）
            if pdiff <= 0:
                edge = bps(max(0.0, (ref - s.last_price) / s.last_price))
                return edge >= (self.min_edge_bps + 2 * self.fee_bps + self.interest_bps_daily) * edge_mult
            return False
        else:
            actual_p = self._actual_profit_pct(s)
            # 趋势感知卖出门槛：上涨趋势让利润跑，下跌快速锁定
            if s.trend_score > 30:
                return actual_p >= 1.5  # 强上涨：利润1.5%以上才卖
            elif s.trend_score > 15:
                return actual_p >= 1.0  # 中等上涨：利润1.0%以上
            elif actual_p >= max(fee_pct, 0.5):
                return True  # 横盘/下跌：至少覆盖手续费+0.5%
            pdiff = self._pct_diff(s.last_price, ref)
            if is_strong_down and pdiff < 0:
                drop = abs(pdiff)
                if drop >= self.stop_loss_pct:
                    return bps(drop / 100) >= 2 * self.fee_bps
                return False
            edge = bps(max(0.0, (s.last_price - ref) / ref))
            return edge >= (self.min_edge_bps + 2 * self.fee_bps) * edge_mult

    # ── 批次买入检查 ──────────────────────────────────────────

    def _check_batch_buy(self, s: MarketState, pos: float,
                         total_bal: float) -> Optional[Dict[str, Any]]:
        if s.usdt_pct <= 60 or s.avg_sell_price <= 0:
            return None
        pm = self._profit_margin(s.avg_sell_price, s.last_price)
        min_req = 0.5 if self.scalp_mode else self.MIN_PROFIT_PCT
        if pm < min_req or s.last_price >= s.avg_sell_price or pos >= self.max_position_pct:
            return None

        try:
            from db import recent_trades
            recent = recent_trades(limit=20)
            last_batch = None
            for trade in recent:
                side = trade.get("side", "").upper()
                decision = trade.get("decision", "").upper()
                if (side == "BUY" or decision == "BUY") and "分批买入" in trade.get("reason", ""):
                    t = trade.get("ts_ms", 0)
                    if t > 0:
                        diff = self._time_since_min(t)
                        if self.BATCH_INTERVAL_MIN <= diff <= self.MAX_BATCH_HOURS * 60:
                            last_batch = trade
                            break

            if not last_batch:
                return None

            last_reason = last_batch.get("reason", "")
            last_price = float(last_batch.get("price", 0) or last_batch.get("last_price", 0) or 0)
            time_diff = self._time_since_min(last_batch.get("ts_ms", 0))

            # 批次号
            batch_map = {"第1批": 2, "第2批": 3, "第3批": 4, "第4批": 5}
            batch_num = None
            for k, v in batch_map.items():
                if k in last_reason:
                    batch_num = v; break
            if "第5批" in last_reason:
                return None
            if batch_num is None:
                batch_num = 2

            # 价格条件
            pc = self._pct_diff(s.last_price, last_price) if last_price > 0 else 0
            ok = False
            batch_why = ""
            if pc <= -0.5:
                ok, batch_why = True, f"价格继续下跌{abs(pc):.1f}%"
            elif abs(pc) < 0.5 and pm >= self.MIN_PROFIT_PCT:
                ok, batch_why = True, f"价格企稳，套利{pm:.1f}%"
            elif pc > 0 and s.last_price < s.avg_sell_price and pm >= self.MIN_PROFIT_PCT:
                ok, batch_why = True, f"价格反弹但仍低于均价"

            if not ok:
                return None

            usage = self._usdt_usage_pct(pm, s.usdt_pct)
            if total_bal > 0 and s.last_price > 0:
                buy_val = s.usdt_balance * (usage / 100)
                target = (buy_val / total_bal) * 100
                batch_pct = target * self.BATCH_RATIOS[min(batch_num - 1, len(self.BATCH_RATIOS) - 1)]
                pct_buy = min(batch_pct, self.max_position_pct - pos)
                if pct_buy < self.base_trade_pct * 0.3:
                    return None
                return {
                    "position_pct": pct_buy,
                    "reason": f"分批买入第{batch_num}批 | {batch_why}，套利{pm:.1f}%，距上次{time_diff:.0f}分钟"
                }
        except Exception as e:
            log.warning(f"批次买入检查失败: {e}")
        return None


# ── 风控门控（精简版）──────────────────────────────────────────

def should_trade_gate(
    side: str, last_price: float, cost_price: float, rsi14: float,
    sma12: float, sma24: float, vol_daily: float,
    cfg: Dict[str, Any], fee_bps_taker: float, min_edge_bps: float,
    position_pct: float = 50.0, avg_sell_price: float = 0.0,
    usdt_pct: float = 0.0, is_stop_loss: bool = False,
    regime: str = "SIDEWAYS"
) -> Tuple[bool, str]:
    """风控门控 - 精简版：只保留 analyze() 中未检查的项目"""
    scalp_mode = bool(cfg.get("scalp_mode", False))
    max_vol = float(cfg.get("max_daily_vol", 0.08))
    sma_filter = bool(cfg.get("sma_filter", True))

    # 价格有效性
    if last_price <= 0 or math.isnan(last_price):
        return False, f"无效价格({last_price})"
    if math.isnan(cost_price):
        return False, "成本价NaN"

    # 日波动率
    vol_th = max_vol * (1.5 if scalp_mode else 1.0)
    if vol_daily and vol_daily > vol_th:
        return False, f"波动率过高 {pct(vol_daily):.1f}%"

    # SMA过滤（安全网）- 自适应：牛市放宽容忍度
    if regime == REGIME_BULL:
        sma_tol = 1.08 if side == "Buy" else 0.92
    elif regime == REGIME_BEAR:
        sma_tol = 1.00 if side == "Buy" else 0.98
    else:
        sma_tol = (1.05 if scalp_mode else 1.02) if side == "Buy" else (0.95 if scalp_mode else 0.98)
    if sma_filter and not math.isnan(sma24):
        if side == "Buy" and last_price > sma24 * sma_tol:
            return False, "价格高于均线过多"
        # 卖出时：重仓盈利跳过SMA检查
        ref = avg_sell_price if (usdt_pct > 60 and avg_sell_price > 0) else cost_price
        pdiff = (last_price - ref) / ref * 100 if ref > 0 else 0
        is_heavy_profit = position_pct > 60 and pdiff > 0.5
        if side == "Sell" and not is_heavy_profit and not is_stop_loss:
            if last_price < sma24 * sma_tol:
                return False, "价格低于均线过多"

    return True, f"通过风控门槛，允许{'买入' if side == 'Buy' else '卖出'}"


def create_smart_strategy(cfg: Dict[str, Any]) -> SmartStrategy:
    """创建智能策略实例"""
    strategy_cfg = cfg.get("strategy", {})
    fees_cfg = cfg.get("fees", {})
    config = {
        "rsi_oversold": strategy_cfg.get("rsi_oversold", 25),
        "rsi_overbought": strategy_cfg.get("rsi_overbought", 75),
        "min_edge_bps": strategy_cfg.get("min_edge_bps", 40),
        "fee_bps": fees_cfg.get("spot_taker_bps", 10),
        "grid_enabled": strategy_cfg.get("grid_enabled", True),
        "grid_spacing_pct": strategy_cfg.get("grid_spacing_pct", 0.6),
        "trend_threshold": strategy_cfg.get("trend_threshold", 30),
        "stop_loss_pct": strategy_cfg.get("stop_loss_pct", 2.5),
        "trailing_stop_pct": strategy_cfg.get("trailing_stop_pct", 2.0),
        "max_position_pct": strategy_cfg.get("max_position_pct", 90),
        "min_position_pct": strategy_cfg.get("min_position_pct", 10),
        "base_trade_pct": strategy_cfg.get("base_trade_pct", 8),
        "max_atr_pct": strategy_cfg.get("max_atr_pct", 5.0),
        "min_confirmation": strategy_cfg.get("min_confirmation", 2),
        "scalp_mode": strategy_cfg.get("scalp_mode", False),
        # 杠杆参数
        "leverage": cfg.get("risk", {}).get("leverage", 1.0),
        # BTC趋势参数
        "btc_trend_enabled": strategy_cfg.get("btc_trend_enabled", False),
        "btc_trend_weight": strategy_cfg.get("btc_trend_weight", 0.25),
        "btc_trend_threshold": strategy_cfg.get("btc_trend_threshold", 20),
        # 自适应市场状态
        "regime_detection_enabled": strategy_cfg.get("regime_detection_enabled", True),
        # v4.2 下跌熔断器开关
        "downtrend_breaker_enabled": strategy_cfg.get("downtrend_breaker_enabled", True),
    }
    return SmartStrategy(config)
