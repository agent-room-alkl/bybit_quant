# -*- coding: utf-8 -*-
"""
增强版技术指标模块
包含：SMA, EMA, RSI, MACD, 布林带, ATR, 成交量分析等
"""
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np

def klines_to_df(klines_json: Dict[str, Any]) -> pd.DataFrame:
    """将Bybit K线数据转换为DataFrame"""
    data = klines_json.get("result", {}).get("list", []) or []
    cols = ["startTime", "open", "high", "low", "close", "volume", "turnover"]
    df = pd.DataFrame(data, columns=cols)
    if df.empty:
        return df
    df["startTime"] = pd.to_datetime(df["startTime"].astype(np.int64), unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume", "turnover"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("startTime").reset_index(drop=True)
    return df

def sma(series: pd.Series, window: int) -> pd.Series:
    """简单移动平均线"""
    return series.rolling(window=window, min_periods=1).mean()

def ema(series: pd.Series, span: int) -> pd.Series:
    """指数移动平均线"""
    return series.ewm(span=span, adjust=False, min_periods=1).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """相对强弱指数 (0-100)"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=period, min_periods=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD指标
    返回: (MACD线, Signal线, Histogram柱状图)
    """
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def bollinger_bands(series: pd.Series, window: int = 20, num_std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    布林带
    返回: (上轨, 中轨, 下轨)
    """
    middle = sma(series, window)
    std = series.rolling(window=window, min_periods=1).std()
    upper = middle + (std * num_std)
    lower = middle - (std * num_std)
    return upper, middle, lower

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """平均真实波幅 - 用于动态止损"""
    high_low = high - low
    high_close = (high - close.shift()).abs()
    low_close = (low - close.shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=1).mean()

def stochastic_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, k_smooth: int = 3) -> pd.Series:
    """随机RSI - 更敏感的超买超卖指标"""
    rsi_values = rsi(series, rsi_period)
    stoch_rsi = (rsi_values - rsi_values.rolling(stoch_period).min()) / \
                (rsi_values.rolling(stoch_period).max() - rsi_values.rolling(stoch_period).min())
    return stoch_rsi.rolling(k_smooth).mean() * 100

def volume_sma(volume: pd.Series, window: int = 20) -> pd.Series:
    """成交量移动平均"""
    return sma(volume, window)

def price_momentum(series: pd.Series, period: int = 10) -> pd.Series:
    """价格动量 - 当前价格与N周期前的变化率"""
    return (series / series.shift(period) - 1) * 100

def trend_strength(series: pd.Series, short: int = 7, medium: int = 25, long: int = 99) -> pd.Series:
    """
    趋势强度指标 (-100 到 +100)
    正值表示上涨趋势，负值表示下跌趋势，绝对值越大趋势越强
    """
    sma_short = sma(series, short)
    sma_medium = sma(series, medium)
    sma_long = sma(series, long)
    
    # 计算均线排列得分
    score = pd.Series(0.0, index=series.index)
    
    # 多头排列加分
    score = score + np.where(sma_short > sma_medium, 33, 0)
    score = score + np.where(sma_medium > sma_long, 33, 0)
    score = score + np.where(series > sma_short, 34, 0)
    
    # 空头排列减分
    score = score - np.where(sma_short < sma_medium, 33, 0)
    score = score - np.where(sma_medium < sma_long, 33, 0)
    score = score - np.where(series < sma_short, 34, 0)
    
    return score

def support_resistance_levels(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 20) -> Tuple[float, float]:
    """
    计算支撑位和阻力位
    返回: (支撑位, 阻力位)
    """
    recent_low = low.iloc[-lookback:].min()
    recent_high = high.iloc[-lookback:].max()
    return recent_low, recent_high

def volatility_percentile(series: pd.Series, window: int = 24, lookback: int = 100) -> float:
    """
    当前波动率在历史波动率中的百分位
    用于判断当前是高波动还是低波动环境
    """
    returns = series.pct_change()
    current_vol = returns.iloc[-window:].std()
    historical_vols = returns.rolling(window).std().iloc[-lookback:]
    if historical_vols.empty or current_vol != current_vol:
        return 50.0
    percentile = (historical_vols < current_vol).sum() / len(historical_vols) * 100
    return percentile

def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    为DataFrame添加所有技术指标
    """
    if df.empty:
        return df
    df = df.copy()
    
    # 基础均线
    df["SMA7"] = sma(df["close"], 7)
    df["SMA12"] = sma(df["close"], 12)
    df["SMA24"] = sma(df["close"], 24)
    df["SMA72"] = sma(df["close"], 72)
    
    # EMA
    df["EMA9"] = ema(df["close"], 9)
    df["EMA21"] = ema(df["close"], 21)
    
    # RSI
    df["RSI14"] = rsi(df["close"], 14)
    df["RSI7"] = rsi(df["close"], 7)  # 更敏感的RSI
    
    # MACD
    df["MACD"], df["MACD_Signal"], df["MACD_Hist"] = macd(df["close"])
    
    # 布林带
    df["BB_Upper"], df["BB_Middle"], df["BB_Lower"] = bollinger_bands(df["close"], 20, 2.0)
    df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Middle"] * 100  # 布林带宽度百分比
    df["BB_Position"] = (df["close"] - df["BB_Lower"]) / (df["BB_Upper"] - df["BB_Lower"])  # 价格在布林带中的位置 (0-1)
    
    # ATR
    df["ATR14"] = atr(df["high"], df["low"], df["close"], 14)
    df["ATR_Pct"] = df["ATR14"] / df["close"] * 100  # ATR百分比
    
    # 动量和趋势
    df["Momentum10"] = price_momentum(df["close"], 10)
    df["Trend"] = trend_strength(df["close"])
    
    # 成交量分析
    df["Volume_SMA20"] = volume_sma(df["volume"], 20)
    df["Volume_Ratio"] = df["volume"] / df["Volume_SMA20"]  # 成交量相对于均值的倍数
    
    # 波动率
    df["RET"] = df["close"].pct_change()
    df["VOL"] = df["RET"].rolling(24, min_periods=5).std() * (24 ** 0.5)  # 日化波动率
    
    # 随机RSI
    df["StochRSI"] = stochastic_rsi(df["close"])

    # v5.2: EMA(8) for trend crossover detection
    df["EMA8"] = ema(df["close"], 8)

    # v5.2: ADX (Average Directional Index) for trend strength confirmation
    df["ADX"] = _adx(df["high"], df["low"], df["close"], 14)

    return df


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ADX (Average Directional Index) — measures trend strength (0-100)."""
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr_s = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_s)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_s)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx

def get_market_condition(df: pd.DataFrame) -> Dict[str, Any]:
    """
    分析当前市场状态
    返回包含趋势、波动率、超买超卖等信息的字典
    """
    if df.empty or len(df) < 50:
        return {"condition": "unknown", "trend": 0, "volatility": "medium"}
    
    last = df.iloc[-1]
    
    # 趋势判断
    trend = last.get("Trend", 0)
    if trend > 50:
        trend_label = "strong_bull"
    elif trend > 20:
        trend_label = "bull"
    elif trend < -50:
        trend_label = "strong_bear"
    elif trend < -20:
        trend_label = "bear"
    else:
        trend_label = "sideways"
    
    # 波动率判断
    vol_pct = volatility_percentile(df["close"])
    if vol_pct > 75:
        vol_label = "high"
    elif vol_pct < 25:
        vol_label = "low"
    else:
        vol_label = "medium"
    
    # RSI状态
    rsi_val = last.get("RSI14", 50)
    if rsi_val > 70:
        rsi_state = "overbought"
    elif rsi_val < 30:
        rsi_state = "oversold"
    else:
        rsi_state = "neutral"
    
    # MACD状态
    macd_hist = last.get("MACD_Hist", 0)
    prev_hist = df.iloc[-2].get("MACD_Hist", 0) if len(df) > 1 else 0
    if macd_hist > 0 and macd_hist > prev_hist:
        macd_state = "bullish_momentum"
    elif macd_hist < 0 and macd_hist < prev_hist:
        macd_state = "bearish_momentum"
    elif macd_hist > 0:
        macd_state = "bullish_weakening"
    else:
        macd_state = "bearish_weakening"
    
    # 布林带位置
    bb_pos = last.get("BB_Position", 0.5)
    if bb_pos > 0.95:
        bb_state = "upper_band"
    elif bb_pos < 0.05:
        bb_state = "lower_band"
    else:
        bb_state = "middle"
    
    # 支撑阻力
    support, resistance = support_resistance_levels(df["high"], df["low"], df["close"])
    current_price = last["close"]
    distance_to_support = (current_price - support) / current_price * 100
    distance_to_resistance = (resistance - current_price) / current_price * 100
    
    return {
        "condition": trend_label,
        "trend_score": trend,
        "volatility": vol_label,
        "volatility_percentile": vol_pct,
        "rsi": rsi_val,
        "rsi_state": rsi_state,
        "macd_state": macd_state,
        "bb_position": bb_pos,
        "bb_state": bb_state,
        "support": support,
        "resistance": resistance,
        "dist_to_support_pct": distance_to_support,
        "dist_to_resistance_pct": distance_to_resistance,
        "atr_pct": last.get("ATR_Pct", 0),
        "volume_ratio": last.get("Volume_Ratio", 1.0)
    }
