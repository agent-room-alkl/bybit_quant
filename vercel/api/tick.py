# -*- coding: utf-8 -*-
"""
Vercel Cron 入口：每分钟被调用一次，跑「一拍」交易循环。
等价于本地 main.py 的 trading_loop 单次迭代，策略逻辑完全复用 bot_core.one_step_for_symbol。

无状态：所有跨拍状态走 PG（db_pg）。用执行锁防 cron 重叠重复下单。
"""
import os
import sys
import json
import logging

# 让 lib/ 下的策略模块可被 import。Vercel 不同运行环境 cwd 可能不同，
# 多候选路径兜底（相对本文件、相对 cwd、相对 /var/task）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.abspath(os.path.join(_HERE, "..", "..", "lib")),
    os.path.join(os.getcwd(), "lib"),
    "/var/task/lib",
    os.path.abspath(os.path.join(_HERE, "..", "lib")),
]
_LIB = next((_p for _p in _CANDIDATES if os.path.isdir(_p)), _CANDIDATES[0])
for _p in _CANDIDATES:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(level=os.environ.get("BYBIT_LOG_LEVEL", "INFO"))
log = logging.getLogger("auto_bot")

# 关键技巧：把 db_pg 注册成 `db`，使 bot_core 里的 `from db import ...` 自动走 PG 版
import db_pg
sys.modules["db"] = db_pg


def _load_cfg() -> dict:
    """加载 config.json 基础配置，并用环境变量覆盖密钥/开关（Vercel 上不放明文密钥）。"""
    cfg_path = os.path.join(_LIB, "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量覆盖（生产密钥只放 Vercel env）
    cfg["api_key"] = os.environ.get("BYBIT_API_KEY", cfg.get("api_key", ""))
    cfg["api_secret"] = os.environ.get("BYBIT_API_SECRET", cfg.get("api_secret", ""))
    cfg["gpt_api_key"] = os.environ.get("GPT_API_KEY", cfg.get("gpt_api_key", ""))
    if os.environ.get("ENABLE_TRADING") is not None:
        cfg["enable_trading"] = os.environ.get("ENABLE_TRADING", "true").lower() == "true"
    return cfg


def run_tick() -> dict:
    from bybit_client import BybitClient
    from bot_core import one_step_for_symbol

    cfg = _load_cfg()
    db_pg.init_db()

    client = BybitClient(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=bool(cfg.get("testnet", False)),
        recv_window_ms=int(cfg.get("recv_window_ms", 5000)),
        account_type=cfg.get("account_type", "UNIFIED"),
    )

    symbols = cfg.get("symbols", ["SOLUSDT"])
    results = {}
    for symbol in symbols:
        # 执行锁：拿不到(上一拍仍在跑 或 PG异常)就跳过，绝不下单（fail-closed）
        owner = db_pg.acquire_tick_lock(symbol, ttl_sec=120)
        if owner is None:
            results[symbol] = {"skipped": "locked (上一拍仍在执行 或 锁不可用)"}
            continue
        try:
            r = one_step_for_symbol(client, cfg, symbol)
            results[symbol] = {
                "decision": r.get("decision"),
                "price": r.get("last_price"),
                "reason": (r.get("reason") or "")[:160],
            }
        except Exception as e:
            log.exception(f"[{symbol}] tick 异常")
            results[symbol] = {"error": str(e)[:200]}
        finally:
            db_pg.release_tick_lock(symbol, owner)  # 正常结束立即放锁，不漏下一拍
    return {"ok": True, "results": results}


# ── Vercel Python serverless 入口 ────────────────────
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def _auth_ok(self) -> bool:
        # Vercel Cron 会带 Authorization: Bearer $CRON_SECRET
        # fail-closed：交易入口不允许裸奔——没配 CRON_SECRET 直接拒绝
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            log.error("CRON_SECRET 未配置，拒绝执行（交易入口必须鉴权）")
            return False
        return self.headers.get("Authorization") == f"Bearer {secret}"

    def do_GET(self):
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        try:
            out = run_tick()
            code = 200
        except Exception as e:
            out = {"ok": False, "error": str(e)}
            code = 500
        body = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
