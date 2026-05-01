# -*- coding: utf-8 -*-
"""
轻量存储层：用 JSON 文件 + 内存替代 SQLite
- meta (key-value): JSON 文件持久化，重启不丢失
- trades / signals: JSON 文件持久化，重启后仍可复盘
"""
import os, json, time, threading, logging
from typing import Optional, Dict, Any, List

log = logging.getLogger("auto_bot")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
META_FILE = os.path.join(DATA_DIR, "meta.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.json")

# ── 内存存储 ─────────────────────────────────────────
_meta: Dict[str, str] = {}
_trades: List[Dict[str, Any]] = []       # 最近 200 条
_signals: List[Dict[str, Any]] = []      # 最近 500 条
_lock = threading.Lock()

MAX_TRADES = 200
MAX_SIGNALS = 500


# ── 初始化 ───────────────────────────────────────────
def init_db():
    """启动时从 JSON 文件加载轻量数据"""
    global _meta, _trades, _signals
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, "r", encoding="utf-8") as f:
                _meta = json.load(f)
            log.info(f"[存储] 已加载 {len(_meta)} 条 meta 记录")
        except Exception as e:
            log.warning(f"[存储] meta.json 加载失败: {e}")
            _meta = {}
    for path, name in [(TRADES_FILE, "trades"), (SIGNALS_FILE, "signals")]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if not isinstance(rows, list):
                rows = []
            if name == "trades":
                _trades = rows[:MAX_TRADES]
            else:
                _signals = rows[:MAX_SIGNALS]
            log.info(f"[存储] 已加载 {len(rows)} 条 {name} 记录")
        except Exception as e:
            log.warning(f"[存储] {name}.json 加载失败: {e}")
    return None  # 兼容旧代码 conn = init_db()


def _save_meta():
    """持久化 meta 到 JSON 文件"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = META_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, META_FILE)  # 原子写入
    except Exception as e:
        log.warning(f"[存储] meta 保存失败: {e}")


def _save_ring(path: str, rows: List[Dict[str, Any]]):
    """持久化环形缓冲，避免重启后交易复盘断档。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning(f"[存储] {os.path.basename(path)} 保存失败: {e}")


# ── meta key-value ───────────────────────────────────
def set_meta(key: str, value: str):
    with _lock:
        _meta[key] = value
        _save_meta()


def get_meta(key: str) -> Optional[str]:
    with _lock:
        return _meta.get(key)


# ── trades 环形缓冲 ─────────────────────────────────
def log_trade(ts_ms: int, symbol: str, side: str, qty: str, price: str,
              order_type: str, tif: str, reason: str, status: str,
              order_id: str, raw_resp: Dict[str, Any]):
    record = {
        "ts_ms": int(ts_ms),
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price or ""),
        "order_type": order_type,
        "tif": tif,
        "reason": reason,
        "status": status,
        "order_id": order_id or "",
        "raw_resp": raw_resp,
    }
    with _lock:
        _trades.insert(0, record)  # 最新在前
        if len(_trades) > MAX_TRADES:
            _trades[MAX_TRADES:] = []
        _save_ring(TRADES_FILE, _trades)


def recent_trades(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        return list(_trades[:limit])


# ── signals 环形缓冲 ────────────────────────────────
def log_signal(ts_ms: int, symbol: str, last_price: float, cost_price: float,
               rsi14: float, sma12: float, sma24: float, sma72: float,
               vol: float, bid1: float, ask1: float,
               decision: str, reason: str):
    record = {
        "ts_ms": int(ts_ms),
        "symbol": symbol,
        "last_price": last_price,
        "cost_price": cost_price,
        "rsi14": rsi14,
        "sma12": sma12,
        "sma24": sma24,
        "sma72": sma72,
        "vol": vol,
        "bid1": bid1,
        "ask1": ask1,
        "decision": decision,
        "reason": reason,
    }
    with _lock:
        _signals.insert(0, record)
        if len(_signals) > MAX_SIGNALS:
            _signals[MAX_SIGNALS:] = []
        _save_ring(SIGNALS_FILE, _signals)


def recent_signals(symbol: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        if symbol:
            filtered = [s for s in _signals if s.get("symbol") == symbol]
            return filtered[:limit]
        return list(_signals[:limit])
