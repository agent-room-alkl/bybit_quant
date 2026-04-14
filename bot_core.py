# -*- coding: utf-8 -*-
"""
智能量化交易核心模块 v2.0
整合新策略系统，支持网格交易+趋势跟踪+多指标确认
"""
from __future__ import annotations
import time, json, math, logging, datetime as dt, os
from typing import Dict, Any, List, Tuple, Optional
from dateutil import tz

from bybit_client import BybitClient
from trade_logic import (
    parse_instr_filters, suggest_action, 
    calculate_position, calculate_trade_qty, generate_order,
    RiskManager, PositionInfo
)
from indicators import klines_to_df, enrich_indicators, get_market_condition
from db import init_db, log_trade, log_signal, set_meta, get_meta
from cost import get_spot_avg_cost, get_spot_avg_cost_by_position, get_cost_price
from strategy_v5 import should_trade_gate, SmartStrategy, MarketState, create_smart_strategy
from news_sentiment import get_news_sentiment, get_cached_score

log = logging.getLogger("auto_bot")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DAY_MS = 24 * 3600 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def human_time(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms/1000.0).astimezone(tz.tzlocal()).strftime("%Y-%m-%d %H:%M:%S")


def _nz_today_str() -> str:
    """
    返回新西兰时区（Pacific/Auckland）的当前日期字符串：YYYYMMDD
    用于“每日次数 / 金额 / 盈亏”这类按天统计的键，确保以新西兰当地日期为准。
    """
    try:
        nz_tz = tz.gettz("Pacific/Auckland")
        now_nz = dt.datetime.now(tz=nz_tz)
    except Exception:
        # 兜底：如果时区获取失败，则退回本地时间
        now_nz = dt.datetime.now()
    return now_nz.strftime("%Y%m%d")


import threading as _threading

def _refresh_news_bg(api_key: str, model: str):
    """后台线程刷新新闻缓存"""
    try:
        get_news_sentiment(api_key, model)
    except Exception as e:
        log.warning(f"[NEWS] 后台刷新失败: {e}")

def _get_news_fields(cfg: dict) -> dict:
    """获取新闻情绪字段，永远返回缓存数据，不阻塞主循环"""
    try:
        api_key = cfg.get("gpt_api_key", "")
        model = cfg.get("gpt_model", "gpt-4o")
        if not api_key:
            return {}

        # 始终读缓存（不阻塞）
        sentiment = get_cached_sentiment()

        # 缓存过期时，后台线程刷新，不阻塞交易循环
        if sentiment.get("age_min", 999) > 30:
            _threading.Thread(target=_refresh_news_bg, args=(api_key, model), daemon=True).start()
            log.info("[NEWS] 缓存过期，后台刷新新闻情绪...")

        return {
            "news_sentiment": sentiment.get("score", 0),
            "news_confidence": sentiment.get("confidence", 0.0),
            "news_risk_level": sentiment.get("risk_level", "medium"),
            "news_action": sentiment.get("suggested_action", "hold"),
        }
    except Exception as e:
        log.warning(f"[NEWS] 获取新闻情绪失败: {e}")
        return {}


def _cooldown_ok(symbol: str, side: str, cooldown_min: int) -> Tuple[bool, str]:
    """检查冷却时间"""
    k = f"last_{side.lower()}_{symbol.upper()}"
    v = get_meta(k)
    if not v:
        return True, "首次交易"
    try:
        last_ms = int(v)
    except Exception:
        return True, "首次交易"
    mins = (_now_ms() - last_ms) / 60000.0
    if mins >= cooldown_min:
        return True, f"冷却完成 {mins:.1f}min >= {cooldown_min}min"
    return False, f"冷却中 {mins:.1f}min < {cooldown_min}min"


def _bump_side_counter(symbol: str, side: str):
    """增加交易计数"""
    key = f"cnt_{side.lower()}_{symbol.upper()}_{_nz_today_str()}"
    v = get_meta(key)
    n = (int(v) if v and v.isdigit() else 0) + 1
    set_meta(key, str(n))


def _check_daily_limits(symbol: str, side: str, max_orders_per_day: int) -> Tuple[bool, str]:
    """检查每日交易次数限制"""
    key = f"cnt_{side.lower()}_{symbol.upper()}_{_nz_today_str()}"
    v = get_meta(key)
    n = (int(v) if v and v.isdigit() else 0)
    if n >= max_orders_per_day:
        return False, f"当日该方向下单次数 {n} 已达上限 {max_orders_per_day}"
    return True, f"今日该方向已下单 {n} 次"


def _get_daily_volume(symbol: str) -> float:
    """获取今日净交易额（USDT）：买入为正，卖出为负，净额=买入-卖出"""
    buy_key = f"daily_vol_buy_{symbol.upper()}_{_nz_today_str()}"
    sell_key = f"daily_vol_sell_{symbol.upper()}_{_nz_today_str()}"
    buy_vol = float(get_meta(buy_key) or 0)
    sell_vol = float(get_meta(sell_key) or 0)
    # 净交易额 = 买入金额 - 卖出金额（正数表示净买入，负数表示净卖出）
    return buy_vol - sell_vol


def _get_daily_buy_volume(symbol: str) -> float:
    """获取今日买入总额（USDT）"""
    key = f"daily_vol_buy_{symbol.upper()}_{_nz_today_str()}"
    v = get_meta(key)
    try:
        return float(v) if v else 0.0
    except:
        return 0.0


def _get_daily_sell_volume(symbol: str) -> float:
    """获取今日卖出总额（USDT）"""
    key = f"daily_vol_sell_{symbol.upper()}_{_nz_today_str()}"
    v = get_meta(key)
    try:
        return float(v) if v else 0.0
    except:
        return 0.0


def _add_daily_volume(symbol: str, volume_usdt: float, side: str):
    """增加今日交易额（区分买入和卖出）"""
    if side.lower() == "buy":
        key = f"daily_vol_buy_{symbol.upper()}_{_nz_today_str()}"
        current = _get_daily_buy_volume(symbol)
        set_meta(key, str(current + volume_usdt))
    else:  # sell
        key = f"daily_vol_sell_{symbol.upper()}_{_nz_today_str()}"
        current = _get_daily_sell_volume(symbol)
        set_meta(key, str(current + volume_usdt))


def _check_daily_volume_limit(symbol: str, proposed_volume: float, max_daily_volume_pct: float, total_asset_value: float, side: str) -> Tuple[bool, str]:
    """
    检查每日交易额限制（按总资产百分比）
    v5.2e: 用总买入量限制，不用净值（防止卖出后重置额度）
    """
    if max_daily_volume_pct <= 0:
        return True, "无额度限制"
    if total_asset_value <= 0:
        return False, "总资产为0"

    max_daily_usdt = total_asset_value * (max_daily_volume_pct / 100.0)
    buy_volume = _get_daily_buy_volume(symbol)
    sell_volume = _get_daily_sell_volume(symbol)

    if side.lower() == "sell":
        return True, f"卖出不限额 | 今日买入${buy_volume:.0f} 卖出${sell_volume:.0f}"

    # 买入：用总买入量（不减卖出）检查限额
    new_buy_total = buy_volume + proposed_volume
    if new_buy_total > max_daily_usdt:
        remaining = max(0, max_daily_usdt - buy_volume)
        return False, f"今日买入${new_buy_total:.0f}将超限额${max_daily_usdt:.0f}({max_daily_volume_pct:.0f}%)，剩余${remaining:.0f}"

    return True, f"今日买入${buy_volume:.0f}/${max_daily_usdt:.0f}({max_daily_volume_pct:.0f}%) 卖出${sell_volume:.0f}"


def _get_daily_pnl(symbol: str) -> float:
    """获取今日盈亏百分比（从元数据）"""
    key = f"daily_pnl_{symbol.upper()}_{_nz_today_str()}"
    v = get_meta(key)
    try:
        return float(v) if v else 0.0
    except:
        return 0.0


def _update_daily_pnl(symbol: str, pnl_pct: float):
    """更新今日盈亏百分比"""
    key = f"daily_pnl_{symbol.upper()}_{_nz_today_str()}"
    current = _get_daily_pnl(symbol)
    set_meta(key, str(current + pnl_pct))


def _maybe_cached_cost(client: BybitClient, symbol: str, hist_days: int, max_cache_sec: int = 600) -> float:
    """获取成本价（带缓存）"""
    k = f"wac_{symbol.upper()}"
    ts_k = f"wac_ts_{symbol.upper()}"
    v = get_meta(k)
    ts = get_meta(ts_k)
    if v and ts:
        try:
            if _now_ms() - int(ts) < max_cache_sec * 1000:
                return float(v)
        except Exception:
            pass
    import math as _m
    wac = get_spot_avg_cost(client, symbol, hist_days)
    # v5.2e: NaN不缓存，下次重新计算
    if not _m.isnan(wac) and wac > 0:
        set_meta(k, f"{wac:.12f}")
        set_meta(ts_k, str(_now_ms()))
    return wac


def _get_balances(client: BybitClient, symbol: str) -> Tuple[float, float]:
    """获取账户余额"""
    base = symbol[:-4] if symbol.upper().endswith("USDT") else symbol[:-4]
    quote = "USDT"
    usdt_bal = 0.0
    base_bal = 0.0
    
    try:
        # 优先使用钱包余额API（获取总余额，包括可用和冻结）
        balance = client.get_wallet_balance(coins=f"{quote},{base}")
        
        # 检查API密钥是否过期
        if balance and balance.get("retCode") == 33004:
            log.error(f"API密钥已过期，请更新config.json中的api_key和api_secret")
            return base_bal, usdt_bal
        
        if balance and balance.get("retCode") == 0:
            result = balance.get("result", {})
            list_data = result.get("list", [])
            if list_data:
                account = list_data[0]
                coins = account.get("coin", [])
                for coin_info in coins:
                    coin = coin_info.get("coin", "")
                    if coin.upper() == quote.upper():
                        # 尝试多个字段：walletBalance, availableToWithdraw, free, locked
                        usdt_bal = float(coin_info.get("walletBalance", 0) or 0)
                        if usdt_bal == 0:
                            usdt_bal = float(coin_info.get("availableToWithdraw", 0) or 0)
                        if usdt_bal == 0:
                            usdt_bal = float(coin_info.get("free", 0) or 0)
                        if usdt_bal == 0:
                            usdt_bal = float(coin_info.get("availableToBorrow", 0) or 0)
                    elif coin.upper() == base.upper():
                        base_bal = float(coin_info.get("walletBalance", 0) or 0)
                        if base_bal == 0:
                            base_bal = float(coin_info.get("availableToWithdraw", 0) or 0)
                        if base_bal == 0:
                            base_bal = float(coin_info.get("free", 0) or 0)
                        if base_bal == 0:
                            base_bal = float(coin_info.get("availableToBorrow", 0) or 0)
        elif balance:
            # API调用失败，记录错误信息
            ret_code = balance.get("retCode", -1)
            ret_msg = balance.get("retMsg", "Unknown error")
            log.warning(f"获取钱包余额失败: retCode={ret_code}, retMsg={ret_msg}")
        
        # 如果仍然为0，记录调试信息
        if usdt_bal == 0.0 and base_bal == 0.0:
            log.debug(f"余额获取结果: wallet_balance API响应={balance}, transferable API响应={r if 'r' in locals() else 'N/A'}")
    except Exception as e:
        log.warning(f"获取余额失败: {e}", exc_info=True)
    
    return base_bal, usdt_bal


def _get_total_equity(client: BybitClient) -> Optional[float]:
    """获取账户总资产（USDT），使用API返回的totalEquity，确保与手机端一致"""
    try:
        # 获取所有币种余额（不传coins参数）
        balance = client.get_wallet_balance(coins=None)
        
        if balance and balance.get("retCode") == 0:
            result = balance.get("result", {})
            list_data = result.get("list", [])
            if list_data:
                account = list_data[0]
                total_equity = account.get("totalEquity")
                if total_equity is not None:
                    return float(total_equity)
    except Exception as e:
        log.warning(f"获取总资产失败: {e}", exc_info=True)
    
    return None


def one_step_for_symbol(client: BybitClient, cfg: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """
    对单个交易对执行一次完整的评估和交易循环
    
    使用新的智能策略系统：
    1. 获取市场数据和技术指标
    2. 分析市场状态
    3. 生成交易信号
    4. 风控检查
    5. 执行交易（如果通过所有检查）
    """
    db = init_db()
    now_ms = _now_ms()
    
    # 加载配置
    risk_cfg = cfg.get("risk", {})
    strategy_cfg = cfg.get("strategy", {})
    hist_days = int(cfg.get("history_days_for_cost", 60))
    cooldown_min = int(risk_cfg.get("cooldown_min", 30))
    max_orders_per_day = int(risk_cfg.get("max_orders_per_symbol_per_day", 4))
    max_daily_volume_pct = float(risk_cfg.get("max_daily_volume_pct", 0))  # 每日最大交易额（总资产百分比）
    min_edge_bps = float(strategy_cfg.get("min_edge_bps", 25.0))
    fee_bps_taker = float(cfg.get("fees", {}).get("spot_taker_bps", 10.0))
    min_usdt_per_buy = float(cfg.get("min_usdt_per_buy", 5.0))
    max_daily_loss_pct = float(risk_cfg.get("max_daily_loss_pct", 1.5))
    cooldown_after_loss_min = int(risk_cfg.get("cooldown_after_loss_min", 60))

    # === v3.2: 今日亏损检查 — 超过最大亏损则进入保护模式 ===
    daily_pnl = _get_daily_pnl(symbol)
    if daily_pnl < -max_daily_loss_pct:
        log.warning(f"[{symbol}] 今日亏损{daily_pnl:.2f}%超过上限{max_daily_loss_pct}%，进入保护模式，仅允许止损卖出")
        # 返回HOLD快照，但不阻止止损卖出（止损在strategy中处理）
        # 通过设置标志，在后续风控中使用

    # === v4.4: 并行获取市场数据 (ThreadPoolExecutor) ===
    # 把不互相依赖的API调用并行发出, 减少总等待时间
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_kline_15m():
        return client.get_kline(symbol=symbol, interval="15", limit=100)
    def _fetch_ticker():
        return client.get_ticker(symbol)
    def _fetch_orderbook():
        return client.get_orderbook(symbol, limit=5)
    def _fetch_instruments():
        return client.get_instruments_info(symbol)
    def _fetch_btc_kline():
        return client.get_kline(symbol="BTCUSDT", interval="15", limit=100)
    def _fetch_h1_kline():
        return client.get_kline(symbol=symbol, interval="60", limit=100)
    def _fetch_balance():
        return _get_balances(client, symbol)
    def _fetch_trade_history():
        # v4.4: 一次获取全部交易记录, 供cost计算和最近买卖查询复用
        from cost import fetch_execs
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - hist_days * 24 * 3600 * 1000
        return fetch_execs(client, symbol, start_ms, end_ms)

    futures = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures['kl'] = executor.submit(_fetch_kline_15m)
        futures['tkr'] = executor.submit(_fetch_ticker)
        futures['ob'] = executor.submit(_fetch_orderbook)
        futures['instr'] = executor.submit(_fetch_instruments)
        futures['btc_kl'] = executor.submit(_fetch_btc_kline)
        futures['h1_kl'] = executor.submit(_fetch_h1_kline)
        futures['bal'] = executor.submit(_fetch_balance)
        futures['execs'] = executor.submit(_fetch_trade_history)

    kl = futures['kl'].result()
    df = enrich_indicators(klines_to_df(kl))

    last_price = float("nan")
    try:
        tkr = futures['tkr'].result()
        last_price = float(tkr["result"]["list"][0]["lastPrice"])
    except Exception:
        pass

    ob = futures['ob'].result()
    bid1, ask1 = client.extract_best_prices(ob)
    instr_json = futures['instr'].result()
    instr = parse_instr_filters(instr_json)

    # === 获取余额和成本 (v4.4: 已并行获取) ===
    base_bal, usdt_bal = futures['bal'].result()
    log.info(f"[{symbol}] 余额: {base_bal:.4f} SOL + ${usdt_bal:.2f} USDT")
    import math

    # v4.4: 获取预缓存的交易记录, 供后续所有cost函数复用
    _cached_execs = []
    try:
        _cached_execs = futures['execs'].result() or []
    except Exception as e:
        log.warning(f"[{symbol}] 获取交易记录失败: {e}")

    # v5.3: 预解析卖出记录，供等量匹配卖出价逻辑使用
    sell_execs_raw = []
    for _e in _cached_execs:
        if _e.get("side") == "Sell":
            try:
                _price = float(_e.get("execPrice", 0) or 0)
                _qty = float(_e.get("execQty", 0) or 0)
                _time_ms = int(_e.get("execTime", 0))
                if _price > 0 and _qty > 0:
                    sell_execs_raw.append({"price": _price, "qty": _qty, "time_ms": _time_ms})
            except (ValueError, TypeError):
                pass

    # 统一成本价计算：FIFO 为主，最近买入为备用 (v4.4: 传入缓存的交易记录)
    cost_price = get_cost_price(client, symbol, base_balance=base_bal, history_days=hist_days, cached_execs=_cached_execs)

    # 兜底：有持仓但无法计算成本 → 用当前价格；无持仓 → 0
    if math.isnan(cost_price) or cost_price <= 0:
        if base_bal > 0 and not math.isnan(last_price) and last_price > 0:
            cost_price = last_price
            log.warning(f"[{symbol}] 无法获取成本价，使用当前价格: {cost_price:.4f}")
        else:
            cost_price = 0.0

    # 15天趋势仅作为信息传递给策略，不再篡改成本价
    long_term_trend_pct = 0.0
    if df is not None and not df.empty:
        try:
            from cost import calculate_15day_trend
            trend_pct = calculate_15day_trend(df, days=15)
            if trend_pct is not None:
                long_term_trend_pct = trend_pct
        except Exception as e:
            log.warning(f"[{symbol}] 计算15天趋势失败: {e}")
    
    # === v4.4: 从缓存的交易记录中提取最近买入信息 (不再单独调API) ===
    last_buy_price = 0.0
    last_buy_time = 0
    _api_recent_trades = _cached_execs  # 复用已获取的交易记录
    # _cached_execs按时间升序, 需要倒序查找最近买入
    for exec_item in reversed(_cached_execs):
        if exec_item.get("side") == "Buy":
            try:
                last_buy_price = float(exec_item.get("execPrice", 0))
                last_buy_time = int(exec_item.get("execTime", 0))
                if last_buy_price > 0:
                    break
            except Exception:
                pass
    # 最终回退：使用成本价
    if last_buy_price <= 0:
        last_buy_price = cost_price

    # === 计算仓位 ===
    position = calculate_position(base_bal, usdt_bal, last_price)

    # === 提取技术指标 ===
    rsi14 = rsi7 = macd_hist = bb_position = trend_score = float("nan")
    atr_pct = volume_ratio = sma7 = sma24 = sma72 = float("nan")
    support = resistance = 0.0
    vol = float("nan")
    
    if df is not None and not df.empty:
        last_row = df.iloc[-1]
        rsi14 = float(last_row.get("RSI14", float("nan")))
        rsi7 = float(last_row.get("RSI7", float("nan")))
        macd_hist = float(last_row.get("MACD_Hist", 0))
        bb_position = float(last_row.get("BB_Position", 0.5))
        trend_score = float(last_row.get("Trend", 0))
        atr_pct = float(last_row.get("ATR_Pct", 0))
        volume_ratio = float(last_row.get("Volume_Ratio", 1.0))
        sma7 = float(last_row.get("SMA7", float("nan")))
        sma24 = float(last_row.get("SMA24", float("nan")))
        sma72 = float(last_row.get("SMA72", float("nan")))
        vol = float(last_row.get("VOL", float("nan")))

        # 获取市场状态
        market_cond = get_market_condition(df)
        support = market_cond.get("support", 0)
        resistance = market_cond.get("resistance", 0)
        
        # 计算最近高点/低点（用于回调买入/反弹卖出判断）
        # 使用最近20根K线（约20小时）的最高价和最低价
        lookback_period = 20
        if len(df) >= lookback_period:
            recent_high = float(df["high"].iloc[-lookback_period:].max())
            recent_low = float(df["low"].iloc[-lookback_period:].min())
        else:
            recent_high = float(df["high"].max()) if len(df) > 0 else last_price
            recent_low = float(df["low"].min()) if len(df) > 0 else last_price
    else:
        recent_high = last_price
        recent_low = last_price
    
    # === 获取最近卖出记录（用于高卖低买判断）===
    last_sell_price = 0.0
    last_sell_qty = 0.0
    last_sell_time = 0
    try:
        from cost import get_recent_sell_info
        sell_info = get_recent_sell_info(client, symbol, history_days=hist_days, cached_execs=_cached_execs)
        if sell_info:
            last_sell_price = sell_info.get("price", 0.0)
            last_sell_qty = sell_info.get("qty", 0.0)
            last_sell_time = sell_info.get("time", 0)
    except Exception as e:
        log.warning(f"[{symbol}] 获取最近卖出记录失败: {e}")
    
    # === 获取BTC趋势数据 (v4.4: 已并行获取) ===
    btc_trend_score = 0.0
    btc_long_term_trend_pct = 0.0
    try:
        btc_kl = futures['btc_kl'].result()
        btc_df = enrich_indicators(klines_to_df(btc_kl))
        
        if btc_df is not None and not btc_df.empty:
            btc_last_row = btc_df.iloc[-1]
            btc_trend_score = float(btc_last_row.get("Trend", 0))
            
            # 计算BTC的15天趋势百分比
            from cost import calculate_15day_trend
            btc_trend_pct = calculate_15day_trend(btc_df, days=15)
            if btc_trend_pct is not None:
                btc_long_term_trend_pct = btc_trend_pct
            
            log.debug(f"[{symbol}] BTC趋势: trend_score={btc_trend_score:.1f}, 15天趋势={btc_long_term_trend_pct:+.2f}%")
    except Exception as e:
        log.warning(f"[{symbol}] 获取BTC趋势数据失败: {e}，将使用默认值0")
    
    # === 计算资产占比和卖出均价（用于USDT主导时的成本计算）===
    avg_sell_price = 0.0
    usdt_pct = 0.0
    base_pct = 0.0
    total_value_usdt = 0.0
    
    try:
        # 计算总资产价值
        base_value_usdt = base_bal * last_price if not math.isnan(last_price) and last_price > 0 else 0.0
        total_value_usdt = base_value_usdt + usdt_bal
        
        if total_value_usdt > 0:
            usdt_pct = (usdt_bal / total_value_usdt) * 100
            base_pct = (base_value_usdt / total_value_usdt) * 100
            
            # 如果USDT占比 > 60%，计算卖出均价作为参考成本
            if usdt_pct > 60:
                from cost import get_avg_sell_price
                avg_sell = get_avg_sell_price(client, symbol, limit=10, history_days=7, cached_execs=_cached_execs)
                if avg_sell is not None and avg_sell > 0:
                    avg_sell_price = avg_sell
                    log.info(f"[{symbol}] USDT占比{usdt_pct:.1f}%，计算卖出均价作为参考成本: ${avg_sell_price:.4f}")
    except Exception as e:
        log.warning(f"[{symbol}] 计算卖出均价或资产占比失败: {e}")
    
    # === 获取止损冷却时间 ===
    last_stop_loss_time = 0
    try:
        sl_ts = get_meta(f"last_stop_loss_{symbol.upper()}")
        if sl_ts:
            last_stop_loss_time = int(sl_ts)
    except Exception:
        pass

    # === v3.1: 获取1H级别趋势数据 (v4.4: 已并行获取) ===
    h1_trend_score = 0.0
    h1_sma7 = 0.0
    h1_sma24 = 0.0
    h1_sma72 = 0.0
    h1_rsi14 = 50.0
    h1_ema8 = 0.0
    adx_val = 25.0
    try:
        h1_kl = futures['h1_kl'].result()
        h1_df = enrich_indicators(klines_to_df(h1_kl))
        if h1_df is not None and not h1_df.empty:
            h1_last = h1_df.iloc[-1]
            h1_trend_score = float(h1_last.get("Trend", 0))
            h1_sma7 = float(h1_last.get("SMA7", 0))
            h1_sma24 = float(h1_last.get("SMA24", 0))
            h1_sma72 = float(h1_last.get("SMA72", 0))
            h1_rsi14 = float(h1_last.get("RSI14", 50))
            h1_ema8 = float(h1_last.get("EMA8", 0))
            adx_val = float(h1_last.get("ADX", 25))
            if math.isnan(adx_val): adx_val = 25.0
            log.info(f"[{symbol}] 1H趋势: score={h1_trend_score:.0f}, "
                     f"SMA7={h1_sma7:.2f}, SMA24={h1_sma24:.2f}, EMA8={h1_ema8:.2f}, ADX={adx_val:.0f}")
    except Exception as e:
        log.warning(f"[{symbol}] 获取1H趋势数据失败: {e}")

    # === v4.3: 计算连续买入次数 (v4.4: 复用缓存的交易记录) ===
    consecutive_buys = 0
    if _api_recent_trades:
        # _cached_execs按时间升序, 倒序遍历找最近的连续买入
        for exec_item in reversed(_api_recent_trades):
            if exec_item.get("side") == "Buy":
                consecutive_buys += 1
            elif exec_item.get("side") == "Sell":
                break  # 遇到卖出就停止计数
    # else: API数据为空，consecutive_buys保持0

    # === 创建智能策略实例 ===
    strategy = create_smart_strategy(cfg)

    # 创建市场状态对象
    market_state = MarketState(
        last_price=last_price if not math.isnan(last_price) else 0,
        cost_price=cost_price if not math.isnan(cost_price) else 0,
        rsi14=rsi14 if not math.isnan(rsi14) else 50,
        rsi7=rsi7 if not math.isnan(rsi7) else 50,
        macd_hist=macd_hist if not math.isnan(macd_hist) else 0,
        bb_position=bb_position if not math.isnan(bb_position) else 0.5,
        trend_score=trend_score if not math.isnan(trend_score) else 0,
        atr_pct=atr_pct if not math.isnan(atr_pct) else 0,
        volume_ratio=volume_ratio if not math.isnan(volume_ratio) else 1,
        support=support,
        resistance=resistance,
        sma7=sma7 if not math.isnan(sma7) else last_price,
        sma24=sma24 if not math.isnan(sma24) else last_price,
        sma72=sma72 if not math.isnan(sma72) else last_price,
        recent_high=recent_high,
        recent_low=recent_low,
        last_sell_price=last_sell_price,
        last_sell_qty=last_sell_qty,
        last_sell_time=last_sell_time,
        avg_sell_price=avg_sell_price,
        original_cost_price=cost_price,  # 不再趋势调整，与cost_price相同
        usdt_balance=usdt_bal,
        base_balance=base_bal,
        usdt_pct=usdt_pct,
        base_pct=base_pct,
        long_term_trend_pct=long_term_trend_pct,
        btc_trend_score=btc_trend_score,
        btc_long_term_trend_pct=btc_long_term_trend_pct,
        last_stop_loss_time=last_stop_loss_time,
        last_buy_time=last_buy_time,
        # v3.1: 多时间框架数据
        h1_trend_score=h1_trend_score,
        h1_sma7=h1_sma7,
        h1_sma24=h1_sma24,
        h1_sma72=h1_sma72,
        h1_rsi14=h1_rsi14,
        consecutive_buys=consecutive_buys,
        # v5.2: 新增指标
        h1_ema8=h1_ema8,
        adx=adx_val,
        # v5.3: 等量匹配卖出价所需的原始卖出记录
        sell_execs_raw=sell_execs_raw,
        # v6.0: 新闻情绪
        **_get_news_fields(cfg),
    )
    
    # === 获取智能策略信号 ===
    # 传递总资产用于计算等量买入（基于卖出量）
    total_balance_usdt = position.total_value_usdt if hasattr(position, 'total_value_usdt') else 0.0
    signal = strategy.analyze(market_state, position.position_pct, last_buy_price, total_balance_usdt)

    decision = signal.action
    reason = signal.reason
    confidence = signal.confidence
    
    # === 风控检查 ===
    order_to_send = None
    
    trade_volume_usdt = 0.0  # 本次交易金额
    
    if decision in ("BUY", "SELL"):
        side = "Buy" if decision == "BUY" else "Sell"
        
        # 检查是否是止损卖出（止损时跳过RSI检查）
        is_stop_loss = decision == "SELL" and "触发止损" in reason

        # v3.2: 今日亏损保护模式 — 只允许止损卖出，禁止买入和普通卖出
        if daily_pnl < -max_daily_loss_pct and not is_stop_loss:
            if decision == "BUY":
                decision = "HOLD"
                reason = f"今日亏损{daily_pnl:.2f}%超过上限{max_daily_loss_pct}%，禁止买入"
                log.warning(f"[{symbol}] {reason}")
            # 非止损卖出仍允许（保护利润）

        # v3.3 自适应冷却：小步快走模式，缩短冷却但仍防重仓追高
        effective_cooldown = cooldown_min
        if side == "Buy":
            pos_pct = position.position_pct
            if pos_pct > 70:
                effective_cooldown = max(cooldown_min, 30)   # 重仓：至少30分钟
            elif pos_pct > 50:
                effective_cooldown = max(cooldown_min, 20)   # 中重仓：至少20分钟
            elif pos_pct > 30:
                effective_cooldown = max(cooldown_min, 15)   # 中仓：至少15分钟
        # v3.3 卖出冷却缩短
        elif side == "Sell":
            if not is_stop_loss:
                effective_cooldown = max(cooldown_min, 8)    # 非止损卖出：至少8分钟

        # 检查冷却时间
        ok1, why1 = _cooldown_ok(symbol, side, effective_cooldown)
        
        # 检查每日交易限制
        ok2, why2 = _check_daily_limits(symbol, side, max_orders_per_day)

        # v3.2 防频繁交易：检查今日买卖总次数（双向合计）
        buy_key = f"cnt_buy_{symbol.upper()}_{_nz_today_str()}"
        sell_key = f"cnt_sell_{symbol.upper()}_{_nz_today_str()}"
        total_today = (int(get_meta(buy_key) or 0)) + (int(get_meta(sell_key) or 0))
        ok2b = True
        why2b = ""
        if total_today >= max_orders_per_day * 2:  # 总交易次数不超过单方向限额的2倍
            ok2b = False
            why2b = f"今日总交易{total_today}次已达上限{max_orders_per_day * 2}次(买+卖)"

        # 检查传统风控门槛（传入regime用于自适应SMA过滤）
        regime = getattr(market_state, 'regime', 'SIDEWAYS')
        ok3, why3 = should_trade_gate(
            side, last_price, cost_price, rsi14, sma24, sma72, vol,
            strategy_cfg, fee_bps_taker, min_edge_bps, position.position_pct,
            avg_sell_price, usdt_pct, is_stop_loss=is_stop_loss,
            regime=regime
        )
        
        # 买入时检查：不能高于最近一次卖出价格（包括手动交易）
        # 这是硬性风控规则：如果在app上手动以137卖出，系统不应该在137或更高价格买入
        ok4, why4 = (True, "")
        if decision == "BUY" and last_sell_price > 0:
            # 高频模式允许更大容差，捕捉上涨趋势中的买入机会
            # STA fix: 低仓位时放宽容差，避免over-sell后无法重建仓位
            scalp_mode = bool(strategy_cfg.get("scalp_mode", False))
            pos_pct = position.position_pct if hasattr(position, 'position_pct') else 50
            if scalp_mode:
                # STA fix v2: 全面提高容差，避免错过breakout行情
                # 1H score=100的金叉突破被0.5%容差拦住，导致踏空
                if pos_pct < 15:
                    tolerance_pct = 0.020   # 极低仓位：2.0%容差，优先建仓
                elif pos_pct < 30:
                    tolerance_pct = 0.015   # 低仓位：1.5%容差
                elif pos_pct < 50:
                    tolerance_pct = 0.012   # 中仓位：1.2%容差
                else:
                    tolerance_pct = 0.008   # 高仓位：0.8%容差
            else:
                tolerance_pct = 0.001  # 普通模式0.1%
            if last_price > last_sell_price * (1 + tolerance_pct):
                ok4 = False
                price_diff_pct = (last_price - last_sell_price) / last_sell_price * 100
                why4 = f"买入价${last_price:.2f}高于最近卖出价${last_sell_price:.2f}（高{price_diff_pct:.2f}%，包括手动交易），拒绝买入以避免高买低卖"
                log.warning(f"[{symbol}] {why4}")
        
        if not (ok1 and ok2 and ok2b and ok3 and ok4):
            decision = "HOLD"
            failures = [why for ok, why in [(ok1, why1), (ok2, why2), (ok2b, why2b), (ok3, why3), (ok4, why4)] if not ok and why]
            reason = f"风控拦截：{'; '.join(failures)}"
            log.info(f"[{symbol}] {reason}")
        else:
            # 计算交易数量
            trade_pct = signal.position_pct
            leverage_enabled = bool(risk_cfg.get("leverage_enabled", False))
            leverage = float(risk_cfg.get("leverage", 1.0)) if leverage_enabled else 1.0
            qty, qty_reason = calculate_trade_qty(
                decision, position, trade_pct, last_price, instr, min_usdt_per_buy, leverage
            )
            
            if qty > 0:
                # 计算交易金额并检查每日额度（按总资产百分比，使用净交易额）
                trade_volume_usdt = qty * last_price  # 下单金额（包含杠杆）
                
                # 对于杠杆交易，限额应该基于实际使用的资金，而不是下单金额
                # 实际使用资金 = 下单金额 / 杠杆倍数（仅买入时）
                if decision == "BUY" and leverage >= 2.0:
                    actual_capital_used = trade_volume_usdt / leverage
                    # 限额检查使用实际使用的资金
                    ok4, why4 = _check_daily_volume_limit(symbol, actual_capital_used, max_daily_volume_pct, position.total_value_usdt, side)
                    log.info(f"[{symbol}] 杠杆交易：下单金额{trade_volume_usdt:.2f} USDT，实际使用资金{actual_capital_used:.2f} USDT（{leverage}x杠杆）")
                else:
                    # 非杠杆交易或卖出，使用下单金额
                    ok4, why4 = _check_daily_volume_limit(symbol, trade_volume_usdt, max_daily_volume_pct, position.total_value_usdt, side)
                
                if not ok4:
                    decision = "HOLD"
                    reason = f"额度限制：{why4}"
                    log.info(f"[{symbol}] {reason}")
                else:
                    # 生成订单
                    order_to_send = generate_order(
                        symbol, decision, qty, last_price, (bid1, ask1), instr
                    )
                    
                    if order_to_send:
                        reason = f"{reason} | 交易量: {qty_reason} | 信号强度: {confidence:.0f} | {why4}"
                    else:
                        decision = "HOLD"
                        reason = f"订单生成失败: {qty_reason}"
            else:
                decision = "HOLD"
                reason = f"交易数量不足: {qty_reason}"

    # === 记录信号 ===
    log.info(f"[{symbol}] 决策={decision} | 价格=${last_price:.2f} 成本=${cost_price:.2f} 仓位={position.position_pct:.0f}% | {reason[:120]}")
    log_signal(
        now_ms, symbol, last_price, cost_price, rsi14,
        sma7, sma24, sma72, vol, bid1 or 0.0, ask1 or 0.0,
        decision, reason
    )

    # === 执行交易 ===
    placed = None
    
    if decision in ("BUY", "SELL") and order_to_send:
        od = order_to_send
        
        # 检查是否启用交易
        if not bool(cfg.get("enable_trading", True)):
            log_trade(
                now_ms, symbol, od["side"], od["qty"], od.get("price", ""),
                od["orderType"], od.get("timeInForce", ""),
                reason + "（模拟：enable_trading=false）", "DRYRUN", "", {}
            )
            placed = {"status": "DRYRUN", "resp": {}}
            log.info(f"[{symbol}] DRYRUN: {od['side']} {od['qty']} @ {od.get('price')}")
        else:
            # 发送订单（杠杆启用且>=2倍时使用杠杆API）
            isLeverage = 1 if (leverage_enabled and leverage >= 2.0) else 0
            resp = client.place_order(
                symbol=od["symbol"],
                side=od["side"],
                order_type=od["orderType"],
                qty=od["qty"],
                price=od.get("price"),
                tif=od.get("timeInForce", "IOC"),
                order_link_id=f"smart_{symbol}_{int(time.time())}",
                isLeverage=isLeverage
            )

            status = "OK" if (resp and resp.get("retCode") == 0) else f"ERR({resp.get('retMsg') if resp else 'noresp'})"

            order_id = ""
            try:
                order_id = (((resp or {}).get("result") or {}).get("orderId") or "") if resp else ""
            except Exception:
                pass

            # 检查是否是抵押品设置错误
            if resp and resp.get("retCode") != 0:
                error_msg = resp.get("retMsg", "")
                if "collateral" in error_msg.lower() or "collateral settings" in error_msg.lower():
                    base_coin = symbol.replace("USDT", "")
                    log.error(f"[{symbol}] 杠杆交易失败：{base_coin} 未设置为抵押品")
                    log.error(f"[{symbol}] 请在Bybit账户中将 {base_coin} 设置为抵押品：资产 -> 现货账户 -> 杠杆账户 -> 设置为抵押品")

            log_trade(
                now_ms, symbol, od["side"], od["qty"], od.get("price", ""),
                od["orderType"], od.get("timeInForce", ""),
                reason, status, order_id, resp or {}
            )

            # 更新冷却时间（无论成功失败都更新，避免频繁重试）
            set_meta(f"last_{od['side'].lower()}_{symbol.upper()}", str(now_ms))

            placed = {"status": status, "resp": resp}

            if status == "OK":
                # 记录止损事件时间（用于止损后冷却）
                if "触发止损" in reason:
                    set_meta(f"last_stop_loss_{symbol.upper()}", str(now_ms))
                    log.info(f"[{symbol}] 止损已触发，将进入{cfg.get('strategy',{}).get('stop_loss_cooldown_min', 45)}分钟买入冷却期")

                # 只有交易成功时才计入每日次数和交易额
                _bump_side_counter(symbol, od["side"])
                
                # 对于杠杆交易，记录实际使用的资金，而不是下单金额
                if od["side"].upper() == "BUY" and leverage >= 2.0:
                    actual_capital_used = trade_volume_usdt / leverage
                    _add_daily_volume(symbol, actual_capital_used, od["side"])
                    log.info(f"[{symbol}] 杠杆交易记录：下单金额{trade_volume_usdt:.2f} USDT，实际使用资金{actual_capital_used:.2f} USDT")
                else:
                    _add_daily_volume(symbol, trade_volume_usdt, od["side"])
                net_vol = _get_daily_volume(symbol)
                buy_vol = _get_daily_buy_volume(symbol)
                sell_vol = _get_daily_sell_volume(symbol)
                log.info(f"[{symbol}] 交易成功: {od['side']} {od['qty']} @ {od.get('price')} | 今日净交易额: ${net_vol:.2f} (买入${buy_vol:.2f} - 卖出${sell_vol:.2f})")
            else:
                # 交易失败不计入每日次数限制，但记录日志
                log.warning(f"[{symbol}] 交易失败: {status} (不计入每日次数限制)")
    
    # === 构建快照返回 ===
    snapshot = {
        "ts_ms": now_ms,
        "symbol": symbol,
        "last_price": last_price,
        "cost_price": cost_price,
        "position_pct": position.position_pct,
        "total_value_usdt": position.total_value_usdt,
        "rsi14": rsi14,
        "rsi7": rsi7,
        "macd_hist": macd_hist,
        "bb_position": bb_position,
        "trend_score": trend_score,
        "atr_pct": atr_pct,
        "sma7": sma7,
        "sma24": sma24,
        "sma72": sma72,
        "vol": vol,
        "support": support,
        "resistance": resistance,
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        "placed": placed
    }
    
    return snapshot


def get_portfolio_summary(client: BybitClient, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """获取投资组合摘要"""
    symbols = cfg.get("symbols", [])
    
    total_value = 0.0
    positions = []
    
    for symbol in symbols:
        try:
            base_bal, usdt_bal = _get_balances(client, symbol)
            
            tkr = client.get_ticker(symbol)
            last_price = float(tkr["result"]["list"][0]["lastPrice"])
            
            position = calculate_position(base_bal, usdt_bal, last_price)
            
            # 只对第一个symbol算usdt（避免重复计算）
            if symbol == symbols[0]:
                total_value += position.total_value_usdt
            else:
                total_value += position.base_value_usdt
            
            import math
            cost_price = get_cost_price(client, symbol, base_balance=base_bal, history_days=60)
            if math.isnan(cost_price) or cost_price <= 0:
                cost_price = 0.0
            pnl_pct = ((last_price - cost_price) / cost_price * 100) if cost_price > 0 else 0
            
            positions.append({
                "symbol": symbol,
                "base_balance": base_bal,
                "last_price": last_price,
                "cost_price": cost_price,
                "value_usdt": position.base_value_usdt,
                "position_pct": position.position_pct,
                "pnl_pct": pnl_pct
            })
        except Exception as e:
            log.warning(f"获取{symbol}信息失败: {e}")
    
    return {
        "total_value_usdt": total_value,
        "positions": positions,
        "timestamp": _now_ms()
    }
