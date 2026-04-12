# -*- coding: utf-8 -*-
"""
统一工具脚本 - 合并所有检查工具
用法：
  python tools.py signal      # 查看当前信号
  python tools.py action       # 实时分析当前动作
  python tools.py price        # 查询实时价格
  python tools.py stoploss     # 检查止损逻辑
  python tools.py cost         # 计算成本价
  python tools.py costdetails  # 检查成本价详细计算
  python tools.py trades       # 查询交易记录
"""
import sys
import io
import json
import os
import argparse
from datetime import datetime
from typing import Optional

# 设置输出编码为UTF-8
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 导入必要的模块
from bybit_client import BybitClient
from bot_core import _get_balances, one_step_for_symbol
from strategy_v5 import SmartStrategy, MarketState, create_smart_strategy
from cost import get_spot_avg_cost_by_position, get_avg_sell_price
from indicators import enrich_indicators, klines_to_df
from db import recent_signals, recent_trades


def load_config() -> dict:
    """加载配置文件"""
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def create_client(cfg: dict) -> BybitClient:
    """创建Bybit客户端"""
    key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
    sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
    return BybitClient(
        api_key=key,
        api_secret=sec,
        testnet=cfg.get("testnet", False),
        account_type=cfg.get("account_type", "UNIFIED")
    )


def cmd_signal():
    """查看当前交易信号"""
    cfg = load_config()
    symbol = cfg.get("symbols", ["SOLUSDT"])[0]
    
    print("=" * 100)
    print(f"当前交易对: {symbol}")
    print("=" * 100)
    
    # 获取最近的信号
    signals = recent_signals(symbol, limit=5)
    if signals:
        print("\n【最近5个交易信号】")
        print("-" * 100)
        for i, s in enumerate(signals[:5], 1):
            ts = s.get('ts_ms', 0)
            dt = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S') if ts > 0 else "N/A"
            decision = s.get('decision', 'N/A')
            price = s.get('last_price', 0)
            cost = s.get('cost_price', 0)
            reason = s.get('reason', 'N/A')
            position_pct = s.get('position_pct', 0)
            confidence = s.get('confidence', 0)
            
            profit_pct = ((price - cost) / cost * 100) if cost > 0 else 0
            
            icon = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "⚪"
            print(f"{i}. {icon} {decision:4s} | 时间: {dt}")
            print(f"   价格: ${price:.4f} | 成本: ${cost:.4f} | 盈亏: {profit_pct:+.2f}%")
            print(f"   仓位: {position_pct:.1f}% | 信心: {confidence:.0f}%")
            print(f"   原因: {reason}")
            print()
        
        latest = signals[0]
        latest_decision = latest.get('decision', 'HOLD')
        latest_reason = latest.get('reason', '无信号')
        latest_price = latest.get('last_price', 0)
        
        print("\n" + "=" * 100)
        print("【当前应该执行的动作】")
        print("=" * 100)
        if latest_decision == "BUY":
            print(f"🟢 买入信号")
        elif latest_decision == "SELL":
            print(f"🔴 卖出信号")
        else:
            print(f"⚪ 持有（HOLD）")
        
        print(f"\n价格: ${latest_price:.4f}")
        print(f"原因: {latest_reason}")
        print(f"时间: {datetime.fromtimestamp(latest.get('ts_ms', 0) / 1000).strftime('%Y-%m-%d %H:%M:%S') if latest.get('ts_ms', 0) > 0 else 'N/A'}")
    else:
        print("\n⚠️ 没有找到交易信号记录")
    
    # 获取最近的交易
    print("\n" + "=" * 100)
    print("【最近5笔实际交易】")
    print("-" * 100)
    trades = recent_trades(limit=5)
    if trades:
        for i, t in enumerate(trades[:5], 1):
            if t.get('symbol') == symbol:
                ts = t.get('ts_ms', 0)
                dt = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S') if ts > 0 else "N/A"
                side = t.get('side', 'N/A')
                price = t.get('price', 0)
                qty = t.get('qty', 0)
                status = t.get('status', 'N/A')
                
                icon = "🟢" if side == "Buy" else "🔴"
                status_icon = "✅" if status == "OK" else "🔄" if status == "DRYRUN" else "❌"
                try:
                    price_float = float(price) if price else 0
                    qty_float = float(qty) if qty else 0
                    print(f"{i}. {icon} {side:4s} | {status_icon} {status:6s} | 价格: ${price_float:.4f} | 数量: {qty_float:.4f} | 时间: {dt}")
                except:
                    print(f"{i}. {icon} {side:4s} | {status_icon} {status:6s} | 价格: {price} | 数量: {qty} | 时间: {dt}")
    else:
        print("⚠️ 没有找到交易记录")
    
    print("\n" + "=" * 100)


def cmd_action():
    """基于实时价格分析当前应该执行的动作"""
    cfg = load_config()
    client = create_client(cfg)
    symbol = cfg.get("symbols", ["SOLUSDT"])[0]
    
    print("=" * 100)
    print(f"【实时分析】{symbol} 当前应该执行的动作")
    print("=" * 100)
    
    try:
        tkr = client.get_ticker(symbol)
        if tkr.get("retCode") == 0:
            result = tkr.get("result", {})
            list_data = result.get("list", [])
            if list_data:
                ticker = list_data[0]
                last_price = float(ticker.get("lastPrice", 0))
                print(f"\n【实时价格】")
                print(f"最新价: ${last_price:.4f}")
                print(f"24h最高: ${float(ticker.get('highPrice24h', 0)):.4f}")
                print(f"24h最低: ${float(ticker.get('lowPrice24h', 0)):.4f}")
                print(f"24h涨跌: {float(ticker.get('price24hPcnt', 0)) * 100:+.2f}%")
                
                base_bal, usdt_bal = _get_balances(client, symbol)
                total_value = base_bal * last_price + usdt_bal
                position_pct = (base_bal * last_price / total_value * 100) if total_value > 0 else 0
                usdt_pct = (usdt_bal / total_value * 100) if total_value > 0 else 0
                
                print(f"\n【账户状态】")
                print(f"SOL余额: {base_bal:.4f}")
                print(f"USDT余额: ${usdt_bal:.2f}")
                print(f"总资产: ${total_value:.2f}")
                print(f"仓位占比: {position_pct:.1f}%")
                print(f"USDT占比: {usdt_pct:.1f}%")
                
                cost_adjustment_factor = float(cfg.get("cost_adjustment_factor", 1.0))
                cost_price = get_spot_avg_cost_by_position(
                    client, symbol, usdt_bal, base_bal, last_price, history_days=60, cost_adjustment_factor=cost_adjustment_factor, limit=30
                )
                
                if cost_price and cost_price > 0:
                    profit_pct = ((last_price - cost_price) / cost_price * 100)
                    profit_usdt = base_bal * (last_price - cost_price)
                    print(f"\n【盈亏分析】")
                    print(f"成本价: ${cost_price:.4f}")
                    print(f"当前价: ${last_price:.4f}")
                    print(f"盈亏: {profit_pct:+.2f}%")
                    print(f"盈亏金额: ${profit_usdt:+.2f}")
                else:
                    print(f"\n【成本价】无法计算")
                
                print(f"\n【策略分析】")
                print("-" * 100)
                try:
                    snap = one_step_for_symbol(client, cfg, symbol)
                    decision = snap.get("decision", "HOLD")
                    reason = snap.get("reason", "无原因")
                    confidence = snap.get("confidence", 0)
                    position_pct_signal = snap.get("position_pct", 0)
                    
                    icon = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "⚪"
                    print(f"{icon} 决策: {decision}")
                    print(f"信心度: {confidence:.0f}%")
                    print(f"建议仓位: {position_pct_signal:.1f}%")
                    print(f"原因: {reason}")
                    
                    if snap.get("market_state"):
                        state = snap["market_state"]
                        print(f"\n【市场指标】")
                        if "rsi14" in state:
                            print(f"RSI14: {state.get('rsi14', 0):.1f}")
                        if "trend_score" in state:
                            print(f"趋势得分: {state.get('trend_score', 0):.1f}")
                        if "cost_price" in state:
                            print(f"策略成本价: ${state.get('cost_price', 0):.4f}")
                    
                except Exception as e:
                    print(f"策略分析失败: {e}")
                    import traceback
                    traceback.print_exc()
                
            else:
                print("❌ 未获取到ticker数据")
        else:
            print(f"❌ 获取ticker失败: {tkr.get('retMsg', 'Unknown error')}")
            
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 100)


def cmd_price():
    """查询实时价格"""
    cfg = load_config()
    client = create_client(cfg)
    symbol = cfg.get("symbols", ["SOLUSDT"])[0]
    
    print("=" * 80)
    print(f"查询 {symbol} 实时价格")
    print("=" * 80)
    
    try:
        tkr = client.get_ticker(symbol)
        if tkr.get("retCode") == 0:
            result = tkr.get("result", {})
            list_data = result.get("list", [])
            if list_data:
                ticker = list_data[0]
                last_price = float(ticker.get("lastPrice", 0))
                bid_price = float(ticker.get("bid1Price", 0))
                ask_price = float(ticker.get("ask1Price", 0))
                volume_24h = float(ticker.get("volume24h", 0))
                high_24h = float(ticker.get("highPrice24h", 0))
                low_24h = float(ticker.get("lowPrice24h", 0))
                change_24h = float(ticker.get("price24hPcnt", 0)) * 100
                
                print(f"\n【实时价格】")
                print(f"最新价: ${last_price:.4f}")
                print(f"买一价: ${bid_price:.4f}")
                print(f"卖一价: ${ask_price:.4f}")
                print(f"\n【24小时数据】")
                print(f"最高价: ${high_24h:.4f}")
                print(f"最低价: ${low_24h:.4f}")
                print(f"24h涨跌: {change_24h:+.2f}%")
                print(f"24h成交量: {volume_24h:,.0f}")
                
                print(f"\n【最近K线数据】")
                kl = client.get_kline(symbol=symbol, interval="60", limit=5)
                if kl.get("retCode") == 0:
                    klines = kl.get("result", {}).get("list", [])
                    if klines:
                        print(f"最近5根1小时K线:")
                        for i, k in enumerate(klines[-5:], 1):
                            open_price = float(k[1])
                            high_price = float(k[2])
                            low_price = float(k[3])
                            close_price = float(k[4])
                            volume = float(k[5])
                            timestamp = int(k[0])
                            dt = datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
                            change = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0
                            print(f"  {i}. {dt}")
                            print(f"     开: ${open_price:.4f} | 高: ${high_price:.4f} | 低: ${low_price:.4f} | 收: ${close_price:.4f} | 涨跌: {change:+.2f}% | 量: {volume:,.0f}")
        else:
            print(f"❌ 获取ticker失败: {tkr.get('retMsg', 'Unknown error')}")
            
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 80)


def cmd_stoploss():
    """检查止损逻辑是否应该触发"""
    cfg = load_config()
    client = create_client(cfg)
    symbol = cfg.get("symbols", ["SOLUSDT"])[0]
    
    print("=" * 100)
    print(f"【止损逻辑检查】{symbol}")
    print("=" * 100)
    
    try:
        tkr = client.get_ticker(symbol)
        last_price = float(tkr["result"]["list"][0]["lastPrice"])
        
        base_bal, usdt_bal = _get_balances(client, symbol)
        total_value = base_bal * last_price + usdt_bal
        position_pct = (base_bal * last_price / total_value * 100) if total_value > 0 else 0
        
        cost_adjustment_factor = float(cfg.get("cost_adjustment_factor", 1.0))
        cost_price = get_spot_avg_cost_by_position(
            client, symbol, usdt_bal, base_bal, last_price, history_days=60, cost_adjustment_factor=cost_adjustment_factor, limit=30
        )
        
        print(f"\n【当前状态】")
        print(f"价格: ${last_price:.4f}")
        print(f"成本价: ${cost_price:.4f}")
        print(f"仓位: {position_pct:.1f}%")
        
        if cost_price > 0:
            loss_pct = ((last_price - cost_price) / cost_price * 100)
            print(f"盈亏: {loss_pct:+.2f}%")
        
        # 获取K线数据
        kl = client.get_kline(symbol=symbol, interval="60", limit=72)
        if kl.get("retCode") == 0:
            klines = kl.get("result", {}).get("list", [])
            if klines:
                df = klines_to_df(klines)
                df = enrich_indicators(df)
                
                state = MarketState(
                    last_price=last_price,
                    rsi14=df['rsi14'].iloc[-1] if 'rsi14' in df.columns else 0,
                    trend_score=df['trend_score'].iloc[-1] if 'trend_score' in df.columns else 0,
                    cost_price=cost_price,
                    avg_sell_price=0,
                    usdt_pct=100 - position_pct,
                    recent_high=df['high'].max() if 'high' in df.columns else last_price,
                    recent_low=df['low'].min() if 'low' in df.columns else last_price,
                    long_term_trend_pct=0
                )
                
                strategy = create_smart_strategy(cfg)
                result = strategy._check_risks(state, position_pct, last_price, total_value)
                
                print(f"\n【止损检查结果】")
                if result.get("should_stop_loss"):
                    print(f"✅ 应该止损")
                    print(f"原因: {result.get('stop_loss_reason', '未知')}")
                else:
                    print(f"❌ 不应该止损")
                    if result.get("stop_loss_reason"):
                        print(f"原因: {result.get('stop_loss_reason')}")
        
    except Exception as e:
        print(f"❌ 检查失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 100)


def cmd_cost():
    """计算成本价"""
    cfg = load_config()
    client = create_client(cfg)
    symbol = cfg.get("symbols", ["SOLUSDT"])[0]
    history_days = 60  # 用于限制查询时间范围
    limit = 30  # 使用最近30次买入交易
    
    print(f"\n{'='*60}")
    print(f"计算 {symbol} 的成本价")
    print(f"{'='*60}\n")
    
    try:
        tkr = client.get_ticker(symbol)
        last_price = float(tkr["result"]["list"][0]["lastPrice"])
        print(f"当前价格: ${last_price:.4f}\n")
    except Exception as e:
        print(f"获取价格失败: {e}")
        last_price = 0.0
    
    try:
        base_bal, usdt_bal = _get_balances(client, symbol)
        print(f"SOL余额: {base_bal:.4f}")
        print(f"USDT余额: ${usdt_bal:.2f}\n")
    except Exception as e:
        print(f"获取余额失败: {e}")
        base_bal, usdt_bal = 0.0, 0.0
    
    cost_adjustment_factor = float(cfg.get("cost_adjustment_factor", 1.0))
    
    print(f"查询历史天数: {history_days} 天（时间范围限制）")
    print(f"使用交易次数: 最近 {limit} 次买入交易")
    print(f"成本价调整系数: {cost_adjustment_factor:.2f}")
    print(f"{'='*60}\n")
    
    cost_price = get_spot_avg_cost_by_position(
        client, symbol, usdt_bal, base_bal, last_price, history_days=60, cost_adjustment_factor=cost_adjustment_factor, limit=30
    )
    
    print(f"\n{'='*60}")
    if not (cost_price != cost_price or cost_price <= 0):
        print(f"最终成本价: ${cost_price:.4f}")
    else:
        print(f"成本价计算失败或无效")
    print(f"{'='*60}\n")


def cmd_costdetails():
    """检查成本价和卖出均价的详细计算"""
    cfg = load_config()
    client = create_client(cfg)
    symbol = cfg.get("symbols", ["SOLUSDT"])[0]
    
    print(f"\n{'='*60}")
    print(f"检查 {symbol} 的成本价和卖出均价")
    print(f"{'='*60}\n")
    
    try:
        tkr = client.get_ticker(symbol)
        last_price = float(tkr["result"]["list"][0]["lastPrice"])
        print(f"当前价格: ${last_price:.4f}\n")
    except Exception as e:
        print(f"获取价格失败: {e}")
        last_price = 0.0
    
    try:
        base_bal, usdt_bal = _get_balances(client, symbol)
        print(f"SOL余额: {base_bal:.4f}")
        print(f"USDT余额: ${usdt_bal:.2f}")
        
        total_value = base_bal * last_price + usdt_bal
        usdt_pct = (usdt_bal / total_value * 100) if total_value > 0 else 0
        base_pct = (base_bal * last_price / total_value * 100) if total_value > 0 else 0
        print(f"总资产: ${total_value:.2f}")
        print(f"USDT占比: {usdt_pct:.1f}%")
        print(f"SOL占比: {base_pct:.1f}%\n")
    except Exception as e:
        print(f"获取余额失败: {e}")
        base_bal, usdt_bal = 0.0, 0.0
        usdt_pct = 0.0
    
    cost_adjustment_factor = float(cfg.get("cost_adjustment_factor", 1.0))
    
    print(f"{'='*60}")
    print("1. 实际成本价（最近30次买入交易的加权平均）")
    print(f"{'='*60}")
    cost_price = get_spot_avg_cost_by_position(
        client, symbol, usdt_bal, base_bal, last_price, 
        history_days=60, cost_adjustment_factor=cost_adjustment_factor, limit=30
    )
    if not (cost_price != cost_price or cost_price <= 0):
        print(f"实际成本价: ${cost_price:.4f}")
        print(f"成本价调整系数: {cost_adjustment_factor:.2f}")
    else:
        print("实际成本价: 计算失败或无效")
    
    print(f"\n{'='*60}")
    print("2. 卖出均价（最近7天卖出记录的加权平均）")
    print(f"{'='*60}")
    avg_sell_price = get_avg_sell_price(client, symbol, limit=10, history_days=7)
    if avg_sell_price is not None and avg_sell_price > 0:
        print(f"卖出均价: ${avg_sell_price:.4f}")
    else:
        print("卖出均价: 无卖出记录或计算失败")
    
    print(f"\n{'='*60}")
    print("3. 参考成本价（用于买入判断）")
    print(f"{'='*60}")
    if usdt_pct > 60 and avg_sell_price is not None and avg_sell_price > 0:
        reference_cost = avg_sell_price
        print(f"USDT占比 {usdt_pct:.1f}% > 60%，使用卖出均价作为参考成本")
        print(f"参考成本价: ${reference_cost:.4f} (卖出均价)")
        if not (cost_price != cost_price or cost_price <= 0):
            print(f"实际成本价: ${cost_price:.4f}")
            diff = abs(cost_price - reference_cost)
            diff_pct = (diff / cost_price * 100) if cost_price > 0 else 0
            print(f"差异: ${diff:.4f} ({diff_pct:.2f}%)")
    else:
        if not (cost_price != cost_price or cost_price <= 0):
            reference_cost = cost_price
            print(f"使用实际成本价作为参考成本")
            print(f"参考成本价: ${reference_cost:.4f} (实际成本价)")
        else:
            reference_cost = 0.0
            print("无法确定参考成本价")
    
    if reference_cost > 0 and last_price > 0:
        price_diff_pct = (last_price - reference_cost) / reference_cost * 100
        print(f"\n{'='*60}")
        print("4. 当前价格相对于参考成本价")
        print(f"{'='*60}")
        print(f"当前价格: ${last_price:.4f}")
        print(f"参考成本价: ${reference_cost:.4f}")
        print(f"价格差异: {price_diff_pct:+.2f}%")
        if price_diff_pct > 0:
            print(f"当前价格高于参考成本价 {price_diff_pct:.2f}%")
        else:
            print(f"当前价格低于参考成本价 {abs(price_diff_pct):.2f}%")
    
    print(f"\n{'='*60}\n")


def cmd_trades():
    """查询交易记录"""
    symbol = "SOLUSDT"
    
    trades = recent_trades(100)
    signals = recent_signals(symbol, 200)
    
    print("=" * 80)
    print("最近交易记录（SOLUSDT）")
    print("=" * 80)
    for t in trades:
        if 'SOL' in t.get('symbol', ''):
            ts = t.get('ts_ms', 0)
            dt = datetime.fromtimestamp(ts / 1000) if ts > 0 else "N/A"
            price = t.get('price', 'N/A')
            side = t.get('side', 'N/A')
            reason = t.get('reason', 'N/A')
            print(f"时间: {dt} | 方向: {side:4s} | 价格: {price:>8s} | 数量: {t.get('qty', 'N/A'):>10s} | 原因: {reason}")
    
    print("\n" + "=" * 80)
    print("价格在132左右的信号记录")
    print("=" * 80)
    for s in signals:
        price = s.get('last_price', 0)
        if price > 0 and 130 <= price <= 135:
            ts = s.get('ts_ms', 0)
            dt = datetime.fromtimestamp(ts / 1000) if ts > 0 else "N/A"
            cost = s.get('cost_price', 0)
            decision = s.get('decision', 'N/A')
            reason = s.get('reason', 'N/A')
            rsi = s.get('rsi14', 0)
            profit_pct = ((price - cost) / cost * 100) if cost > 0 else 0
            print(f"时间: {dt} | 价格: {price:.4f} | 成本: {cost:.4f} | 盈利: {profit_pct:+.2f}% | RSI: {rsi:.1f} | 决策: {decision:4s} | 原因: {reason}")
    
    print("\n" + "=" * 80)
    print("最近的卖出交易（价格在132左右）")
    print("=" * 80)
    for t in trades:
        if 'SOL' in t.get('symbol', '') and t.get('side', '').upper() == 'SELL':
            price_str = t.get('price', '')
            try:
                price = float(price_str) if price_str else 0
                if 130 <= price <= 135:
                    ts = t.get('ts_ms', 0)
                    dt = datetime.fromtimestamp(ts / 1000) if ts > 0 else "N/A"
                    reason = t.get('reason', 'N/A')
                    print(f"时间: {dt} | 价格: {price:.4f} | 数量: {t.get('qty', 'N/A'):>10s} | 原因: {reason}")
            except:
                pass


def main():
    parser = argparse.ArgumentParser(description='统一工具脚本')
    parser.add_argument('command', choices=[
        'signal', 'action', 'price', 'stoploss', 'cost', 'costdetails', 'trades'
    ], help='要执行的命令')
    
    args = parser.parse_args()
    
    commands = {
        'signal': cmd_signal,
        'action': cmd_action,
        'price': cmd_price,
        'stoploss': cmd_stoploss,
        'cost': cmd_cost,
        'costdetails': cmd_costdetails,
        'trades': cmd_trades,
    }
    
    commands[args.command]()


if __name__ == '__main__':
    main()


