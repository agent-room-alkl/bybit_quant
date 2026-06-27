# -*- coding: utf-8 -*-
"""
Postgres 存储层 —— db.py 的无状态 drop-in 替代（用于 Vercel serverless）。

对外函数签名与 db.py 完全一致：
    init_db / set_meta / get_meta / log_trade / recent_trades / log_signal / recent_signals
策略与 bot_core 代码无需改动，只把 `import db` 换成 `import db_pg as db` 即可。

设计要点：
- 无内存状态：每次调用从 PG 读写（serverless 每拍都是全新进程）。
- 连接复用：模块级缓存一个连接，warm 实例内复用；断了自动重连。
- schema 固定 bybit_bot，绝不碰 public。
- meta 用 upsert（live 会更新计数/冷却），trades/signals 用 insert。
"""
import os, json, logging
from typing import Optional, Dict, Any, List

log = logging.getLogger("auto_bot")

SCHEMA = "bybit_bot"
MAX_TRADES = 200
MAX_SIGNALS = 500

_conn = None


def _dsn() -> str:
    # serverless 优先池化连接串(6543)；清洗掉 psycopg2 不认的 Prisma/supa 专有参数
    raw = (os.environ.get("POSTGRES_PRISMA_URL")
           or os.environ.get("POSTGRES_URL")
           or os.environ.get("POSTGRES_URL_NON_POOLING"))
    if not raw:
        return raw
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(raw)
    # 只保留 psycopg2 支持的查询参数（sslmode 等），剥离 pgbouncer/supa
    keep = {"sslmode", "connect_timeout", "options", "application_name"}
    q = [(k, v) for k, v in parse_qsl(parts.query) if k in keep]
    if not any(k == "sslmode" for k, _ in q):
        q.append(("sslmode", "require"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _get_conn():
    global _conn
    import psycopg2
    if _conn is not None:
        try:
            # 探活，断了就重连
            with _conn.cursor() as c:
                c.execute("SELECT 1")
            return _conn
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
    _conn = psycopg2.connect(_dsn(), connect_timeout=15)
    _conn.autocommit = True  # 每条语句即时提交，serverless 短生命周期更安全
    return _conn


# ── 初始化（建表，幂等；与迁移脚本一致） ──────────────
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS {SCHEMA}.trades (
    id BIGSERIAL PRIMARY KEY,
    ts_ms BIGINT NOT NULL, symbol TEXT NOT NULL, side TEXT, qty TEXT, price TEXT,
    order_type TEXT, tif TEXT, reason TEXT, status TEXT, order_id TEXT, raw_resp JSONB
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.signals (
    id BIGSERIAL PRIMARY KEY,
    ts_ms BIGINT NOT NULL, symbol TEXT NOT NULL, last_price DOUBLE PRECISION,
    cost_price DOUBLE PRECISION, rsi14 DOUBLE PRECISION, sma12 DOUBLE PRECISION,
    sma24 DOUBLE PRECISION, sma72 DOUBLE PRECISION, vol DOUBLE PRECISION,
    bid1 DOUBLE PRECISION, ask1 DOUBLE PRECISION, decision TEXT, reason TEXT,
    extra JSONB
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON {SCHEMA}.trades (ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON {SCHEMA}.signals (ts_ms DESC);
"""

# log_signal 的固定列（其余进 extra jsonb）
_SIGNAL_BASE = {"ts_ms", "symbol", "last_price", "cost_price", "rsi14", "sma12",
                "sma24", "sma72", "vol", "bid1", "ask1", "decision", "reason"}


def init_db():
    """建表（IF NOT EXISTS）。返回 None 兼容旧代码 conn = init_db()。"""
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(DDL)
        log.info("[存储/PG] schema/表已就绪")
    except Exception as e:
        log.warning(f"[存储/PG] init_db 失败: {e}")
    return None


# ── meta key-value（upsert） ─────────────────────────
def set_meta(key: str, value: str):
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(
                f"INSERT INTO {SCHEMA}.meta(key,value) VALUES(%s,%s) "
                f"ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(value)))
    except Exception as e:
        log.warning(f"[存储/PG] set_meta({key}) 失败: {e}")


def get_meta(key: str) -> Optional[str]:
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(f"SELECT value FROM {SCHEMA}.meta WHERE key=%s", (key,))
            row = c.fetchone()
            return row[0] if row else None
    except Exception as e:
        log.warning(f"[存储/PG] get_meta({key}) 失败: {e}")
        return None


# ── trades ──────────────────────────────────────────
def log_trade(ts_ms: int, symbol: str, side: str, qty: str, price: str,
              order_type: str, tif: str, reason: str, status: str,
              order_id: str, raw_resp: Dict[str, Any]):
    try:
        from psycopg2.extras import Json
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(
                f"INSERT INTO {SCHEMA}.trades"
                f"(ts_ms,symbol,side,qty,price,order_type,tif,reason,status,order_id,raw_resp)"
                f" VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (int(ts_ms), symbol, side, str(qty), str(price or ""), order_type,
                 tif, reason, status, order_id or "",
                 Json(raw_resp) if raw_resp is not None else None))
    except Exception as e:
        log.warning(f"[存储/PG] log_trade 失败: {e}")


def recent_trades(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(
                f"SELECT ts_ms,symbol,side,qty,price,order_type,tif,reason,status,order_id,raw_resp"
                f" FROM {SCHEMA}.trades ORDER BY ts_ms DESC LIMIT %s", (limit,))
            cols = ["ts_ms", "symbol", "side", "qty", "price", "order_type",
                    "tif", "reason", "status", "order_id", "raw_resp"]
            return [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"[存储/PG] recent_trades 失败: {e}")
        return []


# ── signals ─────────────────────────────────────────
def log_signal(ts_ms: int, symbol: str, last_price: float, cost_price: float,
               rsi14: float, sma12: float, sma24: float, sma72: float,
               vol: float, bid1: float, ask1: float,
               decision: str, reason: str,
               extra: Optional[Dict[str, Any]] = None):
    try:
        from psycopg2.extras import Json
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(
                f"INSERT INTO {SCHEMA}.signals"
                f"(ts_ms,symbol,last_price,cost_price,rsi14,sma12,sma24,sma72,vol,bid1,ask1,decision,reason,extra)"
                f" VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (int(ts_ms), symbol, last_price, cost_price, rsi14, sma12, sma24,
                 sma72, vol, bid1, ask1, decision, reason,
                 Json(extra) if extra else None))
    except Exception as e:
        log.warning(f"[存储/PG] log_signal 失败: {e}")


def recent_signals(symbol: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            if symbol:
                c.execute(
                    f"SELECT ts_ms,symbol,last_price,cost_price,rsi14,sma12,sma24,sma72,"
                    f"vol,bid1,ask1,decision,reason,extra FROM {SCHEMA}.signals"
                    f" WHERE symbol=%s ORDER BY ts_ms DESC LIMIT %s", (symbol, limit))
            else:
                c.execute(
                    f"SELECT ts_ms,symbol,last_price,cost_price,rsi14,sma12,sma24,sma72,"
                    f"vol,bid1,ask1,decision,reason,extra FROM {SCHEMA}.signals"
                    f" ORDER BY ts_ms DESC LIMIT %s", (limit,))
            cols = ["ts_ms", "symbol", "last_price", "cost_price", "rsi14", "sma12",
                    "sma24", "sma72", "vol", "bid1", "ask1", "decision", "reason"]
            out = []
            for r in c.fetchall():
                d = dict(zip(cols, r[:-1]))
                if r[-1]:  # extra jsonb 展开回顶层，兼容旧读取
                    d.update(r[-1])
                out.append(d)
            return out
    except Exception as e:
        log.warning(f"[存储/PG] recent_signals 失败: {e}")
        return []


# ── 执行锁（防 cron 重叠重复下单）─────────────────────
# 设计(采纳 Codex review)：
#  - owner token 持有锁，tick 结束显式释放 → 正常结束立即放锁，不漏拍
#  - TTL 设较长(120s>60s间隔)仅作「卡死兜底」，不是正常释放路径
#  - fail-closed：任何异常都 **不** 放行(返回 None)，交易场景宁可漏一拍也不重复下单
def acquire_tick_lock(symbol: str, ttl_sec: int = 120) -> Optional[str]:
    """抢锁。成功返回 owner token（用于释放）；拿不到/异常返回 None（fail-closed）。"""
    import time, uuid
    now = int(time.time())
    owner = uuid.uuid4().hex
    key = f"_tick_lock_{symbol}"
    val = json.dumps({"owner": owner, "exp": now + ttl_sec})
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            # 首次插入→拿锁；已存在且(过期 或 无exp字段)→夺锁；未过期→不返回行
            c.execute(
                f"INSERT INTO {SCHEMA}.meta(key,value) VALUES(%s,%s) "
                f"ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value "
                f"WHERE COALESCE(({SCHEMA}.meta.value::jsonb->>'exp')::bigint, 0) < %s "
                f"RETURNING value",
                (key, val, now))
            return owner if c.fetchone() is not None else None
    except Exception as e:
        log.warning(f"[存储/PG] acquire_tick_lock 异常(fail-closed, 本拍跳过): {e}")
        return None


def release_tick_lock(symbol: str, owner: str):
    """只释放自己持有的锁（owner 匹配才删），避免误删后来者的锁。"""
    key = f"_tick_lock_{symbol}"
    try:
        conn = _get_conn()
        with conn.cursor() as c:
            c.execute(
                f"DELETE FROM {SCHEMA}.meta WHERE key=%s "
                f"AND value::jsonb->>'owner' = %s", (key, owner))
    except Exception as e:
        log.warning(f"[存储/PG] release_tick_lock 异常(将靠TTL兜底): {e}")
