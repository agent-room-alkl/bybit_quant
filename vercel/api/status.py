# -*- coding: utf-8 -*-
"""
只读看板/健康检查接口 GET /api/status
返回最近成交、信号、计数 —— 仅依赖 psycopg2，无 pandas/numpy，冷启动快。
用途：① 部署后 smoke test ② 前端看板数据源 ③ 确认 cron 在写 PG。
"""
import os
import sys
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.abspath(os.path.join(_HERE, "..", "lib")),
           os.path.join(os.getcwd(), "lib"), "/var/task/lib"]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import db_pg  # noqa: E402

from http.server import BaseHTTPRequestHandler


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Bybit Quant</title>
  <style>
    :root { color-scheme: dark; --bg:#101214; --panel:#181b1f; --line:#2b3138; --text:#f3f5f7; --muted:#8f9aa8; --good:#36d399; --warn:#fbbf24; --bad:#fb7185; --blue:#60a5fa; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 24px; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:20px; font-weight:650; }
    main { padding:20px 24px 32px; max-width:1180px; margin:0 auto; }
    .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    input { height:36px; min-width:260px; background:#0d0f11; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:0 10px; }
    button { height:36px; border:1px solid var(--line); border-radius:6px; background:#222832; color:var(--text); padding:0 12px; cursor:pointer; }
    button:hover { border-color:#46515f; }
    .grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:18px 0; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:88px; }
    .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
    .num { font-size:30px; font-weight:700; margin-top:8px; }
    .row { display:flex; justify-content:space-between; gap:12px; padding:10px 0; border-bottom:1px solid var(--line); }
    .row:last-child { border-bottom:0; }
    .section { margin-top:16px; }
    .section h2 { font-size:15px; margin:0 0 10px; }
    .pill { display:inline-flex; align-items:center; height:24px; padding:0 8px; border-radius:999px; font-size:12px; border:1px solid var(--line); color:var(--muted); }
    .ok { color:var(--good); } .hold { color:var(--warn); } .err { color:var(--bad); } .buy { color:var(--good); } .sell { color:var(--bad); }
    .muted { color:var(--muted); }
    pre { white-space:pre-wrap; word-break:break-word; background:#0d0f11; border:1px solid var(--line); border-radius:8px; padding:12px; overflow:auto; }
    @media (max-width:760px) { header { align-items:flex-start; flex-direction:column; } .grid { grid-template-columns:1fr; } input { min-width:0; width:100%; } .toolbar { width:100%; } button { flex:1; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Bybit Quant</h1>
      <div class="muted" id="stamp">loading...</div>
    </div>
    <div class="toolbar">
      <input id="secret" type="password" placeholder="STATUS_SECRET（可选）" autocomplete="off" />
      <button id="save">保存</button>
      <button id="refresh">刷新</button>
    </div>
  </header>
  <main>
    <div class="grid">
      <div class="card"><div class="label">Trades</div><div class="num" id="trades">-</div></div>
      <div class="card"><div class="label">Signals</div><div class="num" id="signals">-</div></div>
      <div class="card"><div class="label">Meta</div><div class="num" id="meta">-</div></div>
    </div>
    <div class="card section">
      <h2>Latest Signal</h2>
      <div id="lastSignal" class="muted">Set STATUS_SECRET to view details.</div>
    </div>
    <div class="grid section">
      <div class="card" style="grid-column:span 2">
        <h2>Recent Signals</h2>
        <div id="recentSignals" class="muted">No detail loaded.</div>
      </div>
      <div class="card">
        <h2>Recent Trades</h2>
        <div id="recentTrades" class="muted">No detail loaded.</div>
      </div>
    </div>
    <pre id="raw"></pre>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    $("secret").value = localStorage.getItem("STATUS_SECRET") || "";
    $("save").onclick = () => { localStorage.setItem("STATUS_SECRET", $("secret").value.trim()); load(); };
    $("refresh").onclick = () => load();
    function cls(v) {
      v = String(v || "").toUpperCase();
      if (v.includes("BUY") || v === "OK") return "buy";
      if (v.includes("SELL") || v.includes("ERR")) return "sell";
      if (v.includes("HOLD")) return "hold";
      return "";
    }
    function dt(ms) { return ms ? new Date(Number(ms)).toLocaleString() : "-"; }
    function rows(items, render) {
      return items && items.length ? items.map(render).join("") : '<div class="muted">No rows.</div>';
    }
    async function load() {
      const key = $("secret").value.trim();
      const url = key ? `/api/status?key=${encodeURIComponent(key)}` : "/api/status";
      $("stamp").textContent = "Refreshing...";
      try {
        const res = await fetch(url, { cache: "no-store" });
        const data = await res.json();
        $("trades").textContent = data.counts?.trades ?? "-";
        $("signals").textContent = data.counts?.signals ?? "-";
        $("meta").textContent = data.counts?.meta ?? "-";
        $("stamp").textContent = `Updated ${new Date().toLocaleTimeString()} · ${res.status}`;
        $("raw").textContent = JSON.stringify(data, null, 2);
        if (data.last_signal) {
          $("lastSignal").innerHTML = `<div class="row"><span>${dt(data.last_signal.ts_ms)}</span><span class="${cls(data.last_signal.decision)}">${data.last_signal.decision || "-"}</span><span>$${data.last_signal.price ?? "-"}</span></div><div class="muted">${data.last_signal.reason || ""}</div>`;
        } else {
          $("lastSignal").textContent = data.detail || "No latest signal.";
        }
        $("recentSignals").innerHTML = rows(data.recent_signals, (s) => `<div class="row"><span>${dt(s.ts_ms)}</span><span class="${cls(s.decision)}">${s.decision || "-"}</span><span>$${s.price ?? "-"}</span></div>`);
        $("recentTrades").innerHTML = rows(data.recent_trades, (t) => `<div class="row"><span class="${cls(t.side)}">${t.side || "-"}</span><span>${t.qty || "-"}</span><span>$${t.price || "-"}</span><span>${t.status || "-"}</span></div>`);
      } catch (e) {
        $("stamp").textContent = "Error";
        $("raw").textContent = String(e && e.stack || e);
      }
    }
    load();
    setInterval(load, 60000);
  </script>
</body>
</html>"""


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
        from urllib.parse import urlparse
        if urlparse(self.path).path in ("", "/"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
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
