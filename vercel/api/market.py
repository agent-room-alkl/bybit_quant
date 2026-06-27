# -*- coding: utf-8 -*-
"""Read-only market endpoints for the migrated dashboard.

Routes:
  /api/klines      Chart data matching local main.py
  /api/indicators  Indicator card data matching local main.py
"""
import json
import math
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.abspath(os.path.join(_HERE, "..", "lib")),
           os.path.join(os.getcwd(), "lib"), "/var/task/lib"]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from bybit_client import BybitClient  # noqa: E402
from indicators import klines_to_df, enrich_indicators, get_market_condition  # noqa: E402


def _cfg() -> dict:
    paths = [
        os.path.abspath(os.path.join(_HERE, "..", "lib", "config.json")),
        os.path.join(os.getcwd(), "lib", "config.json"),
        "/var/task/lib/config.json",
    ]
    for path in paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {"symbols": ["SOLUSDT"], "testnet": False, "account_type": "UNIFIED"}


def _client(cfg: dict) -> BybitClient:
    return BybitClient(
        api_key=os.environ.get("BYBIT_API_KEY", cfg.get("api_key", "")),
        api_secret=os.environ.get("BYBIT_API_SECRET", cfg.get("api_secret", "")),
        testnet=bool(cfg.get("testnet", False)),
        account_type=cfg.get("account_type", "UNIFIED"),
        timeout=20,
    )


def _symbol(cfg: dict) -> str:
    symbols = cfg.get("symbols") or ["SOLUSDT"]
    return symbols[0]


def _safe_float(v, default=0.0):
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_list(series):
    return [_safe_float(x) for x in series]


def get_klines() -> tuple[int, dict]:
    cfg = _cfg()
    kl = _client(cfg).get_kline(symbol=_symbol(cfg), interval="60", limit=72)
    if not kl or (isinstance(kl, dict) and kl.get("retCode") not in (0, None)):
        return 500, {"error": f"获取K线失败: {(kl or {}).get('retMsg', 'no response')}"}

    df = enrich_indicators(klines_to_df(kl))
    if df is None or df.empty:
        return 500, {"error": "No data"}
    df = df.ffill().bfill()
    return 200, {
        "labels": df["startTime"].dt.strftime("%H:%M").tolist(),
        "prices": _safe_list(df["close"]),
        "sma24": _safe_list(df["SMA24"]),
        "sma72": _safe_list(df["SMA72"]),
        "bb_upper": _safe_list(df["BB_Upper"]),
        "bb_lower": _safe_list(df["BB_Lower"]),
        "rsi": _safe_list(df["RSI14"]),
        "macd": _safe_list(df["MACD"]),
        "macd_signal": _safe_list(df["MACD_Signal"]),
        "macd_hist": _safe_list(df["MACD_Hist"]),
        "volume": _safe_list(df["volume"]),
    }


def get_indicators() -> tuple[int, dict]:
    cfg = _cfg()
    kl = _client(cfg).get_kline(symbol=_symbol(cfg), interval="60", limit=100)
    if not kl or (isinstance(kl, dict) and kl.get("retCode") not in (0, None)):
        return 500, {
            "error": f"获取K线失败: {(kl or {}).get('retMsg', 'no response')}",
            "retCode": (kl or {}).get("retCode"),
        }

    df = enrich_indicators(klines_to_df(kl))
    if df is None or df.empty:
        return 500, {"error": "指标数据为空（K线无数据）"}

    last = df.iloc[-1]
    market = get_market_condition(df)
    data = {
        "rsi14": _safe_float(last.get("RSI14", 50), 50),
        "rsi7": _safe_float(last.get("RSI7", 50), 50),
        "trend": _safe_float(last.get("Trend", 0), 0),
        "bb_position": _safe_float(last.get("BB_Position", 0.5), 0.5),
        "macd_hist": _safe_float(last.get("MACD_Hist", 0), 0),
        "atr_pct": _safe_float(last.get("ATR_Pct", 0), 0),
        "volume_ratio": _safe_float(last.get("Volume_Ratio", 1), 1),
        "support": _safe_float(market.get("support", 0), 0),
        "resistance": _safe_float(market.get("resistance", 0), 0),
        "condition": market.get("condition", "unknown"),
    }

    try:
        import db_pg
        cached = db_pg.get_meta("_news_cache")
        news = json.loads(cached) if cached else {}
        data.update({
            "news_sentiment": _safe_float(news.get("score", 0), 0),
            "news_confidence": _safe_float(news.get("confidence", 0), 0),
            "news_risk_level": news.get("risk_level", "medium"),
            "news_action": news.get("suggested_action", "hold"),
            "news_summary": news.get("summary", ""),
            "news_key_factors": (news.get("key_factors") or [])[:3],
            "news_age_min": _safe_float(news.get("age_min", 999), 999),
        })
    except Exception:
        data.update({"news_sentiment": 0, "news_key_factors": [], "news_summary": ""})
    return 200, data


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path.endswith("/klines"):
                code, out = get_klines()
            elif path.endswith("/indicators"):
                code, out = get_indicators()
            else:
                code, out = 404, {"error": "not found"}
        except Exception as e:
            code, out = 500, {"error": str(e), "type": type(e).__name__}
        body = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
