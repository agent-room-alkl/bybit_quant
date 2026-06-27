# -*- coding: utf-8 -*-
"""
只读看板/健康检查接口 GET /api/status
返回最近成交、信号、计数 —— 仅依赖 psycopg2，无 pandas/numpy，冷启动快。
用途：① 部署后 smoke test ② 前端看板数据源 ③ 确认 cron 在写 PG。
"""
import os
import sys
import json

_HERE = os.path.dirname(__file__)
_LIB = os.path.abspath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import db_pg  # noqa: E402

from http.server import BaseHTTPRequestHandler


def gather(detailed: bool) -> dict:
    """detailed=False 时只回 counts（公开可见的最小信息，够当 smoke test）。
    detailed=True（带正确 STATUS_SECRET）才回最近成交/信号明细。"""
    db_pg.init_db()
    conn = db_pg._get_conn()
    counts = {}
    with conn.cursor() as c:
        for t in ("trades", "signals", "meta"):
            c.execute(f"SELECT count(*) FROM bybit_bot.{t}")
            counts[t] = c.fetchone()[0]
    out = {"ok": True, "counts": counts}
    if not detailed:
        out["detail"] = "set STATUS_SECRET and pass ?key= to see trades/signals"
        return out
    trades = db_pg.recent_trades(10)
    signals = db_pg.recent_signals(limit=10)
    last_sig = signals[0] if signals else None
    out["last_signal"] = {
        "ts_ms": last_sig.get("ts_ms"),
        "decision": last_sig.get("decision"),
        "price": last_sig.get("last_price"),
        "reason": (last_sig.get("reason") or "")[:160],
    } if last_sig else None
    out["recent_trades"] = [
        {"ts_ms": t["ts_ms"], "side": t["side"], "qty": t["qty"],
         "price": t["price"], "status": t["status"]} for t in trades]
    out["recent_signals"] = [
        {"ts_ms": s["ts_ms"], "decision": s["decision"],
         "price": s.get("last_price")} for s in signals]
    return out


def _is_detailed(self) -> bool:
    # 未设置 STATUS_SECRET → 永远只回 counts（不公开明细）
    secret = os.environ.get("STATUS_SECRET")
    if not secret:
        return False
    from urllib.parse import urlparse, parse_qs
    key = parse_qs(urlparse(self.path).query).get("key", [None])[0]
    auth = self.headers.get("Authorization")
    return key == secret or auth == f"Bearer {secret}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            out = gather(_is_detailed(self))
            code = 200
        except Exception as e:
            out = {"ok": False, "error": str(e)}
            code = 500
        body = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
