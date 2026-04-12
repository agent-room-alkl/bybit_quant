# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
import time
import math

WINDOW_MS = 7 * 24 * 3600 * 1000

def fetch_execs(client, symbol: str, start_ms: int, end_ms: int, limit: int = 100) -> List[Dict[str, Any]]:
    execs: List[Dict[str, Any]] = []
    t0 = start_ms
    while t0 < end_ms:
        t1 = min(t0 + WINDOW_MS - 1, end_ms)
        cursor = None
        while True:
            resp = client.get_trade_history(symbol=symbol, start_ms=t0, end_ms=t1, limit=limit, cursor=cursor)
            if resp.get("retCode") != 0:
                break
            res = resp.get("result", {}) or {}
            lst = res.get("list", []) or []
            execs.extend(lst)
            cursor = res.get("nextPageCursor")
            if not cursor or not lst:
                break
        t0 = t1 + 1
    execs.sort(key=lambda x: int(x.get("execTime", 0)))
    return execs

def compute_spot_avg_cost_from_execs(execs: List[Dict[str, Any]], base_coin: str, quote_coin: str) -> float:
    """
    计算现货平均成本价
    
    注意：如果最终持仓为0（卖出数量>=买入数量），返回NaN
    这种情况下应该使用最近买入价格的平均值作为成本价
    """
    qty = 0.0
    cost = 0.0
    buy_prices = []  # 记录所有买入价格，用于备用计算
    buy_quantities = []  # 记录所有买入数量
    
    for e in execs:
        try:
            side = e.get("side")
            px = float(e.get("execPrice", 0) or 0)
            q = float(e.get("execQty", 0) or 0)
            fee = float(e.get("execFee", 0) or 0)
            fee_ccy = str(e.get("feeCurrency") or "")
            if side == "Buy":
                cost += px * q
                if fee_ccy.upper() == quote_coin.upper():
                    cost += fee
                qty += q
                # 记录买入价格和数量
                if px > 0 and q > 0:
                    buy_prices.append(px)
                    buy_quantities.append(q)
            elif side == "Sell":
                if qty <= 0:
                    continue
                reduce = min(q, qty)
                avg_cost = cost / qty if qty > 0 else 0.0
                cost -= avg_cost * reduce
                qty -= reduce
        except Exception:
            continue
    
    # 如果最终有持仓，返回平均成本
    if qty > 0:
        return cost / qty
    
    # 如果最终没有持仓（卖出>=买入），返回NaN
    # 注意：不应该使用所有历史买入价格的平均值，因为：
    # 1. 可能包含很久以前的买入价格，不反映当前持仓成本
    # 2. 账户可能有余额但交易历史不完整（超出查询范围、充值等）
    # 应该由调用方使用最近10次买入价格作为备用方案
    return float("nan")

def get_cost_price(client, symbol: str, base_balance: float = 0.0,
                    history_days: int = 60, cached_execs: Optional[List[Dict[str, Any]]] = None) -> float:
    """
    统一成本价计算入口（FIFO 为主，最近买入为备用）

    优先级：
    1. API 直接返回的成本价（如平台支持）
    2. FIFO 加权平均（compute_spot_avg_cost_from_execs）
    3. 最近 10 次买入的加权平均
    4. 返回 NaN

    v4.4: 支持cached_execs参数, 避免重复调用fetch_execs
    """
    import math, logging
    log = logging.getLogger("cost")

    # --- FIFO 优先，失败时 fallback 到 API 成本价 ---
    _api_cost_fallback = None
    try:
        s = symbol.upper()
        base = s[:-4] if s.endswith(("USDT", "USDC")) else s
        if hasattr(client, 'get_spot_cost_price'):
            _api_cost_fallback = client.get_spot_cost_price(base)
    except Exception:
        pass

    # --- 解析 base / quote ---
    try:
        instr = client.get_instruments_info(symbol)
        row = (instr.get("result", {}) or {}).get("list", [])[0]
        base_coin, quote_coin = str(row.get("baseCoin")), str(row.get("quoteCoin"))
    except Exception:
        s = symbol.upper()
        base_coin = s[:-4] if s.endswith(("USDT", "USDC")) else s
        quote_coin = "USDT"

    # --- v4.4: 复用缓存的交易记录, 避免重复API调用 ---
    execs = cached_execs
    if execs is None:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - history_days * 24 * 3600 * 1000
        try:
            execs = fetch_execs(client, symbol, start_ms, end_ms)
        except Exception as e:
            log.warning(f"[{symbol}] 获取交易记录失败: {e}")
            execs = []

    # --- FIFO ---
    try:
        if execs:
            fifo = compute_spot_avg_cost_from_execs(execs, base_coin, quote_coin)
            if not math.isnan(fifo) and fifo > 0:
                log.info(f"[{symbol}] FIFO成本价: ${fifo:.4f}")
                return fifo
    except Exception as e:
        log.warning(f"[{symbol}] FIFO计算失败: {e}")

    # --- 备用：最近买入 (复用已获取的execs) ---
    try:
        avg = get_avg_cost_from_recent_buys(
            client, symbol, limit=10, history_days=history_days,
            current_balance=base_balance if base_balance > 0 else None,
            cached_execs=execs)
        if avg is not None and avg > 0:
            log.info(f"[{symbol}] 备用成本价(最近买入均价): ${avg:.4f}")
            return avg
    except Exception as e:
        log.warning(f"[{symbol}] 备用成本计算失败: {e}")

    # --- 最终 fallback：Bybit API 返回的成本价 ---
    if _api_cost_fallback is not None and _api_cost_fallback > 0:
        log.info(f"[{symbol}] 使用Bybit API成本价(fallback): ${_api_cost_fallback:.4f}")
        return _api_cost_fallback

    return float("nan")


def get_recent_sell_info(client, symbol: str, history_days: int = 60, cached_execs: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """
    从API交易历史获取最近的卖出记录

    Args:
        client: BybitClient实例
        symbol: 交易对符号
        history_days: 查询历史天数（默认60天）
        cached_execs: v4.4 预获取的交易记录, 避免重复API调用

    Returns:
        包含卖出价格、数量、时间的字典，如果没有找到则返回None
        {
            "price": float,  # 卖出价格
            "qty": float,    # 卖出数量
            "time": int      # 卖出时间（毫秒时间戳）
        }
    """
    try:
        # v4.4: 复用缓存的交易记录
        if cached_execs is not None:
            execs = cached_execs
        else:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - history_days * 24 * 3600 * 1000
            execs = fetch_execs(client, symbol, start_ms, end_ms)
        if not execs:
            return None
        
        # 筛选卖出交易，按时间倒序（最新的在前）
        sell_execs = []
        for e in execs:
            if e.get("side") == "Sell":
                try:
                    price = float(e.get("execPrice", 0) or 0)
                    qty = float(e.get("execQty", 0) or 0)
                    exec_time = int(e.get("execTime", 0))
                    if price > 0 and qty > 0:
                        sell_execs.append({
                            "price": price,
                            "qty": qty,
                            "time": exec_time
                        })
                except (ValueError, TypeError):
                    continue
        
        # 按时间倒序排序（最新的在前）
        sell_execs.sort(key=lambda x: x["time"], reverse=True)
        
        # 返回最近的卖出记录
        if sell_execs:
            import logging
            log = logging.getLogger("cost")
            log.debug(f"[{symbol}] 找到最近卖出记录: ${sell_execs[0]['price']:.4f}, 数量: {sell_execs[0]['qty']:.4f}")
            return sell_execs[0]
        
        return None
        
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"从API交易历史获取最近卖出记录失败: {e}")
        return None


def get_avg_sell_price(client, symbol: str, limit: int = 10, history_days: int = 7, cached_execs: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """
    从API交易历史获取最近N次卖出价格，计算加权平均卖出价

    用途：当USDT余额 > SOL余额时，使用卖出均价作为参考成本价
    如果当前价格 < 卖出均价，说明之前卖在高点，现在可以买入更多SOL

    Args:
        client: BybitClient实例
        symbol: 交易对符号
        limit: 获取最近N次卖出（默认10次，7天窗口内）
        history_days: 查询历史天数（默认7天）
        cached_execs: v4.4 预获取的交易记录, 避免重复API调用

    Returns:
        加权平均卖出价格，如果没有找到则返回None
    """
    try:
        # v4.4: 复用缓存的交易记录
        if cached_execs is not None:
            execs = cached_execs
        else:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - history_days * 24 * 3600 * 1000
            execs = fetch_execs(client, symbol, start_ms, end_ms)
        if not execs:
            return None
        
        # 筛选卖出交易，按时间倒序（最新的在前）
        sell_execs = []
        for e in execs:
            if e.get("side") == "Sell":
                try:
                    price = float(e.get("execPrice", 0) or 0)
                    qty = float(e.get("execQty", 0) or 0)
                    exec_time = int(e.get("execTime", 0))
                    if price > 0 and qty > 0:
                        sell_execs.append({
                            "price": price,
                            "qty": qty,
                            "time": exec_time
                        })
                except (ValueError, TypeError):
                    continue
        
        # 按时间倒序排序（最新的在前）
        sell_execs.sort(key=lambda x: x["time"], reverse=True)
        
        # 取最近N次卖出
        recent_sells = sell_execs[:limit]
        
        if not recent_sells:
            return None
        
        # 计算加权平均价格（按数量加权）
        total_value = sum(s["price"] * s["qty"] for s in recent_sells)
        total_qty = sum(s["qty"] for s in recent_sells)
        
        if total_qty > 0:
            avg_price = total_value / total_qty
            import logging
            log = logging.getLogger("cost")
            log.info(f"[{symbol}] 从API交易历史获取最近{len(recent_sells)}次卖出，加权平均卖出价: ${avg_price:.4f} (总数量: {total_qty:.4f})")
            return avg_price
        
        return None
        
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"从API交易历史获取卖出均价失败: {e}")
        return None


def get_matched_sell_price(sell_execs_raw: List[Dict[str, Any]], target_qty: float, max_hours: int = 48) -> Optional[Tuple[float, float]]:
    """
    从预解析的卖出记录中，按时间倒序累加直到达到目标数量，计算加权均卖价。

    Args:
        sell_execs_raw: 已解析的卖出记录列表，每条含 price, qty, time_ms 字段
        target_qty: 计划买入量（目标匹配数量）
        max_hours: 最大回溯小时数（默认48小时）

    Returns:
        (matched_qty, weighted_avg_price)；如果窗口内无卖出记录则返回 None
    """
    import logging as _logging
    _log = _logging.getLogger("cost")
    cutoff_ms = int(time.time() * 1000) - max_hours * 3600 * 1000
    recent_sells = [e for e in sell_execs_raw if e.get("time_ms", 0) >= cutoff_ms]
    recent_sells.sort(key=lambda x: x["time_ms"], reverse=True)

    if not recent_sells:
        return None

    matched_qty = 0.0
    total_value = 0.0
    for sell in recent_sells:
        price = sell["price"]
        qty = sell["qty"]
        take = min(qty, target_qty - matched_qty)
        matched_qty += take
        total_value += take * price
        if matched_qty >= target_qty:
            break

    if matched_qty > 0:
        avg_price = total_value / matched_qty
        _log.debug(f"get_matched_sell_price: target={target_qty:.4f}, matched={matched_qty:.4f}, avg_price={avg_price:.4f}")
        return (matched_qty, avg_price)
    return None


def get_avg_cost_from_recent_buys(client, symbol: str, limit: int = 10, history_days: int = 10, current_balance: Optional[float] = None, cached_execs: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """
    从API交易历史获取最近N次买入价格，计算平均成本价

    Args:
        client: BybitClient实例
        symbol: 交易对符号
        limit: 获取最近N次买入（默认10次）
        history_days: 查询历史天数（默认60天）
        current_balance: 当前账户余额（可选），如果提供，会优先选择与余额数量相近的买入记录
        cached_execs: v4.4 预获取的交易记录, 避免重复API调用

    Returns:
        平均买入价格，如果没有找到则返回None
    """
    try:
        # v4.4: 复用缓存的交易记录
        if cached_execs is not None:
            execs = cached_execs
        else:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - history_days * 24 * 3600 * 1000
            execs = fetch_execs(client, symbol, start_ms, end_ms)
        if not execs:
            return None
        
        # 筛选买入交易，按时间倒序（最新的在前）
        buy_execs = []
        for e in execs:
            if e.get("side") == "Buy":
                try:
                    price = float(e.get("execPrice", 0) or 0)
                    qty = float(e.get("execQty", 0) or 0)
                    exec_time = int(e.get("execTime", 0))
                    if price > 0 and qty > 0:
                        buy_execs.append({
                            "price": price,
                            "qty": qty,
                            "time": exec_time
                        })
                except (ValueError, TypeError):
                    continue
        
        # 按时间倒序排序（最新的在前）
        buy_execs.sort(key=lambda x: x["time"], reverse=True)
        
        # 如果提供了当前余额，优先选择与余额数量相近的买入记录
        if current_balance is not None and current_balance > 0:
            # 找到与余额数量最相近的买入记录（允许±50%的误差）
            matching_buys = []
            for buy in buy_execs:
                if 0.5 * current_balance <= buy["qty"] <= 1.5 * current_balance:
                    matching_buys.append(buy)
            
            # 如果找到匹配的买入记录，使用这些记录计算成本价
            if matching_buys:
                # 取最近N次匹配的买入记录
                recent_matching = matching_buys[:limit]
                total_cost = sum(b["price"] * b["qty"] for b in recent_matching)
                total_qty = sum(b["qty"] for b in recent_matching)
                
                if total_qty > 0:
                    avg_price = total_cost / total_qty
                    import logging
                    log = logging.getLogger("cost")
                    log.info(f"[{symbol}] 基于当前余额({current_balance:.4f})，从{len(recent_matching)}次匹配买入记录计算成本价: ${avg_price:.4f}")
                    return avg_price
        
        # 如果没有匹配的买入记录，使用最近N次买入
        recent_buys = buy_execs[:limit]
        
        if not recent_buys:
            return None
        
        # 计算加权平均价格（按数量加权）
        total_cost = sum(b["price"] * b["qty"] for b in recent_buys)
        total_qty = sum(b["qty"] for b in recent_buys)
        
        if total_qty > 0:
            avg_price = total_cost / total_qty
            import logging
            log = logging.getLogger("cost")
            if current_balance is not None and current_balance > 0:
                log.info(f"[{symbol}] 当前余额({current_balance:.4f})，使用最近{len(recent_buys)}次买入，平均价格: ${avg_price:.4f}")
            else:
                log.info(f"[{symbol}] 从API交易历史获取最近{len(recent_buys)}次买入，平均价格: ${avg_price:.4f}")
            return avg_price
        
        return None
        
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"从API交易历史获取最近买入价格失败: {e}")
        return None


def get_spot_avg_cost_by_position(
    client,
    symbol: str,
    usdt_balance: float,
    base_balance: float,
    last_price: float,
    history_days: int = 60,
    cost_adjustment_factor: float = 1.0,
    limit: int = 30,
    cached_execs: Optional[List[Dict[str, Any]]] = None
) -> float:
    """
    根据最近N次买入交易记录计算加权平均成本价
    
    逻辑：
    - 只使用最近N次（默认30次）的买入交易记录
    - 计算买入价格的加权平均（按数量加权）
    - 不使用卖出交易记录
    - 应用成本价调整系数（cost_adjustment_factor）
    
    Args:
        client: BybitClient实例
        symbol: 交易对符号
        usdt_balance: USDT余额（未使用，保留接口兼容性）
        base_balance: 基础币余额（未使用，保留接口兼容性）
        last_price: 当前价格（未使用，保留接口兼容性）
        history_days: 查询历史天数（默认60天，用于限制查询时间范围，不作为主要限制）
        cost_adjustment_factor: 成本价调整系数（默认1.0，1.1表示价格×1.1，0.9表示价格×0.9）
        limit: 使用的买入交易次数（默认30次）
    
    Returns:
        加权平均成本价（已应用调整系数），如果计算失败返回NaN
    """
    import math
    import logging
    log = logging.getLogger("cost")
    
    try:
        # v4.4: 复用缓存的交易记录
        if cached_execs is not None:
            execs = cached_execs
        else:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - history_days * 24 * 3600 * 1000
            execs = fetch_execs(client, symbol, start_ms, end_ms)
        if not execs:
            log.warning(f"[{symbol}] 无法获取交易历史")
            return float("nan")
        
        # 只提取买入交易，并按时间倒序排序（最新的在前）
        buy_execs = []
        for e in execs:
            try:
                side = e.get("side")
                if side != "Buy":
                    continue  # 只处理买入交易
                
                price = float(e.get("execPrice", 0) or 0)
                qty = float(e.get("execQty", 0) or 0)
                
                if price > 0 and qty > 0:
                    buy_execs.append(e)
            except (ValueError, TypeError):
                continue
        
        # 按时间倒序排序（最新的在前），然后只取最近limit次
        buy_execs.sort(key=lambda x: int(x.get("execTime", 0)), reverse=True)
        buy_execs = buy_execs[:limit]
        
        # 统计买入交易
        buy_total_qty = 0.0
        buy_total_value = 0.0
        buy_count = len(buy_execs)
        
        for e in buy_execs:
            try:
                price = float(e.get("execPrice", 0) or 0)
                qty = float(e.get("execQty", 0) or 0)
                
                if price > 0 and qty > 0:
                    buy_total_qty += qty
                    buy_total_value += price * qty
            except (ValueError, TypeError):
                continue
        
        # 计算买入均价（加权平均）
        if buy_total_qty > 0:
            avg_buy_price = buy_total_value / buy_total_qty
            
            # 应用成本价调整系数
            adjusted_cost_price = avg_buy_price * cost_adjustment_factor
            
            # 详细日志：显示计算过程
            log.info(f"[{symbol}] ===== 成本价计算详情（最近{limit}次买入记录）=====")
            log.info(f"[{symbol}] 买入记录: 交易次数={buy_count}, 总数量={buy_total_qty:.4f}, 总价值=${buy_total_value:.2f}")
            log.info(f"[{symbol}] 加权平均成本价: ${avg_buy_price:.4f}")
            log.info(f"[{symbol}] 成本价调整系数: {cost_adjustment_factor:.2f}")
            log.info(f"[{symbol}] 调整后成本价: ${adjusted_cost_price:.4f}")
            log.info(f"[{symbol}] ===== 成本价: ${adjusted_cost_price:.4f} =====")
            
            return adjusted_cost_price
        else:
            log.warning(f"[{symbol}] 最近{limit}次买入记录中没有有效数据")
            return float("nan")
        
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"[{symbol}] 计算成本价失败: {e}", exc_info=True)
        return float("nan")


def get_spot_avg_cost(client, symbol: str, history_days: int = 180, current_balance: Optional[float] = None) -> float:
    """
    获取现货平均成本价
    
    方法1: 尝试从API直接获取（如果支持）
    方法2: 通过交易历史计算（当前方法）
    方法3: 从API交易历史获取最近买入价格计算（备用方案）
    
    Args:
        client: BybitClient实例
        symbol: 交易对符号
        history_days: 查询历史天数（默认180天）
        current_balance: 当前账户余额（可选），如果提供，会优先选择与余额数量相近的买入记录
    """
    # 首先尝试从API直接获取成本价
    try:
        s = symbol.upper()
        if s.endswith("USDT"):
            base = s[:-4]
        elif s.endswith("USDC"):
            base = s[:-4]
        else:
            base = s
        
        # 尝试从钱包余额API获取成本价
        if hasattr(client, 'get_spot_cost_price'):
            api_cost = client.get_spot_cost_price(base)
            if api_cost is not None and api_cost > 0:
                return api_cost
    except Exception:
        pass
    
    # 如果API不支持，使用交易历史计算
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - history_days * 24 * 3600 * 1000
    try:
        instr = client.get_instruments_info(symbol)
        row = (instr.get("result", {}) or {}).get("list", [])[0]
        base, quote = str(row.get("baseCoin")), str(row.get("quoteCoin"))
    except Exception:
        s = symbol.upper()
        if s.endswith("USDT"):
            base, quote = s[:-4], "USDT"
        elif s.endswith("USDC"):
            base, quote = s[:-4], "USDC"
        else:
            base, quote = s, "USDT"
    
    try:
        execs = fetch_execs(client, symbol, start_ms, end_ms)
        if execs:
            cost_price = compute_spot_avg_cost_from_execs(execs, base, quote)
            # 如果计算出的成本价有效（有持仓），直接返回
            if not math.isnan(cost_price) and cost_price > 0:
                return cost_price
            # 如果返回NaN（持仓为0），但账户可能有余额，使用API交易历史中的最近10次买入价格
            # 这比数据库记录更准确，因为API记录是完整的交易历史
            import logging
            log = logging.getLogger("cost")
            log.debug(f"[{symbol}] 交易历史计算显示无持仓，尝试使用API交易历史中的最近买入价格")
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"获取交易历史失败: {e}")
    
    # 如果交易历史计算失败或返回NaN，从API交易历史获取最近10次买入价格
    # 使用API交易历史而不是数据库记录，更准确可靠
    # 如果提供了当前余额，会优先选择与余额数量相近的买入记录
    try:
        avg_buy_price = get_avg_cost_from_recent_buys(
            client, symbol, 
            limit=10, 
            history_days=history_days,
            current_balance=current_balance
        )
        if avg_buy_price is not None and avg_buy_price > 0:
            import logging
            log = logging.getLogger("cost")
            if current_balance is not None and current_balance > 0:
                log.info(f"[{symbol}] 基于当前余额({current_balance:.4f})，使用API交易历史中最近买入价格计算成本价: {avg_buy_price:.4f}")
            else:
                log.info(f"[{symbol}] 使用API交易历史中最近10次买入价格计算成本价: {avg_buy_price:.4f}")
            return avg_buy_price
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"从API交易历史获取最近买入价格失败: {e}")
    
    # 所有方法都失败，返回NaN
    return float("nan")


def calculate_15day_trend(df, days: int = 15) -> Optional[float]:
    """
    计算最近N天的大趋势（价格变化百分比）
    
    如果大趋势是上涨的，需要调整成本价，因为上涨趋势中最低价格肯定超过了成本价，
    所以永远也买入不了。通过调整成本价，可以让买入条件更合理。
    
    Args:
        df: DataFrame，包含K线数据（必须有'close'列和'startTime'列）
        days: 计算趋势的天数（默认15天）
    
    Returns:
        价格变化百分比（正数表示上涨，负数表示下跌），如果无法计算则返回None
    """
    try:
        if df is None or df.empty:
            return None
        
        if 'close' not in df.columns or 'startTime' not in df.columns:
            return None
        
        # 确保按时间排序
        df_sorted = df.sort_values('startTime').reset_index(drop=True)
        
        # 计算需要多少根K线（假设是1小时K线，15天 = 15 * 24 = 360根）
        # 但为了更准确，我们使用时间范围而不是K线数量
        import pandas as pd
        from datetime import timedelta
        
        if len(df_sorted) < 2:
            return None
        
        # 获取最新时间
        latest_time = df_sorted['startTime'].iloc[-1]
        
        # 计算N天前的时间
        if isinstance(latest_time, pd.Timestamp):
            target_time = latest_time - timedelta(days=days)
        else:
            # 如果是其他格式，尝试转换
            target_time = pd.to_datetime(latest_time) - timedelta(days=days)
        
        # 找到N天前的K线（最接近目标时间的K线）
        mask = df_sorted['startTime'] <= target_time
        if mask.sum() == 0:
            # 如果没有找到，使用最早的数据
            if len(df_sorted) < 2:
                return None
            old_price = float(df_sorted['close'].iloc[0])
        else:
            # 使用最接近目标时间的K线（选择最接近但不早于目标时间的最后一条）
            filtered_df = df_sorted[mask]
            if len(filtered_df) > 0:
                # 选择最接近目标时间的K线（最后一条满足条件的）
                old_idx = filtered_df.index[-1]
                old_price = float(df_sorted.loc[old_idx, 'close'])
            else:
                # 如果没有找到，使用最早的数据
                old_price = float(df_sorted['close'].iloc[0])
        
        # 获取最新价格
        new_price = float(df_sorted['close'].iloc[-1])
        
        if old_price <= 0 or new_price <= 0:
            return None
        
        # 计算价格变化百分比
        trend_pct = ((new_price - old_price) / old_price) * 100
        
        import logging
        log = logging.getLogger("cost")
        log.info(f"计算最近{days}天大趋势: 从 ${old_price:.4f} 到 ${new_price:.4f}, 变化 {trend_pct:+.2f}%")
        
        return trend_pct
        
    except Exception as e:
        import logging
        log = logging.getLogger("cost")
        log.warning(f"计算15天大趋势失败: {e}")
        return None


def adjust_cost_by_trend(cost_price: float, trend_pct: Optional[float], max_adjustment_pct: float = 1.0) -> float:
    """
    根据大趋势调整成本价
    
    上涨趋势：成本价需要上调，因为上涨趋势中最低价格肯定超过了成本价，所以永远也买入不了。
    下跌趋势：成本价需要下调，因为下跌趋势中价格可能已经低于成本价很多，如果使用原始成本价，
              可能永远也卖不出（因为价格可能永远不会回到成本价以上）。
    
    Args:
        cost_price: 原始成本价
        trend_pct: 大趋势百分比（正数表示上涨，负数表示下跌）
        max_adjustment_pct: 最大调整幅度（默认1%），避免过度调整
    
    Returns:
        调整后的成本价
    """
    if cost_price <= 0 or trend_pct is None:
        return cost_price
    
    import logging
    log = logging.getLogger("cost")
    
    # 上涨趋势：上调成本价
    if trend_pct > 0:
        # 调整幅度 = min(趋势涨幅, 最大调整幅度)
        adjustment_pct = min(trend_pct, max_adjustment_pct)
        
        # 调整成本价：成本价 × (1 + 调整幅度)
        adjusted_cost = cost_price * (1 + adjustment_pct / 100)
        
        log.info(f"根据15天大趋势({trend_pct:+.2f}%)上调成本价: ${cost_price:.4f} → ${adjusted_cost:.4f} (调整幅度: +{adjustment_pct:.2f}%)")
        
        return adjusted_cost
    
    # 下跌趋势：下调成本价
    elif trend_pct < 0:
        # 调整幅度 = min(趋势跌幅绝对值, 最大调整幅度)
        adjustment_pct = min(abs(trend_pct), max_adjustment_pct)
        
        # 调整成本价：成本价 × (1 - 调整幅度)
        adjusted_cost = cost_price * (1 - adjustment_pct / 100)
        
        log.info(f"根据15天大趋势({trend_pct:+.2f}%)下调成本价: ${cost_price:.4f} → ${adjusted_cost:.4f} (调整幅度: -{adjustment_pct:.2f}%)")
        
        return adjusted_cost
    
    # 趋势为0或接近0，不调整
    return cost_price
