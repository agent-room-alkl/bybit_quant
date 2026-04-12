# -*- coding: utf-8 -*-
"""
交易逻辑模块 v2.0
包含：仓位管理、订单生成、风险控制
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import math
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def _to_float(value: Optional[str], default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


@dataclass
class InstrFilters:
    """交易对过滤器参数"""
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float


def parse_instr_filters(info_json: Dict[str, Any]) -> InstrFilters:
    """解析交易对信息"""
    result = info_json.get("result", {})
    rows = result.get("list", []) or []
    if not rows:
        return InstrFilters(0.00000001, 0.00000001, 0.0, 0.0)
    row = rows[0]
    price_filter = row.get("priceFilter", {}) or {}
    lot_filter = row.get("lotSizeFilter", {}) or {}
    tick = _to_float(price_filter.get("tickSize"), 0.00000001)
    qty_step = _to_float(lot_filter.get("qtyStep"), 0.00000001)
    min_qty = _to_float(lot_filter.get("minOrderQty"), 0.0)
    min_notional = _to_float(lot_filter.get("minNotionalValue"), 0.0)
    return InstrFilters(tick, qty_step, min_qty, min_notional)


def round_to_step(x: float, step: float, mode: str = "floor") -> float:
    """将数值按步长取整"""
    if step <= 0:
        return x
    n = x / step
    if mode == "ceil":
        n = math.ceil(n - 1e-12)
    elif mode == "round":
        n = round(n)
    else:
        n = math.floor(n + 1e-12)
    return max(0.0, n * step)


def make_price_for_sell(bid1: float, tick: float) -> float:
    """生成卖出价格（略低于bid1以确保成交）"""
    return round_to_step(bid1 - tick, tick, mode="floor")


def make_price_for_buy(ask1: float, tick: float) -> float:
    """生成买入价格（略高于ask1以确保成交）"""
    return round_to_step(ask1 + tick, tick, mode="ceil")


@dataclass
class PositionInfo:
    """仓位信息"""
    base_balance: float  # 基础币余额
    usdt_balance: float  # USDT余额
    total_value_usdt: float  # 总价值(USDT计)
    base_value_usdt: float  # 基础币价值(USDT计)
    position_pct: float  # 仓位百分比 (0-100)


def calculate_position(base_balance: float, usdt_balance: float, last_price: float) -> PositionInfo:
    """计算当前仓位"""
    if last_price <= 0:
        return PositionInfo(base_balance, usdt_balance, usdt_balance, 0, 0)
    
    base_value = base_balance * last_price
    total_value = base_value + usdt_balance
    
    if total_value <= 0:
        return PositionInfo(base_balance, usdt_balance, 0, 0, 0)
    
    position_pct = (base_value / total_value) * 100
    
    return PositionInfo(
        base_balance=base_balance,
        usdt_balance=usdt_balance,
        total_value_usdt=total_value,
        base_value_usdt=base_value,
        position_pct=position_pct
    )


def calculate_trade_qty(
    action: str,
    position_info: PositionInfo,
    trade_pct: float,
    last_price: float,
    instr: InstrFilters,
    min_usdt_per_trade: float = 5.0,
    leverage: float = 1.0
) -> Tuple[float, str]:
    """
    计算交易数量
    
    Args:
        action: BUY 或 SELL
        position_info: 仓位信息
        trade_pct: 交易比例 (占总资产的百分比，0-100)
        last_price: 最新价格
        instr: 交易对过滤器
        min_usdt_per_trade: 最小交易金额
        leverage: 杠杆倍数（默认1.0，不使用杠杆）
    
    Returns:
        (交易数量, 原因说明)
    """
    if last_price <= 0:
        return 0.0, "价格无效"
    
    total_value = position_info.total_value_usdt
    trade_value = total_value * (trade_pct / 100)
    
    if action == "BUY":
        # 买入：用USDT买入基础币
        # trade_value 是策略期望的目标买入金额（SOL价值）
        # 有杠杆时，只需 trade_value/leverage 的自有USDT，杠杆借款补足差额
        # 这样买入的SOL金额 = (自有USDT) * leverage = trade_value → 与卖出对称
        margin_needed = trade_value / leverage if leverage > 1 else trade_value
        available = min(position_info.usdt_balance, margin_needed)

        if available < min_usdt_per_trade:
            return 0.0, f"可用USDT({available:.2f})不足最小交易金额({min_usdt_per_trade})"

        # 使用杠杆：实际下单数量 = 可用资金 * 杠杆倍数 / 价格
        effective_value = available * leverage
        qty = effective_value / last_price
        qty = round_to_step(qty, instr.qty_step, mode="floor")
        
        if qty < instr.min_qty:
            return 0.0, f"数量({qty:.8f})低于最小下单量({instr.min_qty})"
        
        notional = qty * last_price
        if notional < instr.min_notional:
            return 0.0, f"金额({notional:.2f})低于最小名义金额({instr.min_notional})"
        
        leverage_text = f"({leverage}x杠杆)" if leverage > 1.0 else ""
        return qty, f"买入{qty:.6f}(约{notional:.2f}USDT{leverage_text})"
    
    else:  # SELL
        # 卖出：卖出基础币换取USDT
        # trade_pct是占总资产的比例，转换为占持仓的比例
        if position_info.base_value_usdt > 0:
            sell_value = min(position_info.base_value_usdt, trade_value)
            qty = sell_value / last_price
        else:
            return 0.0, "无持仓可卖"
        
        qty = round_to_step(qty, instr.qty_step, mode="floor")
        
        if qty < instr.min_qty:
            return 0.0, f"数量({qty:.8f})低于最小下单量({instr.min_qty})"
        
        # 确保不超过实际持仓
        qty = min(qty, position_info.base_balance)
        qty = round_to_step(qty, instr.qty_step, mode="floor")
        
        notional = qty * last_price
        if notional < instr.min_notional:
            return 0.0, f"金额({notional:.2f})低于最小名义金额({instr.min_notional})"
        
        return qty, f"卖出{qty:.6f}(约{notional:.2f}USDT)"


def suggest_action(
    symbol: str,
    last_price: float,
    cost_price: float,
    base_balance: float,
    usdt_balance: float,
    instr: InstrFilters,
    orderbook_best: Tuple[Optional[float], Optional[float]],
    buy_pct_of_usdt: float = 0.10,
    sell_pct_of_base: float = 0.20,
    min_usdt_per_buy: float = 5.0
) -> Dict[str, Any]:
    """
    生成交易建议（保留原接口兼容性）
    """
    bid1, ask1 = orderbook_best
    tick = instr.tick_size or 0.00000001

    logger.debug(
        f"[{symbol}] last={last_price}, cost={cost_price}, base_bal={base_balance}, "
        f"usdt_bal={usdt_balance}, bid1={bid1}, ask1={ask1}"
    )

    if cost_price <= 0 or last_price <= 0:
        reason = "无效的成本价或最新价"
        logger.warning(f"[{symbol}] HOLD: {reason}")
        return {"action": "HOLD", "order": None, "reason": reason}

    # 计算仓位
    position = calculate_position(base_balance, usdt_balance, last_price)

    # 买入逻辑 - 价格低于成本
    if last_price < cost_price and usdt_balance > min_usdt_per_buy:
        budget = usdt_balance * max(0.0, min(1.0, buy_pct_of_usdt))
        logger.info(f"[{symbol}] 尝试买入: budget={budget:.4f}, usdt_bal={usdt_balance:.4f}, pct={buy_pct_of_usdt}")
        
        if budget < min_usdt_per_buy:
            reason = "可用USDT不足最小买入金额门槛"
            logger.info(f"[{symbol}] HOLD: {reason}")
            return {"action": "HOLD", "order": None, "reason": reason}
        
        qty = round_to_step(budget / last_price, instr.qty_step, mode="floor")
        
        if qty < instr.min_qty:
            reason = "数量低于最小下单量"
            logger.info(f"[{symbol}] HOLD: {reason}, qty={qty}")
            return {"action": "HOLD", "order": None, "reason": reason}
        
        notional = qty * last_price
        if notional < instr.min_notional:
            reason = "名义金额低于最小下单额"
            logger.info(f"[{symbol}] HOLD: {reason}, notional={notional}")
            return {"action": "HOLD", "order": None, "reason": reason}
        
        price = last_price if ask1 is None else make_price_for_buy(ask1, tick)
        qty = round(qty, 8)
        order = {
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": f"{qty:.18f}".rstrip("0").rstrip("."),
            "price": f"{price:.18f}".rstrip("0").rstrip("."),
            "timeInForce": "IOC"
        }
        logger.info(f"[{symbol}] BUY 下单: {order}")
        return {"action": "BUY", "order": order, "reason": f"现价低于成本价{((cost_price-last_price)/cost_price*100):.2f}%，考虑逢低加仓（IOC 限价）"}

    # 卖出逻辑 - 价格高于成本
    if last_price > cost_price and base_balance * sell_pct_of_base > 0:
        sell_qty = base_balance * max(0.0, min(1.0, sell_pct_of_base))
        sell_qty = round_to_step(sell_qty, instr.qty_step, mode="floor")
        sell_qty = round(sell_qty, 8)
        logger.info(f"[{symbol}] 尝试卖出: sell_qty={sell_qty:.8f}, base_bal={base_balance:.8f}, pct={sell_pct_of_base}")
        
        if sell_qty < instr.min_qty:
            reason = "可卖数量低于最小下单量"
            logger.info(f"[{symbol}] HOLD: {reason}")
            return {"action": "HOLD", "order": None, "reason": reason}
        
        price = last_price if bid1 is None else make_price_for_sell(bid1, tick)
        order = {
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Limit",
            "qty": f"{sell_qty:.18f}".rstrip("0").rstrip("."),
            "price": f"{price:.18f}".rstrip("0").rstrip("."),
            "timeInForce": "IOC"
        }
        logger.info(f"[{symbol}] SELL 下单: {order}")
        return {"action": "SELL", "order": order, "reason": f"现价高于成本价{((last_price-cost_price)/cost_price*100):.2f}%，分批止盈（IOC 限价）"}

    reason = "未触发买卖条件"
    logger.debug(f"[{symbol}] HOLD: {reason}")
    return {"action": "HOLD", "order": None, "reason": reason}


def generate_order(
    symbol: str,
    action: str,
    qty: float,
    last_price: float,
    orderbook_best: Tuple[Optional[float], Optional[float]],
    instr: InstrFilters,
    order_type: str = "Limit",
    time_in_force: str = "IOC"
) -> Optional[Dict[str, Any]]:
    """
    生成订单
    
    Args:
        symbol: 交易对
        action: BUY 或 SELL
        qty: 交易数量
        last_price: 最新价格
        orderbook_best: (bid1, ask1)
        instr: 交易对过滤器
        order_type: 订单类型
        time_in_force: 有效期类型
    
    Returns:
        订单字典或None
    """
    if qty <= 0:
        return None
    
    bid1, ask1 = orderbook_best
    tick = instr.tick_size or 0.00000001
    
    qty = round_to_step(qty, instr.qty_step, mode="floor")
    
    if qty < instr.min_qty:
        logger.warning(f"[{symbol}] 订单数量{qty}低于最小{instr.min_qty}")
        return None
    
    if action == "BUY":
        price = last_price if ask1 is None else make_price_for_buy(ask1, tick)
        side = "Buy"
    else:
        price = last_price if bid1 is None else make_price_for_sell(bid1, tick)
        side = "Sell"
    
    notional = qty * price
    # 强制最低金额: max(交易所最低, $5) — 防止 "Order value exceeded lower limit" 错误
    hard_min_notional = max(instr.min_notional, 5.0)
    if notional < hard_min_notional:
        logger.debug(f"[{symbol}] 订单金额${notional:.2f}低于最小${hard_min_notional:.2f}，跳过")
        return None
    
    # 格式化数量：根据qty_step确定小数位数（防止 "too many decimals" 错误）
    step = instr.qty_step if instr.qty_step > 0 else 0.0001
    step_str = f"{step:.10f}".rstrip("0")
    decimals = len(step_str.split(".")[-1]) if "." in step_str else 0
    decimals = min(max(decimals, 2), 4)  # 至少2位，最多4位（Bybit SOLUSDT限制）
    qty_str = f"{qty:.{decimals}f}"
    
    # 格式化价格：通常2位小数足够
    price_str = f"{price:.2f}".rstrip("0").rstrip(".")
    if not price_str or price_str == '':
        price_str = f"{price:.2f}"
    
    order = {
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": qty_str,
        "price": price_str,
        "timeInForce": time_in_force
    }
    
    logger.info(f"[{symbol}] 生成订单: {side} {qty:.8f} @ {price:.8f}")
    return order


class RiskManager:
    """风险管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.max_daily_loss_pct = float(config.get("max_daily_loss_pct", 5.0))  # 日最大亏损
        self.max_position_pct = float(config.get("max_position_pct", 90))  # 最大仓位
        self.min_position_pct = float(config.get("min_position_pct", 10))  # 最小仓位
        self.max_single_trade_pct = float(config.get("max_single_trade_pct", 20))  # 单次最大交易
        self.cooldown_after_loss_min = int(config.get("cooldown_after_loss_min", 30))  # 亏损后冷却
        
    def check_can_trade(
        self,
        action: str,
        position_pct: float,
        daily_pnl_pct: float,
        minutes_since_last_loss: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        检查是否可以交易
        
        Returns:
            (是否可以, 原因)
        """
        # 日亏损检查
        if daily_pnl_pct < -self.max_daily_loss_pct:
            return False, f"日亏损({daily_pnl_pct:.1f}%)超过限制({self.max_daily_loss_pct}%)"
        
        # 仓位检查
        if action == "BUY":
            if position_pct >= self.max_position_pct:
                return False, f"仓位({position_pct:.1f}%)已达上限({self.max_position_pct}%)"
        else:  # SELL
            if position_pct <= self.min_position_pct:
                return False, f"仓位({position_pct:.1f}%)已达下限({self.min_position_pct}%)"
        
        # 亏损冷却检查
        if minutes_since_last_loss is not None:
            if minutes_since_last_loss < self.cooldown_after_loss_min:
                return False, f"亏损后冷却中({minutes_since_last_loss}分钟<{self.cooldown_after_loss_min}分钟)"
        
        return True, "通过风控检查"
    
    def adjust_trade_size(
        self,
        proposed_pct: float,
        action: str,
        position_pct: float,
        volatility: float
    ) -> float:
        """
        根据风险调整交易规模
        
        Args:
            proposed_pct: 建议的交易比例
            action: BUY 或 SELL
            position_pct: 当前仓位百分比
            volatility: 当前波动率
        
        Returns:
            调整后的交易比例
        """
        # 不超过单次最大交易
        adjusted = min(proposed_pct, self.max_single_trade_pct)
        
        # 根据波动率调整（高波动率降低交易量）
        if volatility > 0.05:  # 5%日波动率
            vol_factor = max(0.5, 1 - (volatility - 0.05) * 5)
            adjusted *= vol_factor
        
        # 确保不会超过仓位限制
        if action == "BUY":
            max_allowed = self.max_position_pct - position_pct
            adjusted = min(adjusted, max(0, max_allowed))
        else:
            max_allowed = position_pct - self.min_position_pct
            adjusted = min(adjusted, max(0, max_allowed))
        
        return max(0, adjusted)
