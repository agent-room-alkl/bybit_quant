#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SOL/USDT 信号分析工具
分析1年15分钟K线数据，找出大涨/大跌/反转前的关键指标信号
"""
import math, time, sys
from typing import List, Dict, Any, Optional
import requests
from collections import defaultdict

# ── 配置 ──────────────────────────────────────────────────────
SYMBOL = "SOLUSDT"
INTERVAL = "15"
DAYS = 365

# ── 数据获取 ──────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, days: int) -> List[Dict]:
    """从Bybit获取历史K线数据"""
    base_url = "https://api.bybit.com/v5/market/kline"
    candles_needed = days * 24 * (60 // int(interval))
    end_ms = int(time.time() * 1000)
    all_candles = []

    print(f"  正在获取 {symbol} {interval}m K线 (需要 {candles_needed} 根)...")

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
            print(f"  请求错误: {e}")
            time.sleep(2)
            continue

        if data.get("retCode") != 0:
            print(f"  API错误: {data}")
            break

        raw = data["result"]["list"]
        if not raw:
            break

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

        if requests_made % 10 == 0:
            print(f"    ... 已获取 {len(all_candles)} 根K线")

        if len(raw) < 200:
            break

        time.sleep(0.15)

    all_candles.sort(key=lambda x: x["ts"])
    cutoff = end_ms - days * 24 * 3600 * 1000
    all_candles = [c for c in all_candles if c["ts"] >= cutoff]

    print(f"  完成: {symbol} 获取 {len(all_candles)} 根K线")
    return all_candles


# ── 指标计算 ──────────────────────────────────────────────────
def sma(values, period):
    result = [float('nan')] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1:i + 1]) / period
    return result

def ema(values, period):
    result = [float('nan')] * len(values)
    k = 2.0 / (period + 1)
    for i, v in enumerate(values):
        if math.isnan(v):
            continue
        if i == 0 or math.isnan(result[i-1]):
            result[i] = v
        else:
            result[i] = v * k + result[i-1] * (1 - k)
    return result

def compute_rsi(closes, period=14):
    rsi = [float('nan')] * len(closes)
    if len(closes) < period + 1:
        return rsi
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(closes)):
        if avg_loss == 0:
            rsi[i] = 100
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100 - 100 / (1 + rs)
        if i < len(gains):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return rsi

def compute_bollinger(closes, period=20, std_mult=2):
    upper = [float('nan')] * len(closes)
    lower = [float('nan')] * len(closes)
    mid = sma(closes, period)
    pct_b = [float('nan')] * len(closes)

    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        std = (sum((x - m) ** 2 for x in window) / period) ** 0.5
        upper[i] = m + std_mult * std
        lower[i] = m - std_mult * std
        band_width = upper[i] - lower[i]
        if band_width > 0:
            pct_b[i] = (closes[i] - lower[i]) / band_width
        else:
            pct_b[i] = 0.5
    return upper, lower, mid, pct_b

def compute_atr(candles, period=14):
    atr = [float('nan')] * len(candles)
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]['high']
        l = candles[i]['low']
        pc = candles[i-1]['close']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    if len(trs) < period:
        return atr

    atr_val = sum(trs[:period]) / period
    atr[period] = atr_val
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        atr[i + 1] = atr_val
    return atr

def compute_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [float('nan')] * len(closes)
    for i in range(len(closes)):
        if not math.isnan(ema_fast[i]) and not math.isnan(ema_slow[i]):
            macd_line[i] = ema_fast[i] - ema_slow[i]

    valid_macd = [v for v in macd_line if not math.isnan(v)]
    signal_line = ema(macd_line, signal)
    histogram = [float('nan')] * len(closes)
    for i in range(len(closes)):
        if not math.isnan(macd_line[i]) and not math.isnan(signal_line[i]):
            histogram[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, histogram


# ── 聚合到1H K线 ──────────────────────────────────────────────
def aggregate_to_1h(candles_15m):
    """将15分钟K线聚合为1小时K线"""
    hourly = []
    bucket = []
    for c in candles_15m:
        hour_ts = (c['ts'] // 3600000) * 3600000
        if bucket and (bucket[0]['ts'] // 3600000) * 3600000 != hour_ts:
            hourly.append({
                'ts': (bucket[0]['ts'] // 3600000) * 3600000,
                'open': bucket[0]['open'],
                'high': max(b['high'] for b in bucket),
                'low': min(b['low'] for b in bucket),
                'close': bucket[-1]['close'],
                'volume': sum(b['volume'] for b in bucket),
            })
            bucket = []
        bucket.append(c)
    if bucket:
        hourly.append({
            'ts': (bucket[0]['ts'] // 3600000) * 3600000,
            'open': bucket[0]['open'],
            'high': max(b['high'] for b in bucket),
            'low': min(b['low'] for b in bucket),
            'close': bucket[-1]['close'],
            'volume': sum(b['volume'] for b in bucket),
        })
    return hourly


# ── 计算所有指标 ──────────────────────────────────────────────
def compute_all_indicators(candles_1h):
    """在1H级别计算所有指标"""
    closes = [c['close'] for c in candles_1h]
    volumes = [c['volume'] for c in candles_1h]
    n = len(closes)

    rsi14 = compute_rsi(closes, 14)
    sma7 = sma(closes, 7)
    sma24 = sma(closes, 24)
    sma72 = sma(closes, 72)
    bb_upper, bb_lower, bb_mid, bb_pctb = compute_bollinger(closes, 20, 2)
    atr14 = compute_atr(candles_1h, 14)
    macd_line, macd_signal, macd_hist = compute_macd(closes)

    # 24h平均成交量
    vol_avg24 = sma(volumes, 24)

    # 趋势得分: 基于多周期均线
    sma14 = sma(closes, 14)
    trend_score = [float('nan')] * n
    for i in range(n):
        if any(math.isnan(x) for x in [sma7[i], sma14[i], sma24[i], sma72[i]]):
            continue
        score = 0
        if closes[i] > sma7[i]: score += 25
        if closes[i] > sma14[i]: score += 25
        if closes[i] > sma24[i]: score += 25
        if closes[i] > sma72[i]: score += 25
        trend_score[i] = score

    indicators = []
    for i in range(n):
        vol_ratio = volumes[i] / vol_avg24[i] if (not math.isnan(vol_avg24[i]) and vol_avg24[i] > 0) else float('nan')
        atr_pct = (atr14[i] / closes[i] * 100) if (not math.isnan(atr14[i]) and closes[i] > 0) else float('nan')

        # SMA7 vs SMA24 交叉
        golden_cross = False
        death_cross = False
        if i > 0 and not any(math.isnan(x) for x in [sma7[i], sma24[i], sma7[i-1], sma24[i-1]]):
            if sma7[i] > sma24[i] and sma7[i-1] <= sma24[i-1]:
                golden_cross = True
            if sma7[i] < sma24[i] and sma7[i-1] >= sma24[i-1]:
                death_cross = True

        price_vs_sma72 = "above" if (not math.isnan(sma72[i]) and closes[i] > sma72[i]) else ("below" if not math.isnan(sma72[i]) else "nan")

        indicators.append({
            'ts': candles_1h[i]['ts'],
            'close': closes[i],
            'rsi14': rsi14[i],
            'sma7': sma7[i],
            'sma24': sma24[i],
            'sma72': sma72[i],
            'golden_cross': golden_cross,
            'death_cross': death_cross,
            'price_vs_sma72': price_vs_sma72,
            'trend_score': trend_score[i],
            'vol_ratio': vol_ratio,
            'bb_pctb': bb_pctb[i],
            'atr_pct': atr_pct,
            'macd_hist': macd_hist[i],
            'volume': volumes[i],
        })
    return indicators


# ── 识别重大价格走势 ──────────────────────────────────────────
def find_moves(candles_1h):
    """识别所有重大价格走势"""
    closes = [c['close'] for c in candles_1h]
    n = len(closes)
    moves = []

    # 7天 = 168小时
    window_7d = 168
    window_14d = 336

    # 找涨跌幅
    for i in range(n):
        # 向前看7天
        max_future = closes[i]
        min_future = closes[i]
        max_j = min(i + window_7d, n)

        for j in range(i + 1, max_j):
            max_future = max(max_future, closes[j])
            min_future = min(min_future, closes[j])

        rally_pct = (max_future - closes[i]) / closes[i] * 100
        crash_pct = (min_future - closes[i]) / closes[i] * 100

        if rally_pct >= 10:
            moves.append({
                'type': '大涨(+10%+)',
                'start_idx': i,
                'start_ts': candles_1h[i]['ts'],
                'start_price': closes[i],
                'magnitude': rally_pct,
            })
        elif rally_pct >= 5:
            moves.append({
                'type': '中涨(+5~10%)',
                'start_idx': i,
                'start_ts': candles_1h[i]['ts'],
                'start_price': closes[i],
                'magnitude': rally_pct,
            })

        if crash_pct <= -10:
            moves.append({
                'type': '大跌(-10%+)',
                'start_idx': i,
                'start_ts': candles_1h[i]['ts'],
                'start_price': closes[i],
                'magnitude': crash_pct,
            })
        elif crash_pct <= -5:
            moves.append({
                'type': '中跌(-5~10%)',
                'start_idx': i,
                'start_ts': candles_1h[i]['ts'],
                'start_price': closes[i],
                'magnitude': crash_pct,
            })

    # V底反转: 先跌5%再涨5%（14天内）
    for i in range(n):
        max_j = min(i + window_14d, n)
        # 找到最低点
        min_price = closes[i]
        min_idx = i
        for j in range(i + 1, max_j):
            if closes[j] < min_price:
                min_price = closes[j]
                min_idx = j

        drop_pct = (min_price - closes[i]) / closes[i] * 100
        if drop_pct <= -5 and min_idx < max_j - 1:
            # 从最低点看反弹
            max_after = min_price
            for j in range(min_idx + 1, max_j):
                max_after = max(max_after, closes[j])
            bounce_pct = (max_after - min_price) / min_price * 100
            if bounce_pct >= 5:
                moves.append({
                    'type': 'V底反转',
                    'start_idx': i,
                    'start_ts': candles_1h[i]['ts'],
                    'start_price': closes[i],
                    'magnitude': bounce_pct,
                })

    # 倒V反转: 先涨5%再跌5%（14天内）
    for i in range(n):
        max_j = min(i + window_14d, n)
        max_price = closes[i]
        max_idx = i
        for j in range(i + 1, max_j):
            if closes[j] > max_price:
                max_price = closes[j]
                max_idx = j

        rise_pct = (max_price - closes[i]) / closes[i] * 100
        if rise_pct >= 5 and max_idx < max_j - 1:
            min_after = max_price
            for j in range(max_idx + 1, max_j):
                min_after = min(min_after, closes[j])
            drop_pct = (min_after - max_price) / max_price * 100
            if drop_pct <= -5:
                moves.append({
                    'type': '倒V反转',
                    'start_idx': i,
                    'start_ts': candles_1h[i]['ts'],
                    'start_price': closes[i],
                    'magnitude': drop_pct,
                })

    # 去重: 同类型在24小时内只保留幅度最大的
    moves.sort(key=lambda x: (x['type'], x['start_ts']))
    filtered = []
    for m in moves:
        duplicate = False
        for f in filtered:
            if f['type'] == m['type'] and abs(f['start_ts'] - m['start_ts']) < 24 * 3600 * 1000:
                if abs(m['magnitude']) > abs(f['magnitude']):
                    filtered.remove(f)
                    filtered.append(m)
                duplicate = True
                break
        if not duplicate:
            filtered.append(m)

    return filtered


# ── 分析移动前的指标 ──────────────────────────────────────────
def analyze_pre_move_indicators(moves, indicators):
    """分析每次移动前1-24小时的指标状态"""
    results = defaultdict(list)

    for m in moves:
        idx = m['start_idx']
        # 看前1-24小时的指标(1H K线，所以是前1-24根)
        pre_range = range(max(0, idx - 24), idx)
        if not pre_range:
            continue

        # 收集前24小时的指标
        pre_indicators = [indicators[j] for j in pre_range if j < len(indicators)]
        if not pre_indicators:
            continue

        # 取最近的有效指标值
        latest = pre_indicators[-1]

        # RSI分区
        rsi = latest['rsi14']
        if math.isnan(rsi):
            rsi_zone = 'nan'
        elif rsi < 25:
            rsi_zone = '极度超卖(<25)'
        elif rsi < 35:
            rsi_zone = '超卖(25-35)'
        elif rsi < 45:
            rsi_zone = '偏弱(35-45)'
        elif rsi < 55:
            rsi_zone = '中性(45-55)'
        elif rsi < 65:
            rsi_zone = '偏强(55-65)'
        elif rsi < 75:
            rsi_zone = '超买(65-75)'
        else:
            rsi_zone = '极度超买(>75)'

        # 是否有金叉/死叉
        has_golden = any(ind['golden_cross'] for ind in pre_indicators)
        has_death = any(ind['death_cross'] for ind in pre_indicators)

        # 价格vs SMA72
        price_vs_72 = latest['price_vs_sma72']

        # 趋势得分
        ts_val = latest['trend_score']
        if math.isnan(ts_val):
            trend_zone = 'nan'
        elif ts_val <= 0:
            trend_zone = '极弱(0)'
        elif ts_val <= 25:
            trend_zone = '弱(25)'
        elif ts_val <= 50:
            trend_zone = '中性(50)'
        elif ts_val <= 75:
            trend_zone = '强(75)'
        else:
            trend_zone = '极强(100)'

        # 成交量倍数
        vol_r = latest['vol_ratio']
        if math.isnan(vol_r):
            vol_zone = 'nan'
        elif vol_r < 0.5:
            vol_zone = '极低(<0.5x)'
        elif vol_r < 0.8:
            vol_zone = '偏低(0.5-0.8x)'
        elif vol_r < 1.2:
            vol_zone = '正常(0.8-1.2x)'
        elif vol_r < 2.0:
            vol_zone = '偏高(1.2-2x)'
        else:
            vol_zone = '放量(>2x)'

        # BB位置
        bb = latest['bb_pctb']
        if math.isnan(bb):
            bb_zone = 'nan'
        elif bb < 0:
            bb_zone = '跌破下轨(<0)'
        elif bb < 0.2:
            bb_zone = '下轨附近(0-0.2)'
        elif bb < 0.4:
            bb_zone = '偏低(0.2-0.4)'
        elif bb < 0.6:
            bb_zone = '中轨(0.4-0.6)'
        elif bb < 0.8:
            bb_zone = '偏高(0.6-0.8)'
        elif bb <= 1.0:
            bb_zone = '上轨附近(0.8-1.0)'
        else:
            bb_zone = '突破上轨(>1)'

        # ATR
        atr_val = latest['atr_pct']
        if math.isnan(atr_val):
            atr_zone = 'nan'
        elif atr_val < 1.5:
            atr_zone = '低波动(<1.5%)'
        elif atr_val < 3:
            atr_zone = '正常(1.5-3%)'
        elif atr_val < 5:
            atr_zone = '高波动(3-5%)'
        else:
            atr_zone = '极高波动(>5%)'

        # MACD
        macd_h = latest['macd_hist']
        if math.isnan(macd_h):
            macd_zone = 'nan'
        elif macd_h > 0:
            macd_zone = 'MACD多头'
        else:
            macd_zone = 'MACD空头'

        signal_data = {
            'rsi_zone': rsi_zone,
            'rsi_val': rsi,
            'golden_cross': has_golden,
            'death_cross': has_death,
            'price_vs_sma72': price_vs_72,
            'trend_zone': trend_zone,
            'trend_val': ts_val,
            'vol_zone': vol_zone,
            'vol_ratio': vol_r,
            'bb_zone': bb_zone,
            'bb_val': bb,
            'atr_zone': atr_zone,
            'atr_val': atr_val,
            'macd_zone': macd_zone,
            'magnitude': m['magnitude'],
        }

        results[m['type']].append(signal_data)

    return results


# ── 统计分析 ──────────────────────────────────────────────────
def find_patterns(results):
    """找出各类走势前的共同模式"""
    analysis = {}

    for move_type, signals in results.items():
        n = len(signals)
        if n < 3:
            continue

        # 统计各指标分区频率
        counters = defaultdict(lambda: defaultdict(int))

        for s in signals:
            for key in ['rsi_zone', 'price_vs_sma72', 'trend_zone', 'vol_zone', 'bb_zone', 'atr_zone', 'macd_zone']:
                val = s[key]
                if val != 'nan':
                    counters[key][val] += 1
            if s['golden_cross']:
                counters['交叉']['24H内金叉'] += 1
            if s['death_cross']:
                counters['交叉']['24H内死叉'] += 1

        # 计算百分比
        pct_data = {}
        for indicator, vals in counters.items():
            total = sum(vals.values())
            pct_data[indicator] = {k: (v, v/total*100) for k, v in sorted(vals.items(), key=lambda x: -x[1])}

        # 平均值
        valid_rsi = [s['rsi_val'] for s in signals if not math.isnan(s['rsi_val'])]
        valid_trend = [s['trend_val'] for s in signals if not math.isnan(s['trend_val'])]
        valid_vol = [s['vol_ratio'] for s in signals if not math.isnan(s['vol_ratio'])]
        valid_bb = [s['bb_val'] for s in signals if not math.isnan(s['bb_val'])]
        valid_atr = [s['atr_val'] for s in signals if not math.isnan(s['atr_val'])]

        avg_data = {
            'RSI14均值': sum(valid_rsi)/len(valid_rsi) if valid_rsi else float('nan'),
            '趋势得分均值': sum(valid_trend)/len(valid_trend) if valid_trend else float('nan'),
            '成交量比均值': sum(valid_vol)/len(valid_vol) if valid_vol else float('nan'),
            'BB%B均值': sum(valid_bb)/len(valid_bb) if valid_bb else float('nan'),
            'ATR%均值': sum(valid_atr)/len(valid_atr) if valid_atr else float('nan'),
        }

        analysis[move_type] = {
            'count': n,
            'pct_data': pct_data,
            'avg_data': avg_data,
        }

    return analysis


# ── 找最佳单一指标 ──────────────────────────────────────────
def find_best_single_indicator(results):
    """评估每个指标的预测能力"""
    # 将涨/跌分为正面/负面
    positive_types = ['大涨(+10%+)', '中涨(+5~10%)', 'V底反转']
    negative_types = ['大跌(-10%+)', '中跌(-5~10%)', '倒V反转']

    all_positive = []
    all_negative = []
    for t in positive_types:
        all_positive.extend(results.get(t, []))
    for t in negative_types:
        all_negative.extend(results.get(t, []))

    if not all_positive or not all_negative:
        return {}

    # 对每个指标分区，计算它出现在正面vs负面中的区分度
    indicators_to_check = ['rsi_zone', 'trend_zone', 'vol_zone', 'bb_zone', 'atr_zone', 'macd_zone', 'price_vs_sma72']

    scores = {}
    for ind in indicators_to_check:
        pos_dist = defaultdict(int)
        neg_dist = defaultdict(int)
        for s in all_positive:
            if s[ind] != 'nan':
                pos_dist[s[ind]] += 1
        for s in all_negative:
            if s[ind] != 'nan':
                neg_dist[s[ind]] += 1

        pos_total = sum(pos_dist.values())
        neg_total = sum(neg_dist.values())

        if pos_total == 0 or neg_total == 0:
            continue

        # 计算区分度 (KL散度的简化版)
        all_zones = set(list(pos_dist.keys()) + list(neg_dist.keys()))
        divergence = 0
        for zone in all_zones:
            p = pos_dist.get(zone, 0) / pos_total
            q = neg_dist.get(zone, 0) / neg_total
            if p > 0 and q > 0:
                divergence += abs(p - q)
            elif p > 0 or q > 0:
                divergence += max(p, q)

        scores[ind] = divergence

    return scores


# ── 主程序 ──────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("  SOL/USDT 信号分析 - 1年15分钟K线数据")
    print("=" * 70)

    # 1. 获取数据
    print("\n[1/5] 获取K线数据...")
    candles_15m = fetch_klines(SYMBOL, INTERVAL, DAYS)
    if len(candles_15m) < 1000:
        print("数据不足，退出")
        return

    # 2. 聚合到1H
    print("\n[2/5] 聚合到1小时K线并计算指标...")
    candles_1h = aggregate_to_1h(candles_15m)
    print(f"  1H K线数量: {len(candles_1h)}")

    # 计算指标
    indicators = compute_all_indicators(candles_1h)
    print(f"  指标计算完成")

    # 3. 识别重大走势
    print("\n[3/5] 识别重大价格走势...")
    moves = find_moves(candles_1h)

    move_counts = defaultdict(int)
    for m in moves:
        move_counts[m['type']] += 1

    print(f"\n  走势统计:")
    for t, c in sorted(move_counts.items()):
        print(f"    {t}: {c} 次")

    # 4. 分析走势前的指标
    print("\n[4/5] 分析走势前的指标信号...")
    results = analyze_pre_move_indicators(moves, indicators)
    analysis = find_patterns(results)

    # 5. 输出结果
    print("\n[5/5] 生成分析报告...")

    print("\n")
    print("=" * 70)
    print("  信号分析报告 - SOL/USDT 1年数据")
    print("=" * 70)

    # 时间范围
    from datetime import datetime
    start_dt = datetime.fromtimestamp(candles_15m[0]['ts'] / 1000)
    end_dt = datetime.fromtimestamp(candles_15m[-1]['ts'] / 1000)
    print(f"\n  数据范围: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}")
    print(f"  15分钟K线: {len(candles_15m)} 根 | 1小时K线: {len(candles_1h)} 根")

    # 每种走势的详细分析
    move_order = ['大涨(+10%+)', '中涨(+5~10%)', '大跌(-10%+)', '中跌(-5~10%)', 'V底反转', '倒V反转']

    for move_type in move_order:
        if move_type not in analysis:
            continue
        a = analysis[move_type]

        print(f"\n{'─' * 70}")
        print(f"  【{move_type}】 共 {a['count']} 次")
        print(f"{'─' * 70}")

        # 平均指标值
        print(f"\n  ▸ 平均指标值:")
        for k, v in a['avg_data'].items():
            if not math.isnan(v):
                print(f"    {k}: {v:.1f}")

        # 各指标分布 - 只显示top 3
        print(f"\n  ▸ 指标分布 (Top频率):")
        indicator_names = {
            'rsi_zone': 'RSI14',
            'trend_zone': '趋势得分',
            'vol_zone': '成交量',
            'bb_zone': '布林带位置',
            'atr_zone': 'ATR波动率',
            'macd_zone': 'MACD方向',
            'price_vs_sma72': '价格vs SMA72',
            '交叉': 'SMA交叉',
        }

        for ind_key, ind_name in indicator_names.items():
            if ind_key in a['pct_data']:
                vals = a['pct_data'][ind_key]
                items = list(vals.items())[:3]
                parts = [f"{k}={v[0]}次({v[1]:.0f}%)" for k, v in items]
                print(f"    {ind_name}: {', '.join(parts)}")

    # ── 黄金规则和危险信号 ──────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  黄金规则 (涨前70%+出现的条件)")
    print(f"{'=' * 70}")

    for move_type in ['大涨(+10%+)', '中涨(+5~10%)', 'V底反转']:
        if move_type not in analysis:
            continue
        a = analysis[move_type]
        print(f"\n  【{move_type}】:")
        golden_rules = []
        for ind_key, ind_name in indicator_names.items():
            if ind_key in a['pct_data']:
                for val, (count, pct) in a['pct_data'][ind_key].items():
                    if pct >= 70:
                        golden_rules.append(f"    ★ {ind_name} = {val} (出现率 {pct:.0f}%)")
        if golden_rules:
            for r in golden_rules:
                print(r)
        else:
            # 放宽到60%
            for ind_key, ind_name in indicator_names.items():
                if ind_key in a['pct_data']:
                    for val, (count, pct) in a['pct_data'][ind_key].items():
                        if pct >= 50:
                            golden_rules.append(f"    ☆ {ind_name} = {val} (出现率 {pct:.0f}%)")
            if golden_rules:
                print("    (无70%+条件，显示50%+条件)")
                for r in golden_rules:
                    print(r)

    print(f"\n{'=' * 70}")
    print(f"  危险信号 (跌前70%+出现的条件)")
    print(f"{'=' * 70}")

    for move_type in ['大跌(-10%+)', '中跌(-5~10%)', '倒V反转']:
        if move_type not in analysis:
            continue
        a = analysis[move_type]
        print(f"\n  【{move_type}】:")
        danger_signals = []
        for ind_key, ind_name in indicator_names.items():
            if ind_key in a['pct_data']:
                for val, (count, pct) in a['pct_data'][ind_key].items():
                    if pct >= 70:
                        danger_signals.append(f"    ⚠ {ind_name} = {val} (出现率 {pct:.0f}%)")
        if danger_signals:
            for s in danger_signals:
                print(s)
        else:
            for ind_key, ind_name in indicator_names.items():
                if ind_key in a['pct_data']:
                    for val, (count, pct) in a['pct_data'][ind_key].items():
                        if pct >= 50:
                            danger_signals.append(f"    △ {ind_name} = {val} (出现率 {pct:.0f}%)")
            if danger_signals:
                print("    (无70%+条件，显示50%+条件)")
                for s in danger_signals:
                    print(s)

    # ── 最佳单一指标 ──────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  最佳单一预测指标 (涨 vs 跌 区分度)")
    print(f"{'=' * 70}")

    scores = find_best_single_indicator(results)
    if scores:
        sorted_scores = sorted(scores.items(), key=lambda x: -x[1])
        ind_cn_names = {
            'rsi_zone': 'RSI14',
            'trend_zone': '趋势得分',
            'vol_zone': '成交量比',
            'bb_zone': '布林带位置',
            'atr_zone': 'ATR波动率',
            'macd_zone': 'MACD方向',
            'price_vs_sma72': '价格vs SMA72',
        }
        print()
        for i, (ind, score) in enumerate(sorted_scores):
            name = ind_cn_names.get(ind, ind)
            bar = '█' * int(score * 20)
            rank = '🥇' if i == 0 else ('🥈' if i == 1 else ('🥉' if i == 2 else '  '))
            print(f"  {rank} {name:15s}  区分度: {score:.3f}  {bar}")

    # ── 涨前 vs 跌前的指标对比 ──────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  涨前 vs 跌前 指标均值对比")
    print(f"{'=' * 70}")

    positive_types_list = ['大涨(+10%+)', '中涨(+5~10%)']
    negative_types_list = ['大跌(-10%+)', '中跌(-5~10%)']

    pos_all = []
    neg_all = []
    for t in positive_types_list:
        pos_all.extend(results.get(t, []))
    for t in negative_types_list:
        neg_all.extend(results.get(t, []))

    if pos_all and neg_all:
        metrics = [
            ('RSI14', 'rsi_val'),
            ('趋势得分', 'trend_val'),
            ('成交量比', 'vol_ratio'),
            ('BB %B', 'bb_val'),
            ('ATR%', 'atr_val'),
        ]

        print(f"\n  {'指标':15s}  {'涨前均值':>10s}  {'跌前均值':>10s}  {'差异':>8s}")
        print(f"  {'─'*50}")

        for name, key in metrics:
            pos_vals = [s[key] for s in pos_all if not math.isnan(s[key])]
            neg_vals = [s[key] for s in neg_all if not math.isnan(s[key])]
            if pos_vals and neg_vals:
                pos_avg = sum(pos_vals) / len(pos_vals)
                neg_avg = sum(neg_vals) / len(neg_vals)
                diff = pos_avg - neg_avg
                print(f"  {name:15s}  {pos_avg:10.2f}  {neg_avg:10.2f}  {diff:+8.2f}")

    # ── 实战建议 ──────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  实战总结与建议")
    print(f"{'=' * 70}")

    # 基于分析生成建议
    if pos_all and neg_all:
        pos_rsi = [s['rsi_val'] for s in pos_all if not math.isnan(s['rsi_val'])]
        neg_rsi = [s['rsi_val'] for s in neg_all if not math.isnan(s['rsi_val'])]
        pos_trend = [s['trend_val'] for s in pos_all if not math.isnan(s['trend_val'])]
        neg_trend = [s['trend_val'] for s in neg_all if not math.isnan(s['trend_val'])]
        pos_bb = [s['bb_val'] for s in pos_all if not math.isnan(s['bb_val'])]
        neg_bb = [s['bb_val'] for s in neg_all if not math.isnan(s['bb_val'])]

        avg_pos_rsi = sum(pos_rsi)/len(pos_rsi) if pos_rsi else 50
        avg_neg_rsi = sum(neg_rsi)/len(neg_rsi) if neg_rsi else 50
        avg_pos_trend = sum(pos_trend)/len(pos_trend) if pos_trend else 50
        avg_neg_trend = sum(neg_trend)/len(neg_trend) if neg_trend else 50
        avg_pos_bb = sum(pos_bb)/len(pos_bb) if pos_bb else 0.5
        avg_neg_bb = sum(neg_bb)/len(neg_bb) if neg_bb else 0.5

        print(f"""
  基于 {len(candles_1h)} 小时K线分析的关键发现:

  1. RSI信号:
     - 涨前平均RSI: {avg_pos_rsi:.1f} | 跌前平均RSI: {avg_neg_rsi:.1f}
     - {'RSI低位是买入良机' if avg_pos_rsi < avg_neg_rsi else 'RSI高位反而容易继续涨'}

  2. 趋势得分:
     - 涨前平均: {avg_pos_trend:.0f} | 跌前平均: {avg_neg_trend:.0f}
     - {'弱趋势时买入收益更高(逆势策略)' if avg_pos_trend < avg_neg_trend else '强趋势时买入收益更高(顺势策略)'}

  3. 布林带位置:
     - 涨前平均BB%B: {avg_pos_bb:.2f} | 跌前平均: {avg_neg_bb:.2f}
     - {'价格偏低时买入更安全' if avg_pos_bb < avg_neg_bb else '价格偏高时反而继续涨'}
""")

    print("  分析完成!")
    print("=" * 70)


if __name__ == "__main__":
    main()
