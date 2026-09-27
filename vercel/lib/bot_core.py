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
from db_pg import init_db, log_trade, log_signal, set_meta, get_meta, recent_signals
from cost import get_spot_avg_cost, get_spot_avg_cost_by_position, get_cost_price
from strategy_v5 import should_trade_gate, SmartStrategy, MarketState, create_smart_strategy
from news_sentiment import get_news_sentiment, get_cached_score, get_cached_sentiment

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
        log.info(f"[NEWS] 后台线程开始刷新...")
        result = get_news_sentiment(api_key, model)
        log.info(f"[NEWS] 后台刷新完成: score={result.get('score',0)}, summary={result.get('summary','')[:50]}")
    except Exception as e:
        log.warning(f"[NEWS] 后台刷新失败: {e}")
        import traceback
        log.warning(traceback.format_exc())

_news_first_fetch_done = False

def _get_news_fields(cfg: dict) -> dict:
    """获取新闻情绪字段（serverless 版）：缓存进 PG，过期则同步刷新。
    无状态环境下后台线程会被杀，故改为 PG 缓存 + 同步取，每 TTL 分钟才调一次 GPT。"""
    try:
        api_key = cfg.get("gpt_api_key", "")
        model = cfg.get("gpt_model", "gpt-4o")
        if not api_key:
            return {}

        import json as _json
        TTL_MIN = int(cfg.get("news_cache_ttl_min", 45))
        now = _now_ms()

        cached = get_meta("_news_cache")
        if cached:
            try:
                d = _json.loads(cached)
                age_min = (now - int(d.get("ts", 0))) / 60000.0
                # 需要双语摘要齐全才算完整缓存，否则重新抓(让旧的单语缓存自动升级)
                has_detail = bool(d.get("summary_zh")) and bool(d.get("summary_en"))
                if age_min < TTL_MIN and has_detail:
                    return {
                        "news_sentiment": d.get("score", 0),
                        "news_confidence": d.get("confidence", 0.0),
                        "news_risk_level": d.get("risk_level", "medium"),
                        "news_action": d.get("suggested_action", "hold"),
                    }
            except Exception:
                pass  # 缓存损坏则重新拉

        # 过期或无缓存：同步取一次（serverless 友好，结果写回 PG 供后续拍复用）
        log.info("[NEWS] 缓存过期/缺失，同步拉取新闻情绪...")
        result = get_news_sentiment(api_key, model)
        d = {
            "score": result.get("score", 0),
            "confidence": result.get("confidence", 0.0),
            "risk_level": result.get("risk_level", "medium"),
            "suggested_action": result.get("suggested_action", "hold"),
            "summary": result.get("summary", ""),
            "summary_zh": result.get("summary_zh", result.get("summary", "")),
            "summary_en": result.get("summary_en", result.get("summary", "")),
            "key_factors": (result.get("key_factors") or [])[:3],
            "key_factors_zh": (result.get("key_factors_zh") or result.get("key_factors") or [])[:3],
            "key_factors_en": (result.get("key_factors_en") or result.get("key_factors") or [])[:3],
            "ts": now,
        }
        try:
            set_meta("_news_cache", _json.dumps(d, ensure_ascii=False))
        except Exception as e:
            log.warning(f"[NEWS] 写缓存失败: {e}")
        return {
            "news_sentiment": d["score"],
            "news_confidence": d["confidence"],
            "news_risk_level": d["risk_level"],
            "news_action": d["suggested_action"],
        }
    except Exception as e:
        log.warning(f"[NEWS] 获取新闻情绪失败: {e}")
        import traceback
        log.warning(traceback.format_exc())
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


def _check_daily_volume_limit(
    symbol: str,
    proposed_volume: float,
    max_daily_volume_pct: float,
    total_asset_value: float,
    side: str,
) -> Tuple[bool, str]:
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
                        # 真正可动用资金优先：availableToWithdraw > free > (walletBalance-locked)
                        # 绝不使用 availableToBorrow（可借额度，非自有资金，会虚高余额）
                        usdt_bal = float(coin_info.get("availableToWithdraw", 0) or 0)
                        if usdt_bal == 0:
                            usdt_bal = float(coin_info.get("free", 0) or 0)
                        if usdt_bal == 0:
                            _wb = float(coin_info.get("walletBalance", 0) or 0)
                            _lk = float(coin_info.get("locked", 0) or 0)
                            usdt_bal = max(0.0, _wb - _lk)
                    elif coin.upper() == base.upper():
                        base_bal = float(coin_info.get("availableToWithdraw", 0) or 0)
                        if base_bal == 0:
                            base_bal = float(coin_info.get("free", 0) or 0)
                        if base_bal == 0:
                            _wb = float(coin_info.get("walletBalance", 0) or 0)
                            _lk = float(coin_info.get("locked", 0) or 0)
                            base_bal = max(0.0, _wb - _lk)
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
    from execution_engine import run_step
    return run_step(client, cfg, symbol, news_provider=_get_news_fields)


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
            from cost import get_reconciled_cost
            cost_price = get_reconciled_cost(symbol, base_bal)
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
