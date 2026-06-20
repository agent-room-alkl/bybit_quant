# -*- coding: utf-8 -*-
"""
智能量化交易系统 - 一体化启动程序
同时运行：自动交易 + Web看板

启动命令：python main.py
访问地址：http://localhost:5000
"""
import os
import json
import time
import threading
import datetime as dt
import logging
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
try:
    import jwt
except ImportError:
    import logging
    logging.warning("PyJWT not installed, JWT features will be disabled. Install with: pip install PyJWT")
    jwt = None
import hashlib
from datetime import datetime, timedelta

from bybit_client import BybitClient
from bot_core import one_step_for_symbol, _get_daily_volume, _get_daily_buy_volume, _get_daily_sell_volume, _nz_today_str
from indicators import klines_to_df, enrich_indicators, get_market_condition
from db import init_db, recent_trades, recent_signals, get_meta, set_meta
from cost import get_spot_avg_cost
import time as _time


def get_daily_volume(symbol: str) -> float:
    """获取今日交易额（与bot_core保持一致，按新西兰日期统计）"""
    return _get_daily_volume(symbol)

# ============== 日志配置 ==============
os.makedirs("logs", exist_ok=True)

def setup_logger():
    """配置日志系统"""
    logger = logging.getLogger("quant")
    logger.setLevel(logging.INFO)
    
    # 避免重复添加handler
    if logger.handlers:
        return logger
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-5s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出（自动轮转，最大5MB，保留5个备份）
    file_handler = RotatingFileHandler(
        'logs/trading.log',
        maxBytes=5*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 错误日志单独记录
    error_handler = RotatingFileHandler(
        'logs/error.log',
        maxBytes=5*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    return logger

log = setup_logger()

# ============== JWT认证配置 ==============
JWT_SECRET = "bybit_quant_secret_key_2024"  # 生产环境应该使用环境变量
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# 用户数据（实际应该存储在数据库中，这里简化处理）
USERS_DB = {
    "admin": {
        "username": "admin",
        "password_hash": hashlib.sha256("Ebin@2021".encode()).hexdigest(),  # 密码：Ebin@2021
        "created_at": datetime.now().isoformat()
    }
}

security = HTTPBearer()

def create_jwt_token(username: str) -> str:
    """创建JWT token"""
    if jwt is None:
        raise HTTPException(status_code=500, detail="JWT library not installed")
    payload = {
        "username": username,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_jwt_token(token: str) -> Optional[Dict]:
    """验证JWT token"""
    if jwt is None:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict:
    """获取当前用户"""
    token = credentials.credentials
    payload = verify_jwt_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username = payload.get("username")
    if username not in USERS_DB:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return {"username": username}

def verify_password(password: str, password_hash: str) -> bool:
    """验证密码"""
    return hashlib.sha256(password.encode()).hexdigest() == password_hash

# ============== 全局状态 ==============
app = FastAPI(title="智能量化交易系统")
trading_status = {
    "running": False,
    "last_update": None,
    "cycle_count": 0,
    "last_snapshot": {},
    "errors": []
}

# ============== 加载配置 ==============
def load_config(path="config.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

cfg = load_config()

# ============== 交易线程 ==============
def create_client():
    """创建API客户端，支持重连"""
    key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
    sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
    return BybitClient(
        api_key=key,
        api_secret=sec,
        testnet=cfg.get("testnet", False),
        account_type=cfg.get("account_type", "UNIFIED")
    )

def trading_loop():
    """后台交易循环 - 增强稳定性版本"""
    global trading_status
    
    poll = int(cfg.get("auto", {}).get("interval_sec", 120))
    symbols = cfg.get("symbols", [])
    
    log.info(f"交易引擎启动 | 币种: {symbols} | 间隔: {poll}秒")
    trading_status["running"] = True
    
    client = None
    consecutive_errors = 0
    max_consecutive_errors = 10  # 连续错误超过10次，重建客户端
    
    while trading_status["running"]:
        try:
            # 如果客户端不存在或连续错误过多，重建客户端
            if client is None or consecutive_errors >= max_consecutive_errors:
                if consecutive_errors >= max_consecutive_errors:
                    log.warning(f"连续错误 {consecutive_errors} 次，重建API客户端...")
                client = create_client()
                consecutive_errors = 0
            
            trading_status["cycle_count"] += 1
            trading_status["last_update"] = dt.datetime.now().isoformat()
            
            cycle_has_error = False
            
            for sym in symbols:
                try:
                    snap = one_step_for_symbol(client, cfg, sym)
                    trading_status["last_snapshot"][sym] = snap
                    
                    decision = snap.get("decision", "HOLD")
                    price = snap.get("last_price", 0)
                    reason = snap.get("reason", '')[:60]
                    
                    if decision == "BUY":
                        log.info(f"[{sym}] 🟢 BUY  | 价格: ${price:.4f} | {reason}")
                    elif decision == "SELL":
                        log.info(f"[{sym}] 🔴 SELL | 价格: ${price:.4f} | {reason}")
                    else:
                        log.debug(f"[{sym}] ⚪ HOLD | 价格: ${price:.4f} | {reason}")
                    
                    # 如果有交易执行，额外记录
                    if snap.get("placed"):
                        status = snap["placed"].get("status", "")
                        if status == "OK":
                            log.info(f"[{sym}] ✅ 交易执行成功")
                        elif status == "DRYRUN":
                            log.info(f"[{sym}] 🔄 模拟交易（未实际下单）")
                        else:
                            log.warning(f"[{sym}] ⚠️ 交易执行异常: {status}")
                    
                except Exception as e:
                    cycle_has_error = True
                    error_msg = f"[{sym}] 执行错误: {str(e)}"
                    log.error(error_msg, exc_info=True)
                    trading_status["errors"].append({
                        "time": dt.datetime.now().isoformat(),
                        "message": error_msg
                    })
                    # 只保留最近10条错误
                    if len(trading_status["errors"]) > 10:
                        trading_status["errors"] = trading_status["errors"][-10:]
            
            # 更新连续错误计数
            if cycle_has_error:
                consecutive_errors += 1
            else:
                consecutive_errors = 0
            
            # 正常睡眠
            time.sleep(poll)
            
        except KeyboardInterrupt:
            log.info("收到中断信号，正在停止...")
            trading_status["running"] = False
            break
        except Exception as e:
            # 捕获外层异常，防止线程崩溃
            consecutive_errors += 1
            error_msg = f"交易循环异常: {str(e)}"
            log.error(error_msg, exc_info=True)
            trading_status["errors"].append({
                "time": dt.datetime.now().isoformat(),
                "message": error_msg
            })
            if len(trading_status["errors"]) > 10:
                trading_status["errors"] = trading_status["errors"][-10:]
            
            # 发生异常后等待更长时间再重试
            wait_time = min(poll * 2, 300)  # 最多等5分钟
            log.info(f"等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
    
    log.info("交易引擎已停止")

# 通用的JSON安全转换：处理 NaN/Inf，避免 Out of range float JSON 错误
def make_json_serializable(obj):
    """递归地将对象转换为可JSON序列化的格式"""
    import math
    if obj is None:
        return None
    elif isinstance(obj, float):
        # 处理NaN和Inf值
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    elif isinstance(obj, (str, int, bool)):
        return obj
    elif isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [make_json_serializable(item) for item in obj]
    else:
        # 对于其他类型，尝试转换为字符串
        return str(obj)

# ============== API接口 ==============
@app.get("/api/status")
async def get_status():
    try:
        import json
        
        # 确保所有值都可以序列化为JSON
        status_copy = {
            "running": trading_status.get("running", False),
            "last_update": trading_status.get("last_update"),
            "cycle_count": trading_status.get("cycle_count", 0),
            "last_snapshot": make_json_serializable(trading_status.get("last_snapshot", {})),
            "errors": trading_status.get("errors", [])
        }
        
        return JSONResponse(status_copy)
    except Exception as e:
        log.error(f"获取状态信息失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/static/chart.min.js")
async def serve_chartjs():
    """本地提供 Chart.js"""
    chart_path = os.path.join(os.path.dirname(__file__), "data", "chart.min.js")
    if os.path.exists(chart_path):
        from fastapi.responses import FileResponse
        return FileResponse(chart_path, media_type="application/javascript")
    return JSONResponse({"error": "not found"}, status_code=404)

@app.get("/api/check_leverage")
async def check_leverage_status():
    """检查账户是否已开通杠杆交易"""
    try:
        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        client = BybitClient(api_key=key, api_secret=sec,
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        
        # 方法1: 查询账户信息
        account_info = client.get_account_info()
        account_ok = account_info.get("retCode") == 0
        
        # 方法2: 查询钱包余额（检查杠杆相关字段）
        balance = client.get_wallet_balance()
        balance_ok = balance.get("retCode") == 0
        
        leverage_detected = False
        leverage_fields = []
        
        if balance_ok:
            result = balance.get("result", {})
            list_data = result.get("list", [])
            if list_data:
                account = list_data[0]
                coins = account.get("coin", [])
                if coins:
                    first_coin = coins[0]
                    # 检查杠杆相关字段
                    if "borrowAmount" in first_coin:
                        leverage_detected = True
                        leverage_fields.append("borrowAmount")
                    if "availableToBorrow" in first_coin:
                        leverage_detected = True
                        leverage_fields.append("availableToBorrow")
                    if "accruedInterest" in first_coin:
                        leverage_detected = True
                        leverage_fields.append("accruedInterest")
        
        # 判断结果
        status = "unknown"
        message = ""
        
        if leverage_detected:
            status = "enabled"
            message = f"✅ 账户可能已开通杠杆交易（检测到字段: {', '.join(leverage_fields)}）"
        elif account_ok and balance_ok:
            status = "maybe_disabled"
            message = "⚠️ 未检测到杠杆相关字段，账户可能未开通杠杆交易"
        else:
            status = "error"
            error_msg = account_info.get("retMsg") or balance.get("retMsg") or "未知错误"
            message = f"❌ 查询失败: {error_msg}"
        
        return JSONResponse({
            "status": status,
            "message": message,
            "account_info_ok": account_ok,
            "balance_ok": balance_ok,
            "leverage_detected": leverage_detected,
            "leverage_fields": leverage_fields,
            "manual_check_url": "https://www.bybit.com/trade/spot/SOL/USDT"
        })
    except Exception as e:
        log.error(f"检查杠杆状态失败: {e}", exc_info=True)
        return JSONResponse({
            "status": "error",
            "message": f"检查失败: {str(e)}"
        }, status_code=500)

@app.get("/api/config")
async def get_config():
    """获取配置信息"""
    try:
        return JSONResponse({
            "cost_adjustment_factor": float(cfg.get("cost_adjustment_factor", 1.0)),
            "leverage_enabled": bool(cfg.get("risk", {}).get("leverage_enabled", False)),
            "leverage": float(cfg.get("risk", {}).get("leverage", 1.0)),
            "max_daily_volume_pct": float(cfg.get("risk", {}).get("max_daily_volume_pct", 90.0))
        })
    except Exception as e:
        log.error(f"获取配置失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# v4.4: portfolio缓存, 避免每次浏览器请求都打Bybit API
_portfolio_cache = {"data": None, "ts": 0}
_PORTFOLIO_CACHE_TTL = 8  # 缓存8秒 (前端10秒轮询一次)
_klines_cache = {"data": None, "ts": 0}
_KLINES_CACHE_TTL = 30  # K线缓存30秒（1H K线变化慢）
_indicators_cache = {"data": None, "ts": 0}
_INDICATORS_CACHE_TTL = 30

@app.get("/api/portfolio")
async def get_portfolio():
    import time as _t
    now = _t.time()
    if _portfolio_cache["data"] is not None and (now - _portfolio_cache["ts"]) < _PORTFOLIO_CACHE_TTL:
        return JSONResponse(_portfolio_cache["data"])

    try:
        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        
        if not key or not sec:
            log.error("API密钥未配置")
            return JSONResponse({"error": "API密钥未配置"}, status_code=500)
        
        client = BybitClient(api_key=key, api_secret=sec, 
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        base_coin = symbol.replace("USDT", "")
        
        # 获取余额（统一使用 _get_balances 函数）
        try:
            from bot_core import _get_balances, _get_total_equity
            base_bal, usdt_bal = _get_balances(client, symbol)
            if base_bal is None or usdt_bal is None:
                log.error(f"获取余额返回None: base_bal={base_bal}, usdt_bal={usdt_bal}")
                base_bal = 0.0
                usdt_bal = 0.0
            else:
                log.debug(f"获取余额成功: {base_coin}={base_bal:.4f}, USDT={usdt_bal:.2f}")
        except Exception as e:
            log.error(f"获取余额异常: {e}", exc_info=True)
            base_bal = 0.0
            usdt_bal = 0.0
        
        # 获取总资产（优先使用API返回的totalEquity，确保与手机端一致）
        try:
            total_equity = _get_total_equity(client)
            if total_equity is None:
                # 如果API获取失败，使用计算值作为后备
                log.debug("无法从API获取总资产，使用计算值")
                total_equity = None
        except Exception as e:
            log.warning(f"获取总资产异常: {e}")
            total_equity = None
        
        # 获取价格
        try:
            tkr = client.get_ticker(symbol)
            if not tkr or "result" not in tkr or "list" not in tkr["result"] or len(tkr["result"]["list"]) == 0:
                log.error(f"获取价格失败: {tkr}")
                return JSONResponse({"error": f"获取价格失败: {tkr}"}, status_code=500)
            ticker_data = tkr["result"]["list"][0]
            last_price = float(ticker_data.get("lastPrice", 0))
            price_change = float(ticker_data.get("price24hPcnt", 0) or 0) * 100
            high_24h = float(ticker_data.get("highPrice24h", 0) or 0)
            low_24h = float(ticker_data.get("lowPrice24h", 0) or 0)
            volume_24h = float(ticker_data.get("volume24h", 0) or 0)
        except Exception as e:
            log.error(f"获取价格异常: {e}", exc_info=True)
            return JSONResponse({"error": f"获取价格异常: {str(e)}"}, status_code=500)
        
        # 获取成本价（FIFO）
        try:
            from cost import get_cost_price
            import math
            cost_price = get_cost_price(client, symbol, base_bal, history_days=60)
            if cost_price is None or (isinstance(cost_price, float) and (math.isnan(cost_price) or cost_price <= 0)):
                cost_price = 0.0
        except Exception as e:
            log.warning(f"获取FIFO成本价失败: {e}")
            cost_price = 0.0
        
        pnl_pct = (last_price - cost_price) / cost_price * 100 if cost_price > 0 else 0
        pnl_usdt = base_bal * (last_price - cost_price) if cost_price > 0 else 0
        
        # 优先使用API返回的总资产（与手机端一致），如果获取失败则使用计算值
        if total_equity is not None:
            total_value = total_equity
        else:
            total_value = usdt_bal + base_bal * last_price
        
        position_pct = (base_bal * last_price / total_value * 100) if total_value > 0 else 0
        
        # 获取今日交易额度信息（按百分比计算，只限制买入总额）
        try:
            net_daily_volume = get_daily_volume(symbol)  # 净交易额 = 买入 - 卖出（用于显示）
            buy_daily_volume = _get_daily_buy_volume(symbol)  # 买入总额（用于限制）
            sell_daily_volume = _get_daily_sell_volume(symbol)  # 卖出总额（用于显示）
        except Exception as e:
            log.warning(f"获取每日交易额失败: {e}")
            net_daily_volume = 0.0
            buy_daily_volume = 0.0
            sell_daily_volume = 0.0
        
        max_daily_volume_pct = float(cfg.get("risk", {}).get("max_daily_volume_pct", 90.0))
        # 按总资产百分比计算最大买入总额（只限制买入，不限制卖出）
        max_daily_volume_usdt = total_value * (max_daily_volume_pct / 100.0) if max_daily_volume_pct > 0 and total_value > 0 else 0
        # 剩余额度 = 最大限额 - 当前买入总额（只有买入会增加买入总额，卖出不影响）
        remaining_volume = max(0, max_daily_volume_usdt - buy_daily_volume) if max_daily_volume_usdt > 0 else -1
        
        # 获取杠杆倍数（未启用时强制1.0）
        leverage_enabled = bool(cfg.get("risk", {}).get("leverage_enabled", False))
        leverage = float(cfg.get("risk", {}).get("leverage", 1.0)) if leverage_enabled else 1.0

        # 清理NaN和Inf值，使其可以JSON序列化
        import math
        def clean_float(v):
            if isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    return 0.0
            return v
        
        result = {
            "symbol": symbol,
            "base_coin": base_coin,
            "last_price": clean_float(last_price),
            "price_change_24h": clean_float(price_change),
            "high_24h": clean_float(high_24h),
            "low_24h": clean_float(low_24h),
            "volume_24h": clean_float(volume_24h),
            "cost_price": clean_float(cost_price),
            "base_balance": clean_float(base_bal),
            "usdt_balance": clean_float(usdt_bal),
            "total_value": clean_float(total_value),
            "pnl_pct": clean_float(pnl_pct),
            "pnl_usdt": clean_float(pnl_usdt),
            "position_pct": clean_float(position_pct),
            "daily_volume": clean_float(net_daily_volume),  # 净交易额（买入-卖出）
            "daily_buy_volume": clean_float(buy_daily_volume),  # 买入总额
            "daily_sell_volume": clean_float(sell_daily_volume),  # 卖出总额
            "max_daily_volume_pct": clean_float(max_daily_volume_pct),
            "max_daily_volume_usdt": clean_float(max_daily_volume_usdt),
            "remaining_volume": clean_float(remaining_volume),
            "leverage": clean_float(leverage)
        }
        
        # v4.4: 写入缓存
        _portfolio_cache["data"] = result
        _portfolio_cache["ts"] = _t.time()
        return JSONResponse(result)
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_trace = traceback.format_exc()
        log.error(f"获取投资组合信息失败: {error_msg}\n{error_trace}")
        return JSONResponse({
            "error": error_msg,
            "type": type(e).__name__
        }, status_code=500)

@app.get("/api/klines")
async def get_klines():
    """获取K线数据用于图表"""
    import time as _t
    now = _t.time()
    if _klines_cache["data"] is not None and (now - _klines_cache["ts"]) < _KLINES_CACHE_TTL:
        return JSONResponse(_klines_cache["data"])
    try:
        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        client = BybitClient(api_key=key, api_secret=sec,
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        
        kl = client.get_kline(symbol=symbol, interval="60", limit=72)
        df = enrich_indicators(klines_to_df(kl))
        
        if df is None or df.empty:
            log.warning("K线数据为空")
            return JSONResponse({"error": "No data"}, status_code=500)
        
        # 填充NaN值，确保图表能正常显示
        df = df.fillna(method='ffill').fillna(method='bfill')
        
        # 安全转换函数：处理NaN和无穷大
        def safe_list(series):
            return [float(x) if x == x and abs(x) != float('inf') else 0 for x in series]
        
        # 转换为图表数据
        data = {
            "labels": df["startTime"].dt.strftime("%H:%M").tolist(),
            "prices": safe_list(df["close"]),
            "sma24": safe_list(df["SMA24"]),
            "sma72": safe_list(df["SMA72"]),
            "bb_upper": safe_list(df["BB_Upper"]),
            "bb_lower": safe_list(df["BB_Lower"]),
            "rsi": safe_list(df["RSI14"]),
            "macd": safe_list(df["MACD"]),
            "macd_signal": safe_list(df["MACD_Signal"]),
            "macd_hist": safe_list(df["MACD_Hist"]),
            "volume": safe_list(df["volume"])
        }
        
        log.debug(f"K线数据: {len(data['prices'])}条")
        _klines_cache["data"] = data
        _klines_cache["ts"] = _t.time()
        return JSONResponse(data)
    except Exception as e:
        log.error(f"获取K线数据失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/indicators")
async def get_indicators():
    import time as _t
    now = _t.time()
    if _indicators_cache["data"] is not None and (now - _indicators_cache["ts"]) < _INDICATORS_CACHE_TTL:
        return JSONResponse(_indicators_cache["data"])
    try:
        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        client = BybitClient(api_key=key, api_secret=sec,
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        
        kl = client.get_kline(symbol=symbol, interval="60", limit=100)
        # 明确处理API异常或空数据，避免前端只看到500
        if not kl:
            log.error("获取K线失败: 响应为空")
            return JSONResponse({"error": "获取K线失败: 响应为空"}, status_code=500)
        if isinstance(kl, dict) and kl.get("retCode") not in (0, None):
            msg = kl.get("retMsg", "未知错误")
            log.error(f"获取K线失败: retCode={kl.get('retCode')}, retMsg={msg}")
            return JSONResponse(
                {"error": f"获取K线失败: {msg}", "retCode": kl.get("retCode")},
                status_code=500
            )
        
        df = enrich_indicators(klines_to_df(kl))
        
        if df is None or df.empty:
            log.error("获取指标失败: K线数据为空")
            return JSONResponse({"error": "指标数据为空（K线无数据）"}, status_code=500)
        
        last = df.iloc[-1]
        market = get_market_condition(df)

        # === 基础技术指标 ===
        data = {
            "rsi14": float(last.get("RSI14", 50)),
            "rsi7": float(last.get("RSI7", 50)),
            "trend": float(last.get("Trend", 0)),
            "bb_position": float(last.get("BB_Position", 0.5)),
            "macd_hist": float(last.get("MACD_Hist", 0)),
            "atr_pct": float(last.get("ATR_Pct", 0)),
            "volume_ratio": float(last.get("Volume_Ratio", 1)),
            "support": market.get("support", 0),
            "resistance": market.get("resistance", 0),
            "condition": market.get("condition", "unknown"),
        }

        # === 新闻情绪数据 ===
        try:
            from news_sentiment import get_cached_sentiment
            news = get_cached_sentiment()
            data["news_sentiment"] = news.get("score", 0)
            data["news_confidence"] = news.get("confidence", 0)
            data["news_risk_level"] = news.get("risk_level", "medium")
            data["news_action"] = news.get("suggested_action", "hold")
            data["news_summary"] = news.get("summary", "")
            data["news_key_factors"] = news.get("key_factors", [])[:3]
            data["news_age_min"] = news.get("age_min", 999)
        except Exception:
            data["news_sentiment"] = 0
            data["news_key_factors"] = []
            data["news_summary"] = ""

        # 统一处理 NaN/Inf，避免 Out of range float JSON 错误
        clean_data = make_json_serializable(data)
        _indicators_cache["data"] = clean_data
        _indicators_cache["ts"] = _t.time()
        return JSONResponse(clean_data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/trades")
async def get_trades():
    try:
        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        client = BybitClient(api_key=key, api_secret=sec,
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        trades = []

        # 1. 挂单（未成交）
        try:
            open_resp = client.get_open_orders(symbol=symbol, limit=10)
            open_orders = (open_resp.get("result") or {}).get("list", [])
            for o in open_orders:
                trades.append({
                    "ts_ms": int(o.get("createdTime", 0)),
                    "symbol": o.get("symbol", symbol),
                    "side": o.get("side", ""),
                    "qty": o.get("qty", "0"),
                    "price": o.get("price", "0"),
                    "order_type": o.get("orderType", ""),
                    "reason": "",
                    "status": "PENDING",
                    "order_id": o.get("orderId", ""),
                })
        except Exception:
            pass

        # 2. 已成交记录
        exec_resp = client.get_trade_history(symbol=symbol, limit=20)
        execs = (exec_resp.get("result") or {}).get("list", [])
        for e in execs:
            trades.append({
                "ts_ms": int(e.get("execTime", 0)),
                "symbol": e.get("symbol", symbol),
                "side": e.get("side", ""),
                "qty": e.get("execQty", "0"),
                "price": e.get("execPrice", "0"),
                "order_type": e.get("orderType", ""),
                "reason": e.get("execType", ""),
                "status": "OK",
                "order_id": e.get("orderId", ""),
            })
        return JSONResponse(trades)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/recommended_price")
async def get_recommended_price():
    """获取推荐的买入/卖出价格"""
    try:
        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        client = BybitClient(api_key=key, api_secret=sec,
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        
        # 获取当前价格
        tkr = client.get_ticker(symbol)
        last_price = float(tkr["result"]["list"][0]["lastPrice"])
        
        # 获取交易对信息
        instr_info = client.get_instruments_info(symbol)
        instr_result = instr_info.get("result", {}).get("list", [])
        tick_size = 0.0001
        if instr_result:
            price_filter = instr_result[0].get("priceFilter", {})
            tick_size = float(price_filter.get("tickSize", 0.0001))
        
        # 获取订单簿
        ob = client.get_orderbook(symbol, limit=5)
        bid1, ask1 = client.extract_best_prices(ob)
        
        # 买入推荐价：比当前价低0.3%~0.5%，取bid1附近
        # 这样挂单等待价格回调时买入
        buy_discount = 0.003  # 0.3%折扣
        buy_price = min(bid1 if bid1 else last_price, last_price * (1 - buy_discount))
        buy_price = round(buy_price / tick_size) * tick_size
        buy_price = round(buy_price, 8)
        
        # 卖出推荐价：比当前价高0.3%~0.5%，取ask1附近
        # 这样挂单等待价格上涨时卖出
        sell_premium = 0.003  # 0.3%溢价
        sell_price = max(ask1 if ask1 else last_price, last_price * (1 + sell_premium))
        sell_price = round(sell_price / tick_size) * tick_size
        sell_price = round(sell_price, 8)
        
        return JSONResponse({
            "symbol": symbol,
            "last_price": last_price,
            "bid1": bid1,
            "ask1": ask1,
            "buy_price": buy_price,
            "buy_discount_pct": round((last_price - buy_price) / last_price * 100, 2),
            "sell_price": sell_price,
            "sell_premium_pct": round((sell_price - last_price) / last_price * 100, 2)
        })
    except Exception as e:
        log.error(f"获取推荐价格失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/manual_trade")
async def manual_trade(request: Request, user: Dict = Depends(get_current_user)):
    """手动交易 - 挂单模式，按推荐价格下单"""
    try:
        body = await request.json()
        side = body.get("side", "").upper()  # BUY or SELL
        custom_price = body.get("price")  # 可选：自定义价格
        amount_usdt = body.get("amount_usdt")  # 可选：指定交易金额（USDT）
        
        if side not in ["BUY", "SELL"]:
            return JSONResponse({"error": "无效的交易方向"}, status_code=400)

        # v4.4: 手动挂单防重复 - 同方向同价格30秒内去重
        import time as _time
        _dedup_key = f"{side}:{custom_price or 'auto'}"
        _now = _time.time()
        if not hasattr(manual_trade, "_last_orders"):
            manual_trade._last_orders = {}
        _last_ts = manual_trade._last_orders.get(_dedup_key, 0)
        if _now - _last_ts < 30:
            return JSONResponse({
                "status": "DEDUP",
                "message": f"同方向同价格30秒内重复, 已忽略(距上次{_now - _last_ts:.0f}秒)"
            })
        manual_trade._last_orders[_dedup_key] = _now

        key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        sec = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        client = BybitClient(api_key=key, api_secret=sec,
                            testnet=cfg.get("testnet", False),
                            account_type=cfg.get("account_type", "UNIFIED"))
        
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        
        # 获取交易对信息
        instr_info = client.get_instruments_info(symbol)
        instr_result = instr_info.get("result", {}).get("list", [])
        if not instr_result:
            return JSONResponse({"error": "获取交易对信息失败"}, status_code=500)
        
        instr = instr_result[0]
        lot_filter = instr.get("lotSizeFilter", {})
        min_qty = float(lot_filter.get("minOrderQty", 0))
        qty_step = float(lot_filter.get("qtyStep", 0.0001))
        min_notional = float(lot_filter.get("minNotionalValue", 0))
        
        price_filter = instr.get("priceFilter", {})
        tick_size = float(price_filter.get("tickSize", 0.0001))
        
        # 获取当前价格和订单簿
        tkr = client.get_ticker(symbol)
        last_price = float(tkr["result"]["list"][0]["lastPrice"])
        
        ob = client.get_orderbook(symbol, limit=5)
        bid1, ask1 = client.extract_best_prices(ob)
        
        # 计算推荐价格
        if custom_price:
            price = float(custom_price)
        else:
            if side == "BUY":
                # 买入：比当前价低0.3%，挂单等待
                buy_discount = 0.003
                price = min(bid1 if bid1 else last_price, last_price * (1 - buy_discount))
            else:
                # 卖出：比当前价高0.3%，挂单等待
                sell_premium = 0.003
                price = max(ask1 if ask1 else last_price, last_price * (1 + sell_premium))
        
        price = round(price / tick_size) * tick_size
        price = round(price, 8)
        
        # 验证价格
        if side == "BUY" and price > last_price:
            return JSONResponse({"error": f"买入价格({price})不能高于当前价({last_price})"}, status_code=400)
        if side == "SELL" and price < last_price:
            return JSONResponse({"error": f"卖出价格({price})不能低于当前价({last_price})"}, status_code=400)
        
        # 计算交易量（未启用杠杆时强制1.0）
        leverage_enabled = bool(cfg.get("risk", {}).get("leverage_enabled", False))
        leverage = float(cfg.get("risk", {}).get("leverage", 1.0)) if leverage_enabled else 1.0
        
        if amount_usdt:
            # 如果指定了交易金额，使用该金额
            target_value = float(amount_usdt)
            # 买入时考虑杠杆：实际下单金额 = 指定金额 * 杠杆倍数
            if side == "BUY" and leverage >= 2.0:
                effective_value = target_value * leverage
                actual_capital = target_value  # 实际使用资金 = 用户指定的金额
                log.info(f"使用{leverage}倍杠杆：指定金额{target_value} USDT，实际下单金额{effective_value} USDT，实际使用资金{actual_capital} USDT")
            else:
                effective_value = target_value
                actual_capital = target_value
        else:
            # 默认：确保满足最小金额要求
            min_order_value = max(min_notional, 5.0)  # 至少5 USDT
            effective_value = min_order_value * 1.2  # 多20%余量
            # 默认情况下，买入时如果使用杠杆，实际使用资金 = 下单金额 / 杠杆
            actual_capital = (effective_value / leverage) if (side == "BUY" and leverage >= 2.0) else effective_value
        
        # 计算所需数量
        trade_qty = effective_value / price
        trade_qty = max(trade_qty, min_qty * 2)  # 至少是最小量的2倍
        trade_qty = round(trade_qty / qty_step) * qty_step
        trade_qty = round(trade_qty, 8)
        
        # 再次验证金额
        notional = trade_qty * price
        if notional < min_notional:
            trade_qty = (min_notional / price) * 1.5
            trade_qty = round(trade_qty / qty_step) * qty_step
            trade_qty = round(trade_qty, 8)
            notional = trade_qty * price
            # 如果之前没有指定金额，重新计算实际使用资金
            if not amount_usdt:
                actual_capital = (notional / leverage) if (side == "BUY" and leverage >= 2.0) else notional
        
        log.info(f"手动挂单: {side} {trade_qty} {symbol} @ {price} (当前价: {last_price}, 下单金额: {notional:.2f} USDT, 实际使用资金: {actual_capital:.2f} USDT, 杠杆: {leverage}x)")
        
        # 检查是否启用交易
        if not cfg.get("enable_trading", False):
            return JSONResponse({
                "status": "DRYRUN",
                "message": "模拟交易（enable_trading=false）",
                "side": side,
                "qty": trade_qty,
                "price": price,
                "last_price": last_price,
                "notional": trade_qty * price,
                "order_type": "GTC挂单"
            })
        
        # 执行交易 - 使用GTC（挂单直到成交或取消）
        # 根据杠杆倍数选择API：>=2倍使用杠杆API，1倍使用正常API
        isLeverage = 1 if leverage >= 2.0 else 0
        
        resp = client.place_order(
            symbol=symbol,
            side="Buy" if side == "BUY" else "Sell",
            order_type="Limit",
            qty=str(trade_qty),
            price=str(price),
            tif="GTC",  # Good Till Cancel - 挂单直到成交
            order_link_id=f"manual_{int(time.time())}",
            isLeverage=isLeverage
        )
        
        if resp.get("retCode") == 0:
            order_id = resp.get("result", {}).get("orderId", "")
            leverage_info = f"（{leverage}x杠杆，实际使用资金{actual_capital:.2f} USDT）" if (side == "BUY" and leverage >= 2.0) else ""
            log.info(f"挂单成功: {side} {trade_qty} @ {price}, 订单ID: {order_id}, 下单金额: {notional:.2f} USDT{leverage_info}")
            return JSONResponse({
                "status": "OK",
                "message": f"挂单成功，等待成交{leverage_info}",
                "side": side,
                "qty": trade_qty,
                "price": price,
                "last_price": last_price,
                "notional": notional,
                "actual_capital": actual_capital if (side == "BUY" and leverage >= 2.0) else notional,
                "leverage": leverage,
                "isLeverage": isLeverage,
                "order_id": order_id,
                "order_type": "GTC挂单"
            })
        else:
            error_msg = resp.get("retMsg", "未知错误")
            error_code = resp.get("retCode", 0)
            
            # 检查是否是抵押品设置错误
            if "collateral" in error_msg.lower() or "collateral settings" in error_msg.lower():
                base_coin = symbol.replace("USDT", "")
                detailed_error = (
                    f"❌ 杠杆交易失败：{base_coin} 未设置为抵押品\n\n"
                    f"解决方案：\n"
                    f"1. 登录Bybit账户：https://www.bybit.com/\n"
                    f"2. 进入 '资产' -> '现货账户' -> '杠杆账户'\n"
                    f"3. 找到 {base_coin} 币种，点击 '设置为抵押品'\n"
                    f"4. 或者进入 '交易' -> '现货' -> 选择 {symbol} -> 点击杠杆设置\n"
                    f"5. 将 {base_coin} 设置为抵押品后，即可使用杠杆交易\n\n"
                    f"注意：使用杠杆交易前，必须先将币种设置为抵押品"
                )
                log.error(f"挂单失败（抵押品设置）: {error_msg}")
                log.error(f"详细说明: {detailed_error}")
                return JSONResponse({
                    "status": "ERROR",
                    "error": error_msg,
                    "error_code": error_code,
                    "error_type": "collateral_not_set",
                    "detailed_message": detailed_error,
                    "base_coin": base_coin,
                    "symbol": symbol,
                    "side": side,
                    "qty": trade_qty,
                    "price": price,
                    "help_url": "https://www.bybit.com/trade/spot/SOL/USDT"
                }, status_code=400)
            else:
                log.error(f"挂单失败: {error_msg}")
                return JSONResponse({
                    "status": "ERROR",
                    "error": error_msg,
                    "error_code": error_code,
                    "side": side,
                    "qty": trade_qty,
                    "price": price
                }, status_code=400)
            
    except Exception as e:
        log.error(f"手动交易异常: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/cancel_orders")
async def cancel_orders(user: Dict = Depends(get_current_user)):
    """撤销当前交易对的所有挂单"""
    try:
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        client = BybitClient(api_key=cfg.get("api_key"), api_secret=cfg.get("api_secret"),
                             testnet=cfg.get("testnet", True),
                             account_type=cfg.get("account_type", "UNIFIED"))
        resp = client.cancel_all_orders(symbol=symbol)
        if BybitClient.ok(resp):
            cancelled = len((resp.get("result") or {}).get("list", []))
            log.info(f"撤销挂单成功: {cancelled} 笔, symbol={symbol}")
            return JSONResponse({"status": "OK", "cancelled": cancelled, "symbol": symbol})
        else:
            msg = resp.get("retMsg", "unknown error")
            log.warning(f"撤销挂单失败: {msg}")
            return JSONResponse({"status": "ERROR", "message": msg}, status_code=400)
    except Exception as e:
        log.error(f"撤销挂单异常: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/update_daily_limit")
async def update_daily_limit(request: Request, user: Dict = Depends(get_current_user)):
    """更新每日交易额度限制（百分比模式）"""
    global cfg
    try:
        data = await request.json()
        new_limit_pct = float(data.get("limit", 90))
        
        if new_limit_pct < 0:
            return JSONResponse({"error": "限额百分比不能为负数"}, status_code=400)
        if new_limit_pct > 100:
            return JSONResponse({"error": "限额百分比不能超过100%"}, status_code=400)
        
        # 更新内存中的配置
        if "risk" not in cfg:
            cfg["risk"] = {}
        cfg["risk"]["max_daily_volume_pct"] = new_limit_pct
        
        # 保存到配置文件
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            log.info(f"每日限额已更新为: {new_limit_pct}%")
            return JSONResponse({
                "status": "OK", 
                "message": f"每日限额已更新为 {new_limit_pct}%",
                "new_limit_pct": new_limit_pct
            })
        except Exception as e:
            log.warning(f"保存配置文件失败: {e}，仅更新内存配置")
            return JSONResponse({
                "status": "OK", 
                "message": f"每日限额已临时更新为 {new_limit_pct}%（配置文件保存失败）",
                "new_limit_pct": new_limit_pct
            })
            
    except Exception as e:
        log.error(f"更新每日限额异常: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/update_leverage")
async def update_leverage(request: Request, user: Dict = Depends(get_current_user)):
    """更新杠杆设置（开关+倍数）"""
    global cfg
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", False))
        new_leverage = float(data.get("leverage", 1.0))

        if enabled:
            if new_leverage < 2.0:
                return JSONResponse({"error": "杠杆倍数不能小于2"}, status_code=400)
            if new_leverage > 10.0:
                return JSONResponse({"error": "杠杆倍数不能超过10倍"}, status_code=400)
        else:
            new_leverage = 1.0

        if "risk" not in cfg:
            cfg["risk"] = {}
        cfg["risk"]["leverage_enabled"] = enabled
        cfg["risk"]["leverage"] = new_leverage

        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            mode = f"{new_leverage}x 杠杆" if enabled else "现货模式"
            log.info(f"交易模式已更新: {mode}")
            return JSONResponse({"status": "OK", "message": mode, "leverage_enabled": enabled, "new_leverage": new_leverage})
        except Exception as e:
            log.warning(f"保存配置文件失败: {e}")
            return JSONResponse({"status": "OK", "message": "已临时更新（配置文件保存失败）", "leverage_enabled": enabled, "new_leverage": new_leverage})
    except Exception as e:
        log.error(f"更新杠杆设置失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/update_cost_adjustment_factor")
async def update_cost_adjustment_factor(request: Request, user: Dict = Depends(get_current_user)):
    """更新成本价调整系数"""
    global cfg
    try:
        data = await request.json()
        new_factor = float(data.get("cost_adjustment_factor", 1.0))
        
        if new_factor < 0.5:
            return JSONResponse({"error": "成本价调整系数不能小于0.5"}, status_code=400)
        if new_factor > 2.0:
            return JSONResponse({"error": "成本价调整系数不能超过2.0（风险过高）"}, status_code=400)
        
        # 更新内存中的配置
        cfg["cost_adjustment_factor"] = new_factor
        
        # 保存到配置文件
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            log.info(f"成本价调整系数已更新为: {new_factor}")
            return JSONResponse({
                "status": "OK", 
                "message": f"成本价调整系数已更新为 {new_factor}",
                "cost_adjustment_factor": new_factor
            })
        except Exception as e:
            log.warning(f"保存配置文件失败: {e}，仅更新内存配置")
            return JSONResponse({
                "status": "OK", 
                "message": f"成本价调整系数已临时更新为 {new_factor}（配置文件保存失败）",
                "cost_adjustment_factor": new_factor
            })
    except Exception as e:
        log.error(f"更新成本价调整系数失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/reset_daily_counters")
async def reset_daily_counters(user: Dict = Depends(get_current_user)):
    """重置今日交易次数和额度（按新西兰当日计算，仅用于紧急情况）"""
    try:
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        # 使用与bot_core一致的新西兰日期
        today = _nz_today_str()
        
        # 重置买入和卖出计数器
        buy_key = f"cnt_buy_{symbol.upper()}_{today}"
        sell_key = f"cnt_sell_{symbol.upper()}_{today}"
        # 重置当日交易额度（买入和卖出分开存储）
        buy_vol_key = f"daily_vol_buy_{symbol.upper()}_{today}"
        sell_vol_key = f"daily_vol_sell_{symbol.upper()}_{today}"
        # 可选：重置当日盈亏统计
        pnl_key = f"daily_pnl_{symbol.upper()}_{today}"
        
        set_meta(buy_key, "0")
        set_meta(sell_key, "0")
        set_meta(buy_vol_key, "0")
        set_meta(sell_vol_key, "0")
        set_meta(pnl_key, "0")
        
        log.info(f"已重置今日交易次数与额度: {symbol}, 日期: {today}")
        return JSONResponse({
            "status": "OK",
            "message": "今日交易次数与额度已重置",
            "symbol": symbol
        })
    except Exception as e:
        log.error(f"重置计数器异常: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/signals")
async def get_signals():
    try:
        init_db()
        symbols = cfg.get("symbols", [])
        symbol = symbols[0] if symbols else "SOLUSDT"
        signals = recent_signals(symbol=symbol, limit=20)
        return JSONResponse(make_json_serializable(signals))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ============== 认证相关API ==============
@app.post("/api/login")
async def login(request: Request):
    """登录接口"""
    try:
        data = await request.json()
        username = data.get("username", "")
        password = data.get("password", "")
        
        if username not in USERS_DB:
            return JSONResponse(
                {"success": False, "message": "用户名或密码错误"},
                status_code=401
            )
        
        user = USERS_DB[username]
        if not verify_password(password, user["password_hash"]):
            return JSONResponse(
                {"success": False, "message": "用户名或密码错误"},
                status_code=401
            )
        
        token = create_jwt_token(username)
        return JSONResponse({
            "success": True,
            "token": token,
            "username": username
        })
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=500
        )

@app.get("/api/logout")
async def logout():
    """退出登录"""
    return JSONResponse({"success": True, "message": "已退出登录"})

@app.get("/api/user")
async def get_user(current_user: Dict = Depends(get_current_user)):
    """获取当前用户信息"""
    return JSONResponse({
        "username": current_user["username"],
        "success": True
    })

# ============== Web界面 ==============
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """登录页面"""
    html = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - 智能量化交易系统</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Rajdhani', 'Segoe UI', sans-serif;
            background: #0a0a1a;
            color: #fff;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        .bg-animation {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: 
                radial-gradient(ellipse at 20% 80%, rgba(0, 212, 255, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, rgba(123, 44, 191, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 50% 50%, rgba(0, 255, 136, 0.05) 0%, transparent 70%);
            z-index: -1;
        }
        .login-container {
            background: linear-gradient(145deg, rgba(30, 42, 74, 0.9), rgba(22, 32, 53, 0.95));
            border-radius: 20px;
            padding: 40px;
            border: 1px solid rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            width: 100%;
            max-width: 400px;
        }
        .login-header {
            text-align: center;
            margin-bottom: 30px;
        }
        .login-header h1 {
            font-family: 'Orbitron', monospace;
            font-size: 2rem;
            font-weight: 900;
            background: linear-gradient(135deg, #00d4ff 0%, #7b2cbf 50%, #00ff88 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        .login-header p {
            color: #5a6a8a;
            font-size: 0.9rem;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            color: #b8c5d6;
            margin-bottom: 8px;
            font-size: 0.9rem;
        }
        .form-group input {
            width: 100%;
            padding: 12px 15px;
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            color: #fff;
            font-size: 1rem;
            outline: none;
            transition: all 0.3s;
        }
        .form-group input:focus {
            border-color: #00d4ff;
            box-shadow: 0 0 15px rgba(0, 212, 255, 0.3);
        }
        .login-btn {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #00d4ff, #7b2cbf);
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s;
            margin-top: 10px;
        }
        .login-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(0, 212, 255, 0.4);
        }
        .login-btn:active {
            transform: translateY(0);
        }
        .error-message {
            color: #e74c3c;
            font-size: 0.85rem;
            margin-top: 10px;
            text-align: center;
            display: none;
        }
        .error-message.show {
            display: block;
        }
        body {
            background: #f5f7fb;
            color: #1f2937;
        }
        .bg-animation {
            background:
                linear-gradient(135deg, rgba(14, 165, 233, 0.10), transparent 32%),
                linear-gradient(225deg, rgba(16, 185, 129, 0.12), transparent 38%),
                #f5f7fb;
        }
        .login-container {
            background: rgba(255, 255, 255, 0.94);
            border: 1px solid #d7dee8;
            border-radius: 8px;
            box-shadow: 0 24px 70px rgba(15, 23, 42, 0.14);
        }
        .login-header h1 {
            background: linear-gradient(135deg, #0f766e 0%, #2563eb 55%, #7c3aed 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .login-header p,
        .form-group label {
            color: #64748b;
        }
        .form-group input {
            background: #ffffff;
            border: 1px solid #cbd5e1;
            color: #111827;
            border-radius: 8px;
        }
        .form-group input:focus {
            border-color: #0ea5e9;
            box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.18);
        }
        .login-btn {
            background: linear-gradient(135deg, #0f766e, #2563eb);
            border-radius: 8px;
            box-shadow: 0 10px 24px rgba(37, 99, 235, 0.18);
        }
        .login-btn:hover {
            box-shadow: 0 14px 30px rgba(37, 99, 235, 0.24);
        }
        .theme-toggle {
            position: fixed;
            top: 18px;
            right: 18px;
            background: rgba(255, 255, 255, 0.86);
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            color: #334155;
            cursor: pointer;
            font-size: 0.9rem;
            padding: 8px 12px;
        }
        body[data-theme="dark"] {
            background: #0a0a1a;
            color: #fff;
        }
        body[data-theme="dark"] .bg-animation {
            background:
                radial-gradient(ellipse at 20% 80%, rgba(0, 212, 255, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, rgba(123, 44, 191, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 50% 50%, rgba(0, 255, 136, 0.05) 0%, transparent 70%);
        }
        body[data-theme="dark"] .login-container {
            background: linear-gradient(145deg, rgba(30, 42, 74, 0.9), rgba(22, 32, 53, 0.95));
            border-color: rgba(255,255,255,0.1);
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
        }
        body[data-theme="dark"] .form-group input {
            background: rgba(0, 0, 0, 0.3);
            border-color: rgba(255,255,255,0.1);
            color: #fff;
        }
        body[data-theme="dark"] .theme-toggle {
            background: rgba(15, 23, 42, 0.82);
            border-color: rgba(148, 163, 184, 0.35);
            color: #e2e8f0;
        }
    </style>
</head>
<body>
    <div class="bg-animation"></div>
    <button type="button" class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">深色</button>
    <div class="login-container">
        <div class="login-header">
            <h1>⚡ QUANTUM TRADER</h1>
            <p>智能量化交易系统</p>
        </div>
        <form id="loginForm">
            <div class="form-group">
                <label>用户名</label>
                <input type="text" id="username" name="username" required autocomplete="username">
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" id="password" name="password" required autocomplete="current-password">
            </div>
            <button type="submit" class="login-btn">登录</button>
            <div class="error-message" id="errorMessage"></div>
        </form>
    </div>
    <script>
        const THEME_KEY = 'quant_theme';
        function applyTheme(theme) {
            const nextTheme = theme === 'dark' ? 'dark' : 'light';
            document.body.dataset.theme = nextTheme;
            localStorage.setItem(THEME_KEY, nextTheme);
            const btn = document.getElementById('theme-toggle');
            if (btn) btn.textContent = nextTheme === 'dark' ? '浅色' : '深色';
        }
        function toggleTheme() {
            applyTheme(document.body.dataset.theme === 'dark' ? 'light' : 'dark');
        }
        applyTheme(localStorage.getItem(THEME_KEY) || 'light');

        document.getElementById('loginForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const errorMsg = document.getElementById('errorMessage');
            
            errorMsg.classList.remove('show');
            
            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ username, password })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    localStorage.setItem('token', data.token);
                    localStorage.setItem('username', data.username);
                    window.location.href = '/';
                } else {
                    errorMsg.textContent = data.message || '登录失败';
                    errorMsg.classList.add('show');
                }
            } catch (error) {
                errorMsg.textContent = '网络错误，请重试';
                errorMsg.classList.add('show');
            }
        });
        
        // 如果已经登录，验证token后跳转
        const token = localStorage.getItem('token');
        if (token) {
            try {
                const payload = JSON.parse(atob(token.split('.')[1]));
                const exp = payload.exp * 1000;
                if (Date.now() < exp) {
                    // token有效，跳转到首页
                    window.location.href = '/';
                } else {
                    // token已过期，清除
                    localStorage.removeItem('token');
                    localStorage.removeItem('username');
                }
            } catch (e) {
                // token格式错误，清除
                localStorage.removeItem('token');
                localStorage.removeItem('username');
            }
        }
    </script>
</body>
</html>
"""
    return html

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # 不在这里检查token，让前端JavaScript检查，避免循环重定向
    # token存储在localStorage中，存储于 localStorage，无法直接读取
    symbols = cfg.get("symbols", [])
    symbol = symbols[0] if symbols else "SOLUSDT"

    # v5.5.13 i18n: 中英文切换字典 (在 f-string 外构建,避免 {{}} 转义复杂)
    import json as _json_mod
    i18n_dict = {
        "zh": {
            "title": "智能量化交易系统",
            "subtitle": "INTELLIGENT ALGORITHMIC TRADING SYSTEM v2.0",
            "logout": "退出",
            "view_strategy": "📋 查看交易策略说明",
            "card_total_asset": "💎 总资产",
            "card_pnl": "📊 持仓盈亏",
            "card_holdings": "🪙 持仓量",
            "card_usdt": "💵 可用USDT",
            "tech_indicators": "📈 技术指标 INDICATORS",
            "news_sentiment": "📰 新闻情绪 NEWS SENTIMENT",
            "price_chart": "📈 价格走势 (72H)",
            "rsi_chart": "📊 RSI指标",
            "macd_chart": "📉 MACD",
            "recent_trades": "📜 最近交易 TRADES",
            "signals_log": "📡 信号记录 SIGNALS",
            "engine_active": "TRADING ENGINE ACTIVE",
            "btn_buy": "🚀 BUY",
            "btn_sell": "📉 SELL",
            "manual_trade": "手动交易",
            "trade_settings": "交易设置",
            "leverage_setting": "杠杆倍数",
            "save": "保存",
            "reset_daily": "重置当日计数",
            "rsi_label": "RSI (14)",
            "trend_strength": "趋势强度",
            "bb_position": "布林位置",
            "macd_hist": "MACD柱",
            "atr_volatility": "ATR波动",
            "volume_ratio": "成交量比",
            "support": "支撑位",
            "resistance": "阻力位",
            "lang_btn": "🌐 EN",
            "theme_btn_dark": "深色",
            "theme_btn_light": "浅色",
        },
        "en": {
            "title": "Smart Quant Trading",
            "subtitle": "INTELLIGENT ALGORITHMIC TRADING SYSTEM v2.0",
            "logout": "Logout",
            "view_strategy": "📋 Strategy Notes",
            "card_total_asset": "💎 Total Asset",
            "card_pnl": "📊 P&L",
            "card_holdings": "🪙 Holdings",
            "card_usdt": "💵 USDT",
            "tech_indicators": "📈 INDICATORS",
            "news_sentiment": "📰 NEWS SENTIMENT",
            "price_chart": "📈 Price (72H)",
            "rsi_chart": "📊 RSI",
            "macd_chart": "📉 MACD",
            "recent_trades": "📜 Recent Trades",
            "signals_log": "📡 Signals",
            "engine_active": "TRADING ENGINE ACTIVE",
            "btn_buy": "🚀 BUY",
            "btn_sell": "📉 SELL",
            "manual_trade": "Manual Trade",
            "trade_settings": "Trade Settings",
            "leverage_setting": "Leverage",
            "save": "Save",
            "reset_daily": "Reset Daily Counter",
            "rsi_label": "RSI (14)",
            "trend_strength": "Trend Strength",
            "bb_position": "BB Position",
            "macd_hist": "MACD Hist",
            "atr_volatility": "ATR Volatility",
            "volume_ratio": "Volume Ratio",
            "support": "Support",
            "resistance": "Resistance",
            "lang_btn": "🌐 中",
            "theme_btn_dark": "Dark",
            "theme_btn_light": "Light",
        },
    }
    i18n_json = _json_mod.dumps(i18n_dict, ensure_ascii=False)

    html = f"""
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>智能量化交易系统</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
    <!-- Chart.js will be loaded at end of body -->
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Rajdhani', 'Segoe UI', sans-serif;
            background: #0a0a1a;
            color: #fff;
            min-height: 100vh;
            overflow-x: hidden;
        }}
        
        /* 动态背景 */
        .bg-animation {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: 
                radial-gradient(ellipse at 20% 80%, rgba(0, 212, 255, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, rgba(123, 44, 191, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 50% 50%, rgba(0, 255, 136, 0.05) 0%, transparent 70%);
            z-index: -1;
        }}
        
        .container {{ max-width: 1600px; margin: 0 auto; padding: 20px; }}
        
        /* 标题 */
        .header {{
            text-align: center;
            margin-bottom: 30px;
            position: relative;
        }}
        h1 {{
            font-family: 'Orbitron', monospace;
            font-size: 2.8rem;
            font-weight: 900;
            background: linear-gradient(135deg, #00d4ff 0%, #7b2cbf 50%, #00ff88 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-shadow: 0 0 60px rgba(0, 212, 255, 0.5);
            letter-spacing: 3px;
        }}
        .subtitle {{
            color: #5a6a8a;
            font-size: 1rem;
            margin-top: 10px;
            letter-spacing: 2px;
        }}
        
        /* 网格布局 */
        .grid-5 {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 15px; margin-bottom: 25px; }}
        .grid-2 {{ display: grid; grid-template-columns: 1fr 2fr; gap: 20px; margin-bottom: 25px; }}
        .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 25px; }}
        
        /* 卡片 */
        .card {{
            background: linear-gradient(145deg, rgba(30, 42, 74, 0.8), rgba(22, 32, 53, 0.9));
            border-radius: 20px;
            padding: 25px;
            border: 1px solid rgba(255,255,255,0.08);
            backdrop-filter: blur(10px);
            box-shadow: 0 10px 40px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.05);
            transition: transform 0.3s, box-shadow 0.3s;
        }}
        .card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 20px 60px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.1);
        }}
        
        .card-title {{
            color: #5a6a8a;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .card-value {{
            font-family: 'Orbitron', monospace;
            font-size: 1.8rem;
            font-weight: 700;
        }}
        .card-sub {{ color: #5a6a8a; font-size: 0.85rem; margin-top: 8px; }}
        
        /* 颜色 */
        .green {{ color: #00ff88; text-shadow: 0 0 20px rgba(0, 255, 136, 0.5); }}
        .red {{ color: #ff4757; text-shadow: 0 0 20px rgba(255, 71, 87, 0.5); }}
        .yellow {{ color: #ffa502; text-shadow: 0 0 20px rgba(255, 165, 2, 0.5); }}
        .blue {{ color: #00d4ff; text-shadow: 0 0 20px rgba(0, 212, 255, 0.5); }}
        .purple {{ color: #7b2cbf; text-shadow: 0 0 20px rgba(123, 44, 191, 0.5); }}
        
        /* 信号卡片 */
        .signal-card {{
            text-align: center;
            padding: 40px 30px;
            position: relative;
            overflow: hidden;
        }}
        .signal-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, transparent, var(--signal-color), transparent);
        }}
        .signal-icon {{ font-size: 4rem; margin-bottom: 15px; filter: drop-shadow(0 0 30px var(--signal-glow)); }}
        .signal-text {{ font-family: 'Orbitron', monospace; font-size: 2.5rem; font-weight: 900; letter-spacing: 5px; }}
        .signal-strength {{
            margin-top: 20px;
            height: 6px;
            background: rgba(255,255,255,0.1);
            border-radius: 3px;
            overflow: hidden;
        }}
        .signal-strength-bar {{ height: 100%; border-radius: 3px; transition: width 0.5s; }}
        
        /* 指标网格 */
        .indicators {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
        .indicator {{
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            padding: 18px;
            text-align: center;
            border: 1px solid rgba(255,255,255,0.05);
            transition: all 0.3s;
        }}
        .indicator:hover {{ background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.1); }}
        .indicator-value {{ font-family: 'Orbitron', monospace; font-size: 1.4rem; font-weight: 700; margin: 8px 0; }}
        .indicator-label {{ color: #5a6a8a; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; }}
        .indicator-status {{ font-size: 0.75rem; margin-top: 5px; }}
        
        /* 图表容器 */
        .chart-container {{ position: relative; height: 250px; }}
        .chart-title {{ color: #5a6a8a; font-size: 0.8rem; margin-bottom: 15px; display: flex; align-items: center; gap: 10px; }}
        .chart-title::after {{ content: ''; flex: 1; height: 1px; background: linear-gradient(90deg, rgba(255,255,255,0.1), transparent); }}
        
        /* 交易记录 */
        .trade-item {{
            background: rgba(255,255,255,0.03);
            border-radius: 10px;
            padding: 15px;
            margin: 8px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-left: 3px solid transparent;
            transition: all 0.3s;
        }}
        .trade-item:hover {{ background: rgba(255,255,255,0.06); }}
        .trade-item.buy {{ border-left-color: #00ff88; }}
        .trade-item.sell {{ border-left-color: #ff4757; }}
        
        /* 状态栏 */
        .status-bar {{
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: linear-gradient(180deg, transparent, rgba(10, 10, 26, 0.95));
            padding: 15px 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.85rem;
        }}
        .status-dot {{
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }}
        .status-dot.active {{ background: #00ff88; box-shadow: 0 0 15px #00ff88; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: 0.5; transform: scale(0.8); }} }}
        
        /* 24小时统计 */
        .stats-row {{ display: flex; gap: 30px; justify-content: center; margin-top: 10px; margin-bottom: 10px; }}
        .stat-item {{ display: flex; align-items: center; gap: 8px; }}
        .stat-label {{ color: #5a6a8a; font-size: 0.8rem; }}
        .stat-value {{ font-family: 'Orbitron', monospace; font-weight: 600; }}

        /* 滚动条 */
        ::-webkit-scrollbar {{ width: 6px; }}
        ::-webkit-scrollbar-track {{ background: rgba(255,255,255,0.05); }}
        ::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.2); border-radius: 3px; }}
        
        /* 交易按钮 */
        .trade-buttons {{
            display: flex;
            gap: 15px;
            margin-top: 25px;
        }}
        .trade-btn {{
            flex: 1;
            padding: 15px 20px;
            border: none;
            border-radius: 12px;
            font-family: 'Orbitron', monospace;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 2px;
        }}
        .trade-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .trade-btn.buy {{
            background: linear-gradient(135deg, #00ff88, #00cc6a);
            color: #000;
            box-shadow: 0 5px 20px rgba(0, 255, 136, 0.3);
        }}
        .trade-btn.buy:hover:not(:disabled) {{
            transform: translateY(-2px);
            box-shadow: 0 8px 30px rgba(0, 255, 136, 0.5);
        }}
        .trade-btn.sell {{
            background: linear-gradient(135deg, #ff4757, #ff2040);
            color: #fff;
            box-shadow: 0 5px 20px rgba(255, 71, 87, 0.3);
        }}
        .trade-btn.sell:hover:not(:disabled) {{
            transform: translateY(-2px);
            box-shadow: 0 8px 30px rgba(255, 71, 87, 0.5);
        }}
        .trade-btn.loading {{
            pointer-events: none;
        }}
        
        /* 交易结果弹窗 */
        .toast {{
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 25px;
            border-radius: 12px;
            font-weight: 600;
            z-index: 1000;
            animation: slideIn 0.3s ease;
            max-width: 400px;
        }}
        .toast.success {{
            background: linear-gradient(135deg, #00ff88, #00cc6a);
            color: #000;
        }}
        .toast.error {{
            background: linear-gradient(135deg, #ff4757, #ff2040);
            color: #fff;
        }}
        .toast.info {{
            background: linear-gradient(135deg, #ffa502, #ff8c00);
            color: #000;
        }}
        @keyframes slideIn {{
            from {{ transform: translateX(100%); opacity: 0; }}
            to {{ transform: translateX(0); opacity: 1; }}
        }}

        /* v5.5.13 theme refresh: default light dashboard with optional dark mode */
        :root {{
            --bg: #f5f7fb;
            --bg-soft: #eef4fb;
            --surface: rgba(255, 255, 255, 0.94);
            --surface-subtle: #f8fafc;
            --border: #d7dee8;
            --text: #162033;
            --muted: #64748b;
            --muted-strong: #475569;
            --accent: #0f766e;
            --accent-2: #2563eb;
            --accent-3: #7c3aed;
            --success: #059669;
            --danger: #dc2626;
            --warning: #d97706;
            --shadow: 0 16px 40px rgba(15, 23, 42, 0.10);
            --grid-line: rgba(100, 116, 139, 0.16);
            --modal-backdrop: rgba(15, 23, 42, 0.54);
        }}
        body[data-theme="dark"] {{
            --bg: #0a0a1a;
            --bg-soft: #111827;
            --surface: linear-gradient(145deg, rgba(30, 42, 74, 0.88), rgba(22, 32, 53, 0.94));
            --surface-subtle: rgba(255,255,255,0.04);
            --border: rgba(255,255,255,0.10);
            --text: #f8fafc;
            --muted: #8a97aa;
            --muted-strong: #b8c5d6;
            --accent: #00d4ff;
            --accent-2: #7b2cbf;
            --accent-3: #00ff88;
            --success: #00ff88;
            --danger: #ff4757;
            --warning: #ffa502;
            --shadow: 0 18px 56px rgba(0,0,0,0.44);
            --grid-line: rgba(255,255,255,0.06);
            --modal-backdrop: rgba(0,0,0,0.78);
        }}
        body {{
            background: var(--bg) !important;
            color: var(--text) !important;
        }}
        .bg-animation {{
            background:
                linear-gradient(135deg, rgba(37, 99, 235, 0.10), transparent 30%),
                linear-gradient(225deg, rgba(15, 118, 110, 0.12), transparent 36%),
                linear-gradient(0deg, rgba(124, 58, 237, 0.05), transparent 48%),
                var(--bg) !important;
        }}
        body[data-theme="dark"] .bg-animation {{
            background:
                radial-gradient(ellipse at 20% 80%, rgba(0, 212, 255, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 20%, rgba(123, 44, 191, 0.1) 0%, transparent 50%),
                radial-gradient(ellipse at 50% 50%, rgba(0, 255, 136, 0.05) 0%, transparent 70%) !important;
        }}
        .card,
        .indicator,
        .trade-item {{
            background: var(--surface) !important;
            border: 1px solid var(--border) !important;
            border-radius: 8px !important;
            box-shadow: var(--shadow) !important;
            color: var(--text) !important;
        }}
        .card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 20px 48px rgba(15, 23, 42, 0.14) !important;
        }}
        body[data-theme="dark"] .card:hover {{
            box-shadow: 0 20px 60px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.08) !important;
        }}
        h1 {{
            background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 58%, var(--accent-3) 100%) !important;
            -webkit-background-clip: text !important;
            -webkit-text-fill-color: transparent !important;
            text-shadow: none !important;
            letter-spacing: 0 !important;
        }}
        .subtitle,
        .card-title,
        .card-sub,
        .chart-title,
        .indicator-label,
        .stat-label,
        .indicator-status {{
            color: var(--muted) !important;
            letter-spacing: 0 !important;
        }}
        .chart-title::after {{
            background: linear-gradient(90deg, var(--border), transparent) !important;
        }}
        .green {{ color: var(--success) !important; text-shadow: none !important; }}
        .red {{ color: var(--danger) !important; text-shadow: none !important; }}
        .yellow {{ color: var(--warning) !important; text-shadow: none !important; }}
        .blue {{ color: var(--accent-2) !important; text-shadow: none !important; }}
        .purple {{ color: var(--accent-3) !important; text-shadow: none !important; }}
        body[data-theme="dark"] .blue {{ color: #00d4ff !important; }}
        body[data-theme="dark"] .purple {{ color: #b794f4 !important; }}
        .signal-strength {{
            background: var(--bg-soft) !important;
        }}
        .indicator {{
            background: var(--surface-subtle) !important;
            box-shadow: none !important;
        }}
        .indicator:hover,
        .trade-item:hover {{
            background: #eef6ff !important;
        }}
        body[data-theme="dark"] .indicator:hover,
        body[data-theme="dark"] .trade-item:hover {{
            background: rgba(255,255,255,0.07) !important;
        }}
        .status-bar {{
            background: rgba(255, 255, 255, 0.92) !important;
            border-top: 1px solid var(--border);
            box-shadow: 0 -8px 30px rgba(15, 23, 42, 0.06);
            color: var(--text);
        }}
        body[data-theme="dark"] .status-bar {{
            background: linear-gradient(180deg, transparent, rgba(10, 10, 26, 0.95)) !important;
            box-shadow: none;
        }}
        .theme-toggle,
        #lang-toggle {{
            background: rgba(255, 255, 255, 0.88) !important;
            border: 1px solid var(--border) !important;
            border-radius: 8px !important;
            color: var(--muted-strong) !important;
            cursor: pointer;
            font-size: 0.85rem;
            padding: 6px 12px;
            transition: background 0.2s, border-color 0.2s, color 0.2s, transform 0.2s;
        }}
        .theme-toggle:hover,
        #lang-toggle:hover {{
            background: #eef6ff !important;
            border-color: rgba(37, 99, 235, 0.35) !important;
            color: var(--accent-2) !important;
        }}
        body[data-theme="dark"] .theme-toggle,
        body[data-theme="dark"] #lang-toggle {{
            background: rgba(0,212,255,0.12) !important;
            border-color: rgba(0,212,255,0.35) !important;
            color: #00d4ff !important;
        }}
        #username-display {{
            color: var(--accent-2) !important;
            font-weight: 700;
        }}
        input,
        select {{
            background: #ffffff !important;
            border: 1px solid var(--border) !important;
            border-radius: 8px !important;
            color: var(--text) !important;
        }}
        body[data-theme="dark"] input,
        body[data-theme="dark"] select {{
            background: rgba(0, 0, 0, 0.28) !important;
            border-color: rgba(255,255,255,0.12) !important;
            color: #ffffff !important;
        }}
        .trade-btn {{
            border-radius: 8px !important;
            letter-spacing: 0 !important;
        }}
        #strategyModal {{
            background: var(--modal-backdrop) !important;
        }}
        #strategyModal > div {{
            background: var(--surface) !important;
            border: 1px solid var(--border) !important;
            border-radius: 8px !important;
            color: var(--text) !important;
            box-shadow: var(--shadow) !important;
        }}
        #strategyModal [style*="background: rgba(0, 0, 0, 0.3)"] {{
            background: var(--surface-subtle) !important;
            border-color: var(--border) !important;
        }}
        #news-card {{
            border-top-color: var(--border) !important;
        }}
        #news-summary,
        #trades-list [style*="color:#5a6a8a"],
        #signals-list [style*="color:#5a6a8a"],
        #news-label,
        #news-age {{
            color: var(--muted) !important;
        }}
        @media (max-width: 900px) {{
            .grid-5,
            .grid-3,
            .grid-2,
            .indicators {{
                grid-template-columns: 1fr !important;
            }}
            .header > div:first-child {{
                position: static !important;
                justify-content: center;
                margin-bottom: 16px;
                flex-wrap: wrap;
            }}
            h1 {{
                font-size: 2rem;
            }}
            .status-bar {{
                position: static;
                margin-top: 20px;
                gap: 10px;
                flex-direction: column;
            }}
        }}
    </style>
</head>
<body>
    <div class="bg-animation"></div>
    
    <div class="container">
        <!-- 头部 -->
        <div class="header" style="position: relative;">
            <div style="position: absolute; top: 0; right: 0; display: flex; align-items: center; gap: 10px;">
                <button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()">深色</button>
                <button id="lang-toggle" onclick="toggleLang()" style="background: rgba(0,212,255,0.15); border: 1px solid rgba(0,212,255,0.4); border-radius: 6px; padding: 6px 12px; color: #00d4ff; cursor: pointer; font-size: 0.85rem; transition: all 0.2s;" onmouseover="this.style.background='rgba(0,212,255,0.3)';" onmouseout="this.style.background='rgba(0,212,255,0.15)';">🌐 EN</button>
                <span style="color: #00d4ff; font-size: 0.9rem;" id="username-display">admin</span>
                <button onclick="logout()" data-i18n="logout" style="background: linear-gradient(135deg, #e74c3c, #c0392b); border: none; border-radius: 6px; padding: 6px 12px; color: white; cursor: pointer; font-size: 0.85rem; transition: all 0.2s;" onmouseover="this.style.transform='scale(1.05)'; this.style.boxShadow='0 0 15px rgba(231,76,60,0.5)';" onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none';">退出</button>
            </div>
            <h1>⚡ QUANTUM TRADER</h1>
            <div class="subtitle" data-i18n="subtitle">INTELLIGENT ALGORITHMIC TRADING SYSTEM v2.0</div>
            <button onclick="showStrategyModal()" data-i18n="view_strategy" style="margin-top: 15px; background: linear-gradient(135deg, #00d4ff, #7b2cbf); border: none; border-radius: 8px; padding: 10px 20px; color: white; cursor: pointer; font-size: 0.9rem; font-weight: 600; transition: all 0.3s;" onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 10px 30px rgba(0,212,255,0.4)';" onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='none';">📋 查看交易策略说明</button>
        </div>
        
        <!-- 策略说明弹出框 -->
        <div id="strategyModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 10000; overflow-y: auto;">
            <div style="max-width: 1200px; margin: 50px auto; background: linear-gradient(145deg, rgba(30, 42, 74, 0.95), rgba(22, 32, 53, 0.98)); border-radius: 20px; padding: 30px; border: 1px solid rgba(0, 212, 255, 0.3); position: relative;">
                <button onclick="closeStrategyModal()" style="position: absolute; top: 15px; right: 15px; background: rgba(231,76,60,0.3); border: 1px solid rgba(231,76,60,0.5); border-radius: 50%; width: 35px; height: 35px; color: #e74c3c; font-size: 1.2rem; cursor: pointer; transition: all 0.2s;" onmouseover="this.style.background='rgba(231,76,60,0.5)';" onmouseout="this.style.background='rgba(231,76,60,0.3)';">×</button>
                <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 20px;">
                    <span style="font-size: 1.5rem;">📋</span>
                    <h2 style="font-family: 'Orbitron', monospace; font-size: 1.5rem; color: #00d4ff; margin: 0;">交易策略说明</h2>
                </div>
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px;">
                    <!-- 核心策略 -->
                    <div style="background: rgba(0, 0, 0, 0.3); padding: 15px; border-radius: 12px; border-left: 3px solid #00d4ff;">
                        <div style="color: #00d4ff; font-weight: 700; margin-bottom: 8px; font-size: 0.9rem;">🎯 核心策略</div>
                        <div style="color: #b8c5d6; font-size: 0.85rem; line-height: 1.6;">
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">网格交易</strong>：价格低于成本时买入，高于成本时卖出</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">趋势跟踪</strong>：上涨趋势买入，下跌趋势卖出</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">多指标确认</strong>：RSI + MACD + 布林带 + 趋势强度</div>
                            <div>• <strong style="color: #fff;">智能仓位</strong>：根据信号强度和当前仓位动态调整</div>
                        </div>
                    </div>
                    
                    <!-- 限额逻辑 -->
                    <div style="background: rgba(0, 0, 0, 0.3); padding: 15px; border-radius: 12px; border-left: 3px solid #2ed573;">
                        <div style="color: #2ed573; font-weight: 700; margin-bottom: 8px; font-size: 0.9rem;">💰 限额逻辑</div>
                        <div style="color: #b8c5d6; font-size: 0.85rem; line-height: 1.6;">
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">净交易额限制</strong>：买入总额 - 卖出总额 ≤ 限额</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">卖出不限制</strong>：卖出是回笼资金，减少净交易额</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">每日重置</strong>：新西兰时间每天0点自动重置</div>
                            <div>• <strong style="color: #fff;">灵活交易</strong>：买入后卖出，可以自由再买入（只要净交易额不超过限额）</div>
                        </div>
                    </div>
                    
                    <!-- 买入卖出逻辑 -->
                    <div style="background: rgba(0, 0, 0, 0.3); padding: 15px; border-radius: 12px; border-left: 3px solid #ffa502;">
                        <div style="color: #ffa502; font-weight: 700; margin-bottom: 8px; font-size: 0.9rem;">📈 买入卖出逻辑</div>
                        <div style="color: #b8c5d6; font-size: 0.85rem; line-height: 1.6;">
                            <div style="margin-bottom: 6px;">• <strong style="color: #2ed573;">买入条件1</strong>：RSI低位 + 价格低于成本 + 布林带下轨（正常套利）</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #2ed573;">买入条件2</strong>：强上涨趋势中，价格略高于成本（≤1%）允许买入</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #2ed573;">买入条件3</strong>：上涨趋势中，从高点回调≥3%时，即使高于成本价也允许买入（已优化）</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #e74c3c;">卖出条件</strong>：RSI高位 + 价格高于成本 + 布林带上轨</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #00d4ff;">分批买入</strong>：下跌趋势中等待更深回调（>1.5-2%），避免过早全仓</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #00d4ff;">分批卖出</strong>：上涨趋势中等待更高价位（>1.5-2%），避免过早卖出</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">回调买入优化</strong>：回调买入时，买入比例降低到28%，并检查利润空间</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">仓位调整</strong>：重仓盈利时抑制买入增强卖出，轻仓亏损时抑制卖出增强买入</div>
                            <div>• <strong style="color: #fff;">小额套利</strong>：降低RSI阈值，放宽条件，提高交易频率</div>
                        </div>
                    </div>
                    
                    <!-- 风控措施 -->
                    <div style="background: rgba(0, 0, 0, 0.3); padding: 15px; border-radius: 12px; border-left: 3px solid #e74c3c;">
                        <div style="color: #e74c3c; font-weight: 700; margin-bottom: 8px; font-size: 0.9rem;">🛡️ 风控措施</div>
                        <div style="color: #b8c5d6; font-size: 0.85rem; line-height: 1.6;">
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">冷却时间</strong>：每次交易后等待1分钟（可配置）</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">每日次数</strong>：每个方向最多30次/天</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">止损保护1</strong>：重仓+大亏损+强下跌趋势时触发止损</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">止损保护2</strong>：回调买入后，如果价格低于新成本价，触发动态止损（卖出70%）</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">止损保护3</strong>：价格持续下跌，即使仓位不重也考虑止损（卖出30%）</div>
                            <div style="margin-bottom: 6px;">• <strong style="color: #fff;">利润空间检查</strong>：买入前估算新成本价，确保有足够利润空间（手续费0.2%+安全边际0.3%）</div>
                            <div>• <strong style="color: #fff;">波动率过滤</strong>：ATR超过6%时暂停交易</div>
                        </div>
                    </div>
                </div>
                <div style="margin-top: 20px; padding: 15px; background: rgba(0, 212, 255, 0.1); border-radius: 8px; border: 1px solid rgba(0, 212, 255, 0.2);">
                    <div style="color: #00d4ff; font-weight: 700; margin-bottom: 8px; font-size: 0.9rem;">💡 策略特点（2024优化版）</div>
                    <div style="color: #b8c5d6; font-size: 0.85rem; line-height: 1.6;">
                        <strong style="color: #fff;">低买高卖</strong>：价格低于成本时买入，高于成本时卖出，确保盈利空间。系统会自动分析市场状态，结合多个技术指标（RSI、MACD、布林带、趋势强度）生成交易信号，并通过多层风控确保交易安全。
                        <br><br>
                        <strong style="color: #fff;">回调买入优化</strong>：在上涨趋势中，如果价格从最近高点回调≥3%，即使高于成本价也允许买入。系统会在买入前估算新成本价，检查是否有足够利润空间（手续费0.2%+安全边际0.3%），并降低买入比例到28%，避免大幅提高成本价。
                        <br><br>
                        <strong style="color: #fff;">动态止损</strong>：如果回调买入后价格下跌，系统会触发动态止损，卖出70%仓位，避免成本价上升后无法盈利。同时，系统还会在价格持续下跌时考虑止损，保护资金安全。
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 资产卡片 -->
        <div class="grid-5">
            <div class="card">
                <div class="card-title" data-i18n="card_total_asset">💎 总资产</div>
                <div class="card-value blue" id="total-value">$--</div>
                <div class="card-sub">Portfolio Value</div>
            </div>
            <div class="card">
                <div class="card-title" data-i18n="card_pnl">📊 持仓盈亏</div>
                <div class="card-value" id="pnl-pct">--%</div>
                <div class="card-sub" id="pnl-usdt">$--</div>
            </div>
            <div class="card">
                <div class="card-title">💰 {symbol}</div>
                <div class="card-value" id="last-price">$--</div>
                <div class="card-sub" id="price-change">24h: --%</div>
            </div>
            <div class="card">
                <div class="card-title" data-i18n="card_holdings">🪙 持仓量</div>
                <div class="card-value yellow" id="base-balance">--</div>
                <div class="card-sub" id="base-value">≈ $--</div>
            </div>
            <div class="card">
                <div class="card-title" data-i18n="card_usdt">💵 可用USDT</div>
                <div class="card-value green" id="usdt-balance">$--</div>
                <div class="card-sub">Available</div>
            </div>
        </div>
        
        <!-- 24H统计 -->
        <div class="stats-row" id="stats-row">
            <div class="stat-item">
                <span class="stat-label">24H 最高:</span>
                <span class="stat-value green" id="high-24h">$--</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">24H 最低:</span>
                <span class="stat-value red" id="low-24h">$--</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">24H 成交量:</span>
                <span class="stat-value blue" id="volume-24h">--</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">成本价:</span>
                <span class="stat-value purple" id="cost-price">$--</span>
            </div>
        </div>
        
        <!-- 今日交易额度 -->
        <div class="stats-row" style="margin-top: 10px; background: linear-gradient(135deg, rgba(46, 213, 115, 0.1) 0%, rgba(255, 165, 2, 0.1) 100%);">
            <div class="stat-item">
                <span class="stat-label">📈 今日已交易:</span>
                <span class="stat-value yellow" id="daily-volume">$0.00</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">📊 每日限额:</span>
                <div style="display: flex; align-items: center; gap: 6px;">
                    <input type="number" id="max-daily-volume-input" value="90" min="0" max="100" step="1"
                        style="width: 70px; background: rgba(52, 152, 219, 0.2); border: 1px solid rgba(52, 152, 219, 0.5); 
                        border-radius: 6px; padding: 4px 8px; color: #3498db; font-size: 1rem; font-weight: 600;
                        text-align: center; outline: none;"
                        onchange="updateDailyLimit(this.value)"
                        onfocus="this.style.borderColor='#3498db'; this.style.boxShadow='0 0 10px rgba(52,152,219,0.3)';"
                        onblur="this.style.borderColor='rgba(52,152,219,0.5)'; this.style.boxShadow='none';">
                    <span style="color: #3498db; font-size: 1rem; font-weight: 600;">%</span>
                    <button onclick="updateDailyLimit(document.getElementById('max-daily-volume-input').value)" 
                        style="background: linear-gradient(135deg, #3498db, #2980b9); border: none; border-radius: 6px;
                        padding: 4px 10px; color: white; cursor: pointer; font-size: 0.8rem; transition: all 0.2s;"
                        onmouseover="this.style.transform='scale(1.05)'; this.style.boxShadow='0 0 15px rgba(52,152,219,0.5)';"
                        onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none';">
                        保存
                    </button>
                </div>
            </div>
            <div class="stat-item">
                <span class="stat-label">💰 剩余额度:</span>
                <span class="stat-value green" id="remaining-volume">$-- (--%)</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">🎯 额度进度:</span>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <div style="width: 80px; height: 8px; background: rgba(255,255,255,0.1); border-radius: 4px; overflow: hidden;">
                        <div id="volume-progress" style="width: 0%; height: 100%; background: linear-gradient(90deg, #2ed573, #ffa502); transition: width 0.3s;"></div>
                    </div>
                    <span class="stat-value" id="volume-percent" style="font-size: 0.9rem;">0%</span>
                </div>
            </div>
            <div class="stat-item">
                <button onclick="resetDailyCounters()" 
                    style="background: linear-gradient(135deg, #e74c3c, #c0392b); border: none; border-radius: 6px;
                    padding: 6px 12px; color: white; cursor: pointer; font-size: 0.85rem; transition: all 0.2s;
                    white-space: nowrap;"
                    onmouseover="this.style.transform='scale(1.05)'; this.style.boxShadow='0 0 15px rgba(231,76,60,0.5)';"
                    onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none';"
                    title="重置今日交易次数（仅用于紧急情况）">
                    🔄 重置次数
                </button>
            </div>
        </div>
        
        <!-- 杠杆设置 -->
        <div class="stats-row" style="margin-top: 10px; background: linear-gradient(135deg, rgba(155, 89, 182, 0.1) 0%, rgba(142, 68, 173, 0.1) 100%);">
            <div class="stat-item">
                <span class="stat-label">⚡ 杠杆交易:</span>
                <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                    <label style="display: flex; align-items: center; gap: 6px; cursor: pointer; color: #b8c5d6;">
                        <input type="checkbox" id="leverage-enabled" onchange="toggleLeverageUI()"
                            style="width: 16px; height: 16px; accent-color: #9b59b6; cursor: pointer;">
                        <span style="font-size: 0.9rem;">启用杠杆</span>
                    </label>
                    <div id="leverage-controls" style="display: none; align-items: center; gap: 6px;">
                        <input type="number" id="leverage-input" value="2" min="2" max="10" step="0.5"
                            style="width: 60px; background: rgba(155, 89, 182, 0.2); border: 1px solid rgba(155, 89, 182, 0.5);
                            border-radius: 6px; padding: 4px 8px; color: #9b59b6; font-size: 1rem; font-weight: 600;
                            text-align: center; outline: none;">
                        <span style="color: #9b59b6; font-size: 1rem; font-weight: 600;">x</span>
                        <button onclick="saveLeverage()"
                            style="background: linear-gradient(135deg, #9b59b6, #8e44ad); border: none; border-radius: 6px;
                            padding: 4px 10px; color: white; cursor: pointer; font-size: 0.8rem;">
                            保存
                        </button>
                    </div>
                    <span id="leverage-status" style="font-size: 0.85rem; color: #2ed573;">现货模式</span>
                </div>
            </div>
        </div>
        
        <!-- 成本价调整系数设置 -->
        <div class="stats-row" style="margin-top: 10px; background: linear-gradient(135deg, rgba(52, 152, 219, 0.1) 0%, rgba(41, 128, 185, 0.1) 100%);">
            <div class="stat-item">
                <span class="stat-label">💰 成本价调整系数:</span>
                <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                    <input type="number" id="cost-adjustment-factor-input" value="1.0" min="0.5" max="2.0" step="0.01"
                        style="width: 80px; background: rgba(52, 152, 219, 0.2); border: 1px solid rgba(52, 152, 219, 0.5); 
                        border-radius: 6px; padding: 4px 8px; color: #3498db; font-size: 1rem; font-weight: 600;
                        text-align: center; outline: none;"
                        onchange="updateCostAdjustmentFactor(this.value)"
                        onfocus="this.style.borderColor='#3498db'; this.style.boxShadow='0 0 10px rgba(52,152,219,0.3)';"
                        onblur="this.style.borderColor='rgba(52,152,219,0.5)'; this.style.boxShadow='none';">
                    <button onclick="updateCostAdjustmentFactor(document.getElementById('cost-adjustment-factor-input').value)" 
                        style="background: linear-gradient(135deg, #3498db, #2980b9); border: none; border-radius: 6px;
                        padding: 4px 10px; color: white; cursor: pointer; font-size: 0.8rem; transition: all 0.2s;"
                        onmouseover="this.style.transform='scale(1.05)'; this.style.boxShadow='0 0 15px rgba(52,152,219,0.5)';"
                        onmouseout="this.style.transform='scale(1)'; this.style.boxShadow='none';">
                        保存
                    </button>
                </div>
            </div>
            <div class="stat-item">
                <span class="stat-label">💡 说明:</span>
                <span class="stat-value" style="font-size: 0.85rem; color: #b8c5d6;">
                    成本价 = 最近10次买入加权平均 × 调整系数（1.0=不变，1.1=+10%，0.9=-10%）
                </span>
            </div>
        </div>
        
        <!-- 信号和指标 -->
        <div class="grid-2">
            <div class="card signal-card" id="signal-card" style="--signal-color: #ffa502; --signal-glow: rgba(255,165,2,0.5);">
                <div class="signal-icon" id="signal-icon">⚪</div>
                <div class="signal-text" id="signal-text">HOLD</div>
                <div class="signal-strength">
                    <div class="signal-strength-bar" id="signal-bar" style="width: 50%; background: #ffa502;"></div>
                </div>
                <div style="margin-top: 25px; display: flex; justify-content: center; gap: 40px;">
                    <div><span class="green" style="font-size: 1.5rem; font-weight: 700;" id="buy-count">0</span><br><span style="color: #5a6a8a; font-size: 0.8rem;">BUY SIGNALS</span></div>
                    <div><span class="red" style="font-size: 1.5rem; font-weight: 700;" id="sell-count">0</span><br><span style="color: #5a6a8a; font-size: 0.8rem;">SELL SIGNALS</span></div>
                </div>
                <!-- 手动交易按钮 -->
                <div class="trade-buttons">
                    <button class="trade-btn buy" id="btn-buy" onclick="manualTrade('BUY')">
                        🟢 买入挂单
                    </button>
                    <button class="trade-btn sell" id="btn-sell" onclick="manualTrade('SELL')">
                        🔴 卖出挂单
                    </button>
                </div>
                <div style="margin-top: 10px; display: flex; justify-content: center;">
                    <button class="trade-btn" id="btn-cancel" onclick="cancelOrders()"
                        style="background: linear-gradient(135deg, #4a3030, #6b2a2a); border: 1px solid #8b3a3a;
                               color: #e06060; font-size: 0.78rem; padding: 7px 20px; width: auto; min-width: 140px;">
                        ✖ 取消所有挂单
                    </button>
                </div>
                <div style="margin-top: 15px; display: flex; justify-content: space-between; font-size: 0.8rem;">
                    <div style="text-align: left;">
                        <div style="color: #5a6a8a;">推荐买入价</div>
                        <div class="green" id="rec-buy-price">$--</div>
                        <div style="color: #5a6a8a; font-size: 0.7rem;" id="rec-buy-discount">--%</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="color: #5a6a8a;">推荐卖出价</div>
                        <div class="red" id="rec-sell-price">$--</div>
                        <div style="color: #5a6a8a; font-size: 0.7rem;" id="rec-sell-premium">+-%</div>
                    </div>
                </div>
                <div style="color: #5a6a8a; font-size: 0.7rem; margin-top: 8px;">GTC挂单 · 最小量×2 · 等待成交</div>
            </div>
            <div class="card">
                <div class="card-title" data-i18n="tech_indicators">📈 技术指标 INDICATORS</div>
                <div class="indicators">
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="rsi_label">RSI (14)</div>
                        <div class="indicator-value" id="rsi">--</div>
                        <div class="indicator-status" id="rsi-status">--</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="trend_strength">趋势强度</div>
                        <div class="indicator-value" id="trend">--</div>
                        <div class="indicator-status" id="trend-status">--</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="bb_position">布林位置</div>
                        <div class="indicator-value" id="bb">--%</div>
                        <div class="indicator-status" id="bb-status">--</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="macd_hist">MACD柱</div>
                        <div class="indicator-value" id="macd">--</div>
                        <div class="indicator-status" id="macd-status">--</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="atr_volatility">ATR波动</div>
                        <div class="indicator-value" id="atr">--%</div>
                        <div class="indicator-status" id="atr-status">--</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="volume_ratio">成交量比</div>
                        <div class="indicator-value" id="vol">--x</div>
                        <div class="indicator-status" id="vol-status">--</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="support">支撑位</div>
                        <div class="indicator-value green" id="support">$--</div>
                        <div class="indicator-status">SUPPORT</div>
                    </div>
                    <div class="indicator">
                        <div class="indicator-label" data-i18n="resistance">阻力位</div>
                        <div class="indicator-value red" id="resistance">$--</div>
                        <div class="indicator-status">RESISTANCE</div>
                    </div>
                </div>
                <!-- 新闻情绪 NEWS SENTIMENT (嵌入在技术指标卡片内) -->
                <div id="news-card" style="margin-top:16px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.08);">
                    <div class="chart-title" data-i18n="news_sentiment" style="margin-bottom:8px;">📰 新闻情绪 NEWS SENTIMENT</div>
                    <div style="display:flex;align-items:center;gap:20px;margin:4px 0 0 0;">
                        <div style="text-align:center;min-width:80px;">
                            <div id="news-score" style="font-size:2em;font-weight:bold;color:#888;">--</div>
                            <div id="news-label" style="font-size:0.85em;color:#666;">加载中</div>
                        </div>
                        <div style="flex:1;">
                            <div id="news-summary" style="color:#aaa;font-size:0.9em;margin-bottom:8px;">--</div>
                            <div id="news-factors" style="display:flex;flex-direction:column;gap:4px;"></div>
                            <div id="news-age" style="color:#555;font-size:0.75em;margin-top:6px;"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- 图表区域 -->
        <div class="grid-3">
            <div class="card">
                <div class="chart-title" data-i18n="price_chart">📈 价格走势 (72H)</div>
                <div class="chart-container">
                    <canvas id="priceChart"></canvas>
                </div>
            </div>
            <div class="card">
                <div class="chart-title" data-i18n="rsi_chart">📊 RSI指标</div>
                <div class="chart-container">
                    <canvas id="rsiChart"></canvas>
                </div>
            </div>
            <div class="card">
                <div class="chart-title" data-i18n="macd_chart">📉 MACD</div>
                <div class="chart-container">
                    <canvas id="macdChart"></canvas>
                </div>
            </div>
        </div>
        
        <!-- 交易和信号记录 -->
        <div class="grid-2">
            <div class="card">
                <div class="card-title" data-i18n="recent_trades">📜 最近交易 TRADES</div>
                <div id="trades-list" style="max-height: 300px; overflow-y: auto;"></div>
            </div>
            <div class="card">
                <div class="card-title" data-i18n="signals_log">📡 信号记录 SIGNALS</div>
                <div id="signals-list" style="max-height: 300px; overflow-y: auto;"></div>
            </div>
        </div>
    </div>
    
    <!-- 状态栏 -->
    <div class="status-bar">
        <div>
            <span class="status-dot active"></span>
            <span data-i18n="engine_active">TRADING ENGINE ACTIVE</span>
        </div>
        <div id="cycle-info">CYCLE: -- | UPDATED: --</div>
    </div>

<script src="/static/chart.min.js"></script>
<script>
    // v5.5.13 i18n: 中英文切换 (字典从 Python 注入,避免 f-string 转义)
    const I18N = {i18n_json};
    function applyI18n() {{
        const lang = localStorage.getItem('lang') || 'zh';
        document.documentElement.lang = lang;
        const dict = I18N[lang] || I18N['zh'];
        document.querySelectorAll('[data-i18n]').forEach(el => {{
            const k = el.getAttribute('data-i18n');
            if (dict[k]) el.textContent = dict[k];
        }});
        const btn = document.getElementById('lang-toggle');
        if (btn) btn.textContent = dict['lang_btn'] || '🌐';
        updateThemeButton();
    }}
    function toggleLang() {{
        const cur = localStorage.getItem('lang') || 'zh';
        localStorage.setItem('lang', cur === 'zh' ? 'en' : 'zh');
        applyI18n();
    }}
    window.addEventListener('DOMContentLoaded', applyI18n);
</script>
<script>
    let priceChart, rsiChart, macdChart;
    const THEME_KEY = 'quant_theme';

    function getCurrentLangDict() {{
        const lang = localStorage.getItem('lang') || 'zh';
        return I18N[lang] || I18N['zh'];
    }}

    function updateThemeButton() {{
        const btn = document.getElementById('theme-toggle');
        if (!btn) return;
        const dict = getCurrentLangDict();
        btn.textContent = document.body.dataset.theme === 'dark'
            ? (dict.theme_btn_light || '浅色')
            : (dict.theme_btn_dark || '深色');
    }}

    function applyTheme(theme) {{
        const nextTheme = theme === 'dark' ? 'dark' : 'light';
        document.body.dataset.theme = nextTheme;
        localStorage.setItem(THEME_KEY, nextTheme);
        updateThemeButton();
        applyChartTheme();
    }}

    function toggleTheme() {{
        applyTheme(document.body.dataset.theme === 'dark' ? 'light' : 'dark');
    }}

    function getThemePalette() {{
        const isDark = document.body.dataset.theme === 'dark';
        return isDark
            ? {{ grid: 'rgba(255,255,255,0.06)', tick: '#8a97aa', price: '#00d4ff', sma: '#ffa502', band: 'rgba(183,148,244,0.55)' }}
            : {{ grid: 'rgba(100,116,139,0.16)', tick: '#64748b', price: '#2563eb', sma: '#d97706', band: 'rgba(124,58,237,0.42)' }};
    }}

    function applyChartTheme() {{
        if (typeof Chart === 'undefined') return;
        const palette = getThemePalette();
        [priceChart, rsiChart, macdChart].forEach(chart => {{
            if (!chart || !chart.options || !chart.options.scales) return;
            Object.values(chart.options.scales).forEach(axis => {{
                if (axis.grid) axis.grid.color = palette.grid;
                if (axis.ticks) axis.ticks.color = palette.tick;
            }});
        }});
        if (priceChart) {{
            priceChart.data.datasets[0].borderColor = palette.price;
            priceChart.data.datasets[1].borderColor = palette.sma;
            priceChart.data.datasets[2].borderColor = palette.band;
            priceChart.data.datasets[3].borderColor = palette.band;
        }}
        if (rsiChart) rsiChart.data.datasets[0].borderColor = document.body.dataset.theme === 'dark' ? '#b794f4' : '#7c3aed';
        [priceChart, rsiChart, macdChart].forEach(chart => chart && chart.update('none'));
    }}

    document.addEventListener('DOMContentLoaded', function() {{
        applyTheme(localStorage.getItem(THEME_KEY) || 'light');
    }});
    
    // 显示Toast通知
    function showToast(message, type = 'info') {{
        const toast = document.createElement('div');
        toast.className = 'toast ' + type;
        toast.textContent = message;
        document.body.appendChild(toast);
        
        setTimeout(() => {{
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        }}, 4000);
    }}
    
    // 登录检查
    function checkAuth() {{
        const token = localStorage.getItem('token');
        if (!token) {{
            window.location.href = '/login';
            return false;
        }}
        // 验证token是否有效
        try {{
            const payload = JSON.parse(atob(token.split('.')[1]));
            const exp = payload.exp * 1000; // JWT exp是秒，需要转换为毫秒
            if (Date.now() > exp) {{
                // token已过期
                localStorage.removeItem('token');
                localStorage.removeItem('username');
                window.location.href = '/login';
                return false;
            }}
        }} catch (e) {{
            // token格式错误
            localStorage.removeItem('token');
            localStorage.removeItem('username');
            window.location.href = '/login';
            return false;
        }}
        return true;
    }}
    
    // 退出登录
    async function logout() {{
        if (confirm('确定要退出登录吗？')) {{
            localStorage.removeItem('token');
            localStorage.removeItem('username');
            window.location.href = '/login';
        }}
    }}
    
    // 显示策略说明弹出框
    function showStrategyModal() {{
        document.getElementById('strategyModal').style.display = 'block';
    }}
    
    // 关闭策略说明弹出框
    function closeStrategyModal() {{
        document.getElementById('strategyModal').style.display = 'none';
    }}
    
    // 点击背景关闭弹出框
    document.addEventListener('DOMContentLoaded', function() {{
        const modal = document.getElementById('strategyModal');
        if (modal) {{
            modal.addEventListener('click', function(e) {{
                if (e.target === modal) {{
                    closeStrategyModal();
                }}
            }});
        }}
        
        // 检查登录状态
        if (!checkAuth()) {{
            return;
        }}
        
        // 显示用户名
        const username = localStorage.getItem('username') || 'admin';
        const usernameDisplay = document.getElementById('username-display');
        if (usernameDisplay) {{
            usernameDisplay.textContent = username;
        }}
        
        // 为所有API请求添加token
        const originalFetch = window.fetch;
        window.fetch = function(...args) {{
            const token = localStorage.getItem('token');
            if (token && args[0] && typeof args[0] === 'string' && args[0].startsWith('/api/')) {{
                if (!args[1]) args[1] = {{}};
                if (!args[1].headers) args[1].headers = {{}};
                args[1].headers['Authorization'] = `Bearer ${{token}}`;
            }}
            return originalFetch.apply(this, args);
        }};
    }});
    
    // 取消所有挂单
    async function cancelOrders() {{
        const btn = document.getElementById('btn-cancel');
        const orig = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ 撤单中...';
        try {{
            const response = await fetch('/api/cancel_orders', {{ method: 'POST' }});
            const result = await response.json();
            if (result.status === 'OK') {{
                const n = result.cancelled ?? 0;
                showToast(n > 0 ? `✅ 已撤销 ${{n}} 笔挂单` : '✅ 当前无挂单', 'success');
                if (n > 0) setTimeout(updateData, 800);
            }} else {{
                showToast(`❌ 撤单失败: ${{result.message || result.error}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 请求失败: ${{e.message}}`, 'error');
        }} finally {{
            btn.disabled = false;
            btn.textContent = orig;
        }}
    }}

    // 手动交易
    async function manualTrade(side) {{
        const btnBuy = document.getElementById('btn-buy');
        const btnSell = document.getElementById('btn-sell');
        
        // 禁用按钮
        btnBuy.disabled = true;
        btnSell.disabled = true;
        const originalBuyText = btnBuy.textContent;
        const originalSellText = btnSell.textContent;
        const btn = side === 'BUY' ? btnBuy : btnSell;
        btn.textContent = '⏳ 挂单中...';
        
        try {{
            const response = await fetch('/api/manual_trade', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ side: side }})
            }});
            
            const result = await response.json();
            
            if (result.status === 'OK') {{
                const diff = side === 'BUY' 
                    ? ((result.last_price - result.price) / result.last_price * 100).toFixed(2)
                    : ((result.price - result.last_price) / result.last_price * 100).toFixed(2);
                showToast(`✅ 挂单成功! ${{side}} ${{result.qty}} @ $${{result.price.toFixed(4)}} (${{side === 'BUY' ? '-' : '+'}}${{diff}}%)`, 'success');
                setTimeout(updateData, 1000);
                setTimeout(updateRecommendedPrices, 1000);
            }} else if (result.status === 'DRYRUN') {{
                showToast(`🔄 模拟挂单: ${{side}} ${{result.qty}} @ $${{result.price.toFixed(4)}}`, 'info');
            }} else {{
                showToast(`❌ 挂单失败: ${{result.message || result.error}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 请求失败: ${{e.message}}`, 'error');
        }} finally {{
            btnBuy.disabled = false;
            btnSell.disabled = false;
            btnBuy.textContent = originalBuyText;
            btnSell.textContent = originalSellText;
        }}
    }}
    
    // 检查杠杆状态
    async function checkLeverageStatus() {{
        try {{
            showToast('🔍 正在检查杠杆状态...', 'info');
            const response = await fetch('/api/check_leverage');
            const result = await response.json();
            
            if (result.status === 'enabled') {{
                showToast(`✅ ${{result.message}}`, 'success');
            }} else if (result.status === 'maybe_disabled') {{
                showToast(`⚠️ ${{result.message}}`, 'warning');
                // 显示详细信息和手动检查链接
                setTimeout(() => {{
                    const manualCheck = confirm(
                        `${{result.message}}\\n\\n` +
                        `建议手动检查：\\n` +
                        `1. 登录Bybit账户\\n` +
                        `2. 进入现货交易页面\\n` +
                        `3. 查看是否有杠杆选项\\n\\n` +
                        `是否打开Bybit交易页面？`
                    );
                    if (manualCheck && result.manual_check_url) {{
                        window.open(result.manual_check_url, '_blank');
                    }}
                }}, 1000);
            }} else {{
                showToast(`❌ ${{result.message}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 检查失败: ${{e.message}}`, 'error');
        }}
    }}
    
    // 杠杆 UI 切换
    function toggleLeverageUI() {{
        const enabled = document.getElementById('leverage-enabled').checked;
        const controls = document.getElementById('leverage-controls');
        const status = document.getElementById('leverage-status');
        controls.style.display = enabled ? 'flex' : 'none';
        status.textContent = enabled ? '杠杆模式' : '现货模式';
        status.style.color = enabled ? '#9b59b6' : '#2ed573';
        if (!enabled) {{ saveLeverage(); }}
    }}

    // 保存杠杆设置
    async function saveLeverage() {{
        const enabled = document.getElementById('leverage-enabled').checked;
        const leverageVal = parseFloat(document.getElementById('leverage-input').value) || 2;
        if (enabled && (leverageVal < 2 || leverageVal > 10)) {{
            showToast('❌ 杠杆倍数须在 2-10 之间', 'error');
            return;
        }}
        try {{
            const response = await fetch('/api/update_leverage', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ enabled: enabled, leverage: enabled ? leverageVal : 1.0 }})
            }});
            const result = await response.json();
            if (result.status === 'OK') {{
                showToast(`✅ ${{enabled ? leverageVal + 'x 杠杆已启用' : '已切换到现货模式'}}`, 'success');
                setTimeout(updateData, 500);
            }} else {{
                showToast(`❌ ${{result.error || '更新失败'}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 请求失败: ${{e.message}}`, 'error');
        }}
    }}
    
    // 更新成本价调整系数
    async function updateCostAdjustmentFactor(newFactor) {{
        const factor = parseFloat(newFactor);
        if (isNaN(factor) || factor < 0.5) {{
            showToast('❌ 请输入有效的成本价调整系数（≥0.5）', 'error');
            return;
        }}
        if (factor > 2.0) {{
            showToast('❌ 成本价调整系数不能超过 2.0（风险过高）', 'error');
            return;
        }}
        
        try {{
            const response = await fetch('/api/update_cost_adjustment_factor', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ cost_adjustment_factor: factor }})
            }});
            
            const result = await response.json();
            
            if (result.status === 'OK') {{
                showToast(`✅ ${{result.message}}`, 'success');
                // 更新输入框的值
                document.getElementById('cost-adjustment-factor-input').value = result.cost_adjustment_factor;
                // 立即刷新数据
                setTimeout(updateData, 500);
            }} else {{
                showToast(`❌ ${{result.error || '更新失败'}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 请求失败: ${{e.message}}`, 'error');
        }}
    }}
    
    // 更新每日限额（百分比模式）
    async function updateDailyLimit(newLimit) {{
        const limit = parseFloat(newLimit);
        if (isNaN(limit) || limit < 0) {{
            showToast('❌ 请输入有效的限额百分比', 'error');
            return;
        }}
        if (limit > 100) {{
            showToast('❌ 限额百分比不能超过 100%', 'error');
            return;
        }}
        
        try {{
            const response = await fetch('/api/update_daily_limit', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ limit: limit }})
            }});
            
            const result = await response.json();
            
            if (result.status === 'OK') {{
                showToast(`✅ ${{result.message}}`, 'success');
                // 更新输入框的值
                document.getElementById('max-daily-volume-input').value = result.new_limit_pct;
                // 立即刷新数据
                setTimeout(updateData, 500);
            }} else {{
                showToast(`❌ ${{result.error || '更新失败'}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 请求失败: ${{e.message}}`, 'error');
        }}
    }}
    
    // 重置今日交易次数
    async function resetDailyCounters() {{
        if (!confirm('确定要重置今日交易次数计数器吗？\\n\\n这将清除今日的买入/卖出次数统计，允许继续交易。\\n\\n注意：仅用于紧急情况！')) {{
            return;
        }}
        
        try {{
            const response = await fetch('/api/reset_daily_counters', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }}
            }});
            
            const result = await response.json();
            
            if (result.status === 'OK') {{
                showToast(`✅ ${{result.message}}`, 'success');
                // 立即刷新数据
                setTimeout(updateData, 500);
            }} else {{
                showToast(`❌ ${{result.error || '重置失败'}}`, 'error');
            }}
        }} catch (e) {{
            showToast(`❌ 请求失败: ${{e.message}}`, 'error');
        }}
    }}
    
    // 更新推荐价格
    async function updateRecommendedPrices() {{
        try {{
            const data = await fetch('/api/recommended_price').then(r => r.json());
            if (data.error) return;
            
            document.getElementById('rec-buy-price').textContent = '$' + data.buy_price.toFixed(4);
            document.getElementById('rec-buy-discount').textContent = '-' + data.buy_discount_pct + '%';
            document.getElementById('rec-sell-price').textContent = '$' + data.sell_price.toFixed(4);
            document.getElementById('rec-sell-premium').textContent = '+' + data.sell_premium_pct + '%';
        }} catch (e) {{
            console.error('获取推荐价格失败:', e);
        }}
    }}
    
    // 初始化图表
    function initCharts() {{
        // 检查 Chart.js 是否已加载
        if (typeof Chart === 'undefined') {{
            if (typeof console !== 'undefined') {{
                console.warn('Chart.js 未加载，延迟初始化图表');
            }}
            setTimeout(initCharts, 500);
            return;
        }}
        
        const palette = getThemePalette();
        const chartOptions = {{
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
                x: {{ grid: {{ color: palette.grid }}, ticks: {{ color: palette.tick, maxTicksLimit: 8 }} }},
                y: {{ grid: {{ color: palette.grid }}, ticks: {{ color: palette.tick }} }}
            }}
        }};

        priceChart = new Chart(document.getElementById('priceChart'), {{
            type: 'line',
            data: {{ labels: [], datasets: [
                {{ label: 'Price', data: [], borderColor: palette.price, borderWidth: 2, fill: false, tension: 0, pointRadius: 0 }},
                {{ label: 'SMA24', data: [], borderColor: palette.sma, borderWidth: 1, fill: false, tension: 0, pointRadius: 0 }},
                {{ label: 'BB Upper', data: [], borderColor: palette.band, borderWidth: 1, borderDash: [5,5], fill: false, pointRadius: 0 }},
                {{ label: 'BB Lower', data: [], borderColor: palette.band, borderWidth: 1, borderDash: [5,5], fill: false, pointRadius: 0 }}
            ]}},
            options: chartOptions
        }});

        rsiChart = new Chart(document.getElementById('rsiChart'), {{
            type: 'line',
            data: {{ labels: [], datasets: [{{ label: 'RSI', data: [], borderColor: document.body.dataset.theme === 'dark' ? '#b794f4' : '#7c3aed', borderWidth: 2, fill: false, tension: 0, pointRadius: 0 }}]}},
            options: {{
                ...chartOptions,
                scales: {{
                    ...chartOptions.scales,
                    y: {{ ...chartOptions.scales.y, min: 0, max: 100 }}
                }},
                plugins: {{
                    legend: {{ display: false }},
                    annotation: {{
                        annotations: {{
                            line1: {{ type: 'line', yMin: 30, yMax: 30, borderColor: '#00ff88', borderWidth: 1, borderDash: [5,5] }},
                            line2: {{ type: 'line', yMin: 70, yMax: 70, borderColor: '#ff4757', borderWidth: 1, borderDash: [5,5] }}
                        }}
                    }}
                }}
            }}
        }});
        
        macdChart = new Chart(document.getElementById('macdChart'), {{
            type: 'bar',
            data: {{ labels: [], datasets: [{{ label: 'MACD Hist', data: [], backgroundColor: [] }}]}},
            options: chartOptions
        }});
        applyChartTheme();
    }}
    
    // 更新图表
    async function updateCharts() {{
        // 确保图表已初始化
        if (!priceChart || !rsiChart || !macdChart) {{
            console.warn('图表尚未初始化，跳过更新');
            return;
        }}
        try {{
            const response = await fetch('/api/klines');
            if (!response.ok) {{
                console.error('K线API响应错误:', response.status);
                return;
            }}
            const data = await response.json();
            if (data.error) {{
                console.error('K线数据错误:', data.error);
                return;
            }}

            // 验证数据
            if (!data.labels || !data.prices || data.prices.length === 0) {{
                console.warn('K线数据为空');
                return;
            }}

            console.log('更新图表, 数据点:', data.prices.length);

            // 价格图表
            priceChart.data.labels = data.labels;
            priceChart.data.datasets[0].data = data.prices;
            priceChart.data.datasets[1].data = data.sma24;
            priceChart.data.datasets[2].data = data.bb_upper;
            priceChart.data.datasets[3].data = data.bb_lower;
            priceChart.update('none');
            
            // RSI图表
            rsiChart.data.labels = data.labels;
            rsiChart.data.datasets[0].data = data.rsi;
            rsiChart.update('none');
            
            // MACD图表
            macdChart.data.labels = data.labels;
            macdChart.data.datasets[0].data = data.macd_hist;
            const isDark = document.body.dataset.theme === 'dark';
            macdChart.data.datasets[0].backgroundColor = data.macd_hist.map(v => v >= 0
                ? (isDark ? 'rgba(0,255,136,0.7)' : 'rgba(5,150,105,0.72)')
                : (isDark ? 'rgba(255,71,87,0.7)' : 'rgba(220,38,38,0.72)'));
            macdChart.update('none');
            
        }} catch (e) {{ 
            console.error('图表更新失败:', e); 
        }}
    }}
    
    // 更新数据
    async function updateData() {{
        try {{
            // v4.4: 只获取一次config, 后面复用 (之前这里和下面各调了一次)
            let _configData = null;
            try {{
                const configResponse = await fetch('/api/config');
                if (configResponse.ok) {{
                    _configData = await configResponse.json();
                    const costFactor = _configData.cost_adjustment_factor || 1.0;
                    const factorInput = document.getElementById('cost-adjustment-factor-input');
                    if (factorInput) {{ factorInput.value = costFactor; }}
                    // 同步杠杆开关状态
                    const levEnabled = _configData.leverage_enabled || false;
                    const levValue = _configData.leverage || 1.0;
                    const levCheckbox = document.getElementById('leverage-enabled');
                    const levInput = document.getElementById('leverage-input');
                    const levControls = document.getElementById('leverage-controls');
                    const levStatus = document.getElementById('leverage-status');
                    if (levCheckbox) {{ levCheckbox.checked = levEnabled; }}
                    if (levInput && levEnabled) {{ levInput.value = levValue; }}
                    if (levControls) {{ levControls.style.display = levEnabled ? 'flex' : 'none'; }}
                    if (levStatus) {{
                        levStatus.textContent = levEnabled ? levValue + 'x 杠杆' : '现货模式';
                        levStatus.style.color = levEnabled ? '#9b59b6' : '#2ed573';
                    }}
                }}
            }} catch (e) {{
                console.warn('获取配置失败:', e);
            }}

            const portfolio = await fetch('/api/portfolio').then(r => r.json());
            if (!portfolio.error) {{
                document.getElementById('total-value').textContent = '$' + portfolio.total_value.toFixed(2);
                
                const pnlPct = document.getElementById('pnl-pct');
                pnlPct.textContent = (portfolio.pnl_pct >= 0 ? '↑ ' : '↓ ') + portfolio.pnl_pct.toFixed(2) + '%';
                pnlPct.className = 'card-value ' + (portfolio.pnl_pct >= 0 ? 'green' : 'red');
                
                document.getElementById('pnl-usdt').textContent = (portfolio.pnl_usdt >= 0 ? '+' : '') + '$' + portfolio.pnl_usdt.toFixed(2);
                document.getElementById('pnl-usdt').className = 'card-sub ' + (portfolio.pnl_usdt >= 0 ? 'green' : 'red');
                
                document.getElementById('last-price').textContent = '$' + portfolio.last_price.toFixed(4);
                document.getElementById('last-price').className = 'card-value ' + (portfolio.price_change_24h >= 0 ? 'green' : 'red');
                
                const pc = document.getElementById('price-change');
                pc.textContent = '24h: ' + (portfolio.price_change_24h >= 0 ? '+' : '') + portfolio.price_change_24h.toFixed(2) + '%';
                pc.className = 'card-sub ' + (portfolio.price_change_24h >= 0 ? 'green' : 'red');
                
                document.getElementById('base-balance').textContent = portfolio.base_balance.toFixed(4);
                document.getElementById('base-value').textContent = '≈ $' + (portfolio.base_balance * portfolio.last_price).toFixed(2);
                document.getElementById('usdt-balance').textContent = '$' + portfolio.usdt_balance.toFixed(2);
                
                document.getElementById('high-24h').textContent = '$' + portfolio.high_24h.toFixed(4);
                document.getElementById('low-24h').textContent = '$' + portfolio.low_24h.toFixed(4);
                document.getElementById('volume-24h').textContent = (portfolio.volume_24h / 1000000).toFixed(2) + 'M';
                document.getElementById('cost-price').textContent = '$' + portfolio.cost_price.toFixed(4);
                
                // 今日交易额度（百分比模式，只限制买入总额）
                const netDailyVol = portfolio.daily_volume || 0;  // 净交易额 = 买入 - 卖出（用于显示）
                const buyDailyVol = portfolio.daily_buy_volume || 0;  // 买入总额（用于限制）
                const sellDailyVol = portfolio.daily_sell_volume || 0;  // 卖出总额（用于显示）
                const maxDailyVolPct = portfolio.max_daily_volume_pct || 0;
                const maxDailyVolUsdt = portfolio.max_daily_volume_usdt || 0;
                const remainingVol = portfolio.remaining_volume >= 0 ? portfolio.remaining_volume : maxDailyVolUsdt;
                const portfolioLeverage = portfolio.leverage || 1.0;
                
                // 更新杠杆倍数输入框
                const leverageInput = document.getElementById('leverage-input');
                if (leverageInput && document.activeElement !== leverageInput) {{
                    leverageInput.value = portfolioLeverage;
                }}
                
                // v4.4: 复用上面已获取的config (不再重复请求)
                if (_configData) {{
                    const costFactor = _configData.cost_adjustment_factor || 1.0;
                    const factorInput = document.getElementById('cost-adjustment-factor-input');
                    if (factorInput && document.activeElement !== factorInput) {{
                        factorInput.value = costFactor;
                    }}
                }}
                // 买入总额百分比（相对于限额）
                const volPercent = maxDailyVolUsdt > 0 ? (buyDailyVol / maxDailyVolUsdt * 100) : 0;
                const remainingVolPct = maxDailyVolUsdt > 0 ? (remainingVol / maxDailyVolUsdt * 100) : 0;
                
                // 显示买入总额（用于限制）和净交易额（用于显示）
                const volDisplay = '买入$' + buyDailyVol.toFixed(2) + '/' + maxDailyVolUsdt.toFixed(2) + ' | 净' + (netDailyVol >= 0 ? '+' : '') + '$' + netDailyVol.toFixed(2) + ' (买' + buyDailyVol.toFixed(2) + '-卖' + sellDailyVol.toFixed(2) + ')';
                document.getElementById('daily-volume').textContent = volDisplay;
                // 更新输入框的值（只在用户没有聚焦时更新，避免打断用户输入）
                const limitInput = document.getElementById('max-daily-volume-input');
                if (document.activeElement !== limitInput) {{
                    limitInput.value = maxDailyVolPct > 0 ? maxDailyVolPct : 0;
                }}
                // 显示剩余额度：同时显示 USDT 金额和百分比
                if (remainingVol >= 0 && maxDailyVolUsdt > 0) {{
                    document.getElementById('remaining-volume').textContent = '$' + remainingVol.toFixed(2) + ' (' + remainingVolPct.toFixed(1) + '%)';
                }} else {{
                    document.getElementById('remaining-volume').textContent = '无限制';
                }}
                document.getElementById('remaining-volume').className = 'stat-value ' + (remainingVol > maxDailyVolUsdt * 0.2 ? 'green' : remainingVol > 0 ? 'yellow' : 'red');
                document.getElementById('volume-progress').style.width = Math.min(100, volPercent) + '%';
                document.getElementById('volume-percent').textContent = volPercent.toFixed(1) + '%';
            }}
            
            const ind = await fetch('/api/indicators').then(r => r.json());
            if (!ind.error) {{
                const setIndicator = (id, value, statusId, statusText, colorClass) => {{
                    document.getElementById(id).textContent = value;
                    document.getElementById(id).className = 'indicator-value ' + colorClass;
                    if (statusId) {{
                        document.getElementById(statusId).textContent = statusText;
                        document.getElementById(statusId).className = 'indicator-status ' + colorClass;
                    }}
                }};
                
                const rsi = ind.rsi14;
                setIndicator('rsi', rsi.toFixed(1), 'rsi-status', rsi < 30 ? '超卖' : rsi > 70 ? '超买' : '中性', rsi < 30 ? 'green' : rsi > 70 ? 'red' : 'yellow');
                
                const trend = ind.trend;
                setIndicator('trend', trend.toFixed(0), 'trend-status', trend > 30 ? '上涨' : trend < -30 ? '下跌' : '震荡', trend > 30 ? 'green' : trend < -30 ? 'red' : 'yellow');
                
                const bb = ind.bb_position;
                setIndicator('bb', (bb * 100).toFixed(1) + '%', 'bb-status', bb < 0.2 ? '下轨' : bb > 0.8 ? '上轨' : '中间', bb < 0.2 ? 'green' : bb > 0.8 ? 'red' : 'yellow');
                
                const macd = ind.macd_hist;
                setIndicator('macd', macd.toFixed(6), 'macd-status', macd > 0 ? '多头' : '空头', macd > 0 ? 'green' : 'red');
                
                setIndicator('atr', ind.atr_pct.toFixed(2) + '%', 'atr-status', ind.atr_pct > 4 ? '高波动' : '正常', ind.atr_pct > 4 ? 'red' : 'yellow');
                setIndicator('vol', ind.volume_ratio.toFixed(2) + 'x', 'vol-status', ind.volume_ratio > 1.5 ? '放量' : '正常', ind.volume_ratio > 1.5 ? 'blue' : 'yellow');
                
                document.getElementById('support').textContent = '$' + ind.support.toFixed(4);
                document.getElementById('resistance').textContent = '$' + ind.resistance.toFixed(4);

                // 信号
                let buySignals = 0, sellSignals = 0;
                if (rsi < 30) buySignals++; else if (rsi > 70) sellSignals++;
                if (bb < 0.2) buySignals++; else if (bb > 0.8) sellSignals++;
                if (macd > 0) buySignals++; else sellSignals++;
                if (trend > 30) buySignals++; else if (trend < -30) sellSignals++;
                
                document.getElementById('buy-count').textContent = buySignals;
                document.getElementById('sell-count').textContent = sellSignals;
                
                const signalCard = document.getElementById('signal-card');
                const signalBar = document.getElementById('signal-bar');
                const strength = Math.max(buySignals, sellSignals) * 25;
                
                if (buySignals >= 3) {{
                    document.getElementById('signal-icon').textContent = '🟢';
                    document.getElementById('signal-text').textContent = 'BUY';
                    document.getElementById('signal-text').className = 'signal-text green';
                    signalCard.style.setProperty('--signal-color', '#00ff88');
                    signalCard.style.setProperty('--signal-glow', 'rgba(0,255,136,0.5)');
                    signalBar.style.background = '#00ff88';
                }} else if (sellSignals >= 3) {{
                    document.getElementById('signal-icon').textContent = '🔴';
                    document.getElementById('signal-text').textContent = 'SELL';
                    document.getElementById('signal-text').className = 'signal-text red';
                    signalCard.style.setProperty('--signal-color', '#ff4757');
                    signalCard.style.setProperty('--signal-glow', 'rgba(255,71,87,0.5)');
                    signalBar.style.background = '#ff4757';
                }} else {{
                    document.getElementById('signal-icon').textContent = '⚪';
                    document.getElementById('signal-text').textContent = 'HOLD';
                    document.getElementById('signal-text').className = 'signal-text yellow';
                    signalCard.style.setProperty('--signal-color', '#ffa502');
                    signalCard.style.setProperty('--signal-glow', 'rgba(255,165,2,0.5)');
                    signalBar.style.background = '#ffa502';
                }}
                signalBar.style.width = strength + '%';

                // 新闻情绪更新
                const ns = ind.news_sentiment || 0;
                const nf = ind.news_key_factors || [];
                const nsEl = document.getElementById('news-score');
                const nlEl = document.getElementById('news-label');
                if (nsEl) {{
                    nsEl.textContent = ns > 0 ? '+' + ns : ns;
                    nsEl.style.color = ns >= 30 ? '#00ff88' : ns <= -30 ? '#ff4757' : ns > 0 ? '#7bed9f' : ns < 0 ? '#ff6b81' : '#888';
                    const actions = {{'aggressive_buy':'强烈看多','cautious_buy':'谨慎看多','hold':'中性','cautious_sell':'谨慎看空','aggressive_sell':'强烈看空'}};
                    nlEl.textContent = actions[ind.news_action] || '中性';
                    nlEl.style.color = nsEl.style.color;
                }}
                const smEl = document.getElementById('news-summary');
                if (smEl) smEl.textContent = ind.news_summary || '暂无数据';
                const ffEl = document.getElementById('news-factors');
                if (ffEl) {{
                    ffEl.innerHTML = nf.map((f, i) => {{
                        const colors = ['#ffa502', '#70a1ff', '#7bed9f'];
                        return '<div style="font-size:0.85em;"><span style="color:' + colors[i % 3] + '">▸</span> ' + f + '</div>';
                    }}).join('');
                }}
                const ageEl = document.getElementById('news-age');
                if (ageEl) {{
                    const age = ind.news_age_min || 0;
                    ageEl.textContent = age < 999 ? '更新于 ' + Math.round(age) + ' 分钟前' : '尚未获取';
                }}
            }}

            // 交易记录（挂单+已成交）
            const trades = await fetch('/api/trades').then(r => r.json());
            if (Array.isArray(trades)) {{
                document.getElementById('trades-list').innerHTML = trades.slice(0, 10).map(t => {{
                    const isPending = t.status === 'PENDING';
                    const icon = isPending ? '⏳' : (t.side === 'Buy' ? '🟢' : '🔴');
                    const cls = t.side === 'Buy' ? 'buy' : 'sell';
                    const time = new Date(t.ts_ms).toLocaleString('zh-CN', {{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}});
                    const price = parseFloat(t.price || 0).toFixed(2);
                    const statusText = isPending ? '<span style="color:#ffa502">挂单中</span>' : '✅';
                    return `<div class="trade-item ${{cls}}" style="${{isPending ? 'opacity:0.85;border-left:2px solid #ffa502;' : ''}}"><span>${{icon}} ${{t.side}}</span><span>${{t.qty}} @ $${{price}}</span><span style="color:#5a6a8a">${{time}}</span><span>${{statusText}}</span></div>`;
                }}).join('') || '<div style="color:#5a6a8a;text-align:center;padding:40px;">NO TRADES YET</div>';
            }}
            
            // 信号记录
            const signals = await fetch('/api/signals').then(r => r.json());
            if (Array.isArray(signals)) {{
                document.getElementById('signals-list').innerHTML = signals.slice(0, 8).map(s => {{
                    const icon = s.decision === 'BUY' ? '🟢' : s.decision === 'SELL' ? '🔴' : '⚪';
                    const cls = s.decision === 'BUY' ? 'buy' : s.decision === 'SELL' ? 'sell' : '';
                    const time = new Date(s.ts_ms).toLocaleString('zh-CN', {{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}});
                    const reason = (s.reason || '').substring(0, 35);
                    return `<div class="trade-item ${{cls}}"><span>${{icon}} ${{s.decision}}</span><span style="color:#5a6a8a;flex:1;margin:0 10px;font-size:0.8rem">${{reason}}...</span><span style="color:#5a6a8a">${{time}}</span></div>`;
                }}).join('') || '<div style="color:#5a6a8a;text-align:center;padding:40px;">NO SIGNALS YET</div>';
            }}
            
            // 状态
            const status = await fetch('/api/status').then(r => r.json());
            const now = new Date().toLocaleTimeString('zh-CN');
            document.getElementById('cycle-info').textContent = `CYCLE: ${{status.cycle_count}} | UPDATED: ${{now}}`;
            
        }} catch (e) {{ console.error('Update failed:', e); }}
    }}
    
    // 初始化
    initCharts();

    // v4.4: 防止请求堆积 - 上一个请求完成后再等N秒发下一个
    let _dataRunning = false;
    let _chartsRunning = false;
    let _recPriceRunning = false;

    async function safeUpdateData() {{
        if (_dataRunning) return;  // 上一次还没完成, 跳过
        _dataRunning = true;
        try {{ await updateData(); }} catch(e) {{ console.error('Update failed:', e); }}
        _dataRunning = false;
    }}
    async function safeUpdateCharts() {{
        if (_chartsRunning) return;
        _chartsRunning = true;
        try {{ await updateCharts(); }} catch(e) {{ console.error('Charts failed:', e); }}
        _chartsRunning = false;
    }}
    async function safeUpdateRecommendedPrices() {{
        if (_recPriceRunning) return;
        _recPriceRunning = true;
        try {{ await updateRecommendedPrices(); }} catch(e) {{ console.error('RecPrice failed:', e); }}
        _recPriceRunning = false;
    }}

    safeUpdateData();
    safeUpdateCharts();
    safeUpdateRecommendedPrices();
    setInterval(safeUpdateData, 10000);         // 10秒 (从5秒放宽)
    setInterval(safeUpdateCharts, 60000);       // 60秒 (从30秒放宽)
    setInterval(safeUpdateRecommendedPrices, 30000);  // 30秒 (从10秒放宽)
</script>
</body>
</html>
    """
    return HTMLResponse(html)

# ============== 启动 ==============
def main():
    banner = """
╔═══════════════════════════════════════════════════════════════╗
║         ⚡ QUANTUM TRADER v2.0 - 智能量化交易系统 ⚡            ║
╠═══════════════════════════════════════════════════════════════╣
║  🚀 交易引擎: 后台自动运行                                     ║
║  📊 Web看板:  http://localhost:5000                           ║
╚═══════════════════════════════════════════════════════════════╝
    """
    print(banner)  # banner用print，方便终端显示
    
    log.info("=" * 50)
    log.info("智能量化交易系统 v2.0 启动")
    log.info("=" * 50)
    
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    init_db()
    
    log.info(f"配置加载完成 | 币种: {cfg.get('symbols', [])} | 交易开关: {cfg.get('enable_trading', False)}")
    log.info(f"日志文件: logs/trading.log, logs/error.log")
    
    trader_thread = threading.Thread(target=trading_loop, daemon=True)
    trader_thread.start()
    log.info("交易线程已启动")
    
    log.info("Web服务启动中 -> http://localhost:5555")
    uvicorn.run(app, host="0.0.0.0", port=5555, log_level="warning")

if __name__ == "__main__":
    main()
