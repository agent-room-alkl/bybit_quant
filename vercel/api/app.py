# -*- coding: utf-8 -*-
"""
Vercel 入口：整体复用本地 main.py 的 FastAPI app（webapp.py），
得到与本地**逐像素一致**的看板 + 全部接口。serverless 适配在此完成，webapp.py 不改逻辑。

适配处理（采纳 Codex review 的5个坑）：
1. lib 入 sys.path
2. `sys.modules['db']=db_pg` 必须在 import webapp 之前 → 让 main 的 `from db import` 走 PG
3. stub uvicorn（main 顶部 import 它，但只在 __main__ 用，不需真装）
4. chdir 到可写工作目录(/tmp/app) 并预先写好 config.json + logs/ → 解决 makedirs/RotatingFileHandler/load_config
5. 密钥从环境变量注入，config.json 不含明文
"""
import os
import sys
import json
import types
import tempfile

# ── 1. lib 路径 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = None
for _p in [os.path.join(_HERE, "..", "lib"), os.path.join(os.getcwd(), "lib"), "/var/task/lib"]:
    _p = os.path.abspath(_p)
    if os.path.isdir(_p):
        _LIB = _p
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

# ── 2. db -> db_pg 别名（务必在 import webapp 之前）──
import db_pg
sys.modules["db"] = db_pg

# ── 3. stub uvicorn（webapp 顶部会 import，但只在本地 __main__ 用）──
if "uvicorn" not in sys.modules:
    _uv = types.ModuleType("uvicorn")
    _uv.run = lambda *a, **k: None
    sys.modules["uvicorn"] = _uv

# ── 4. 可写工作目录：写 config.json + logs/，再 chdir 过去 ──
_WORK = os.path.join(tempfile.gettempdir(), "bybit_app")
os.makedirs(os.path.join(_WORK, "logs"), exist_ok=True)
os.makedirs(os.path.join(_WORK, "data"), exist_ok=True)

def _materialize_config():
    cfg = {}
    src = os.path.join(_LIB, "config.json") if _LIB else None
    if src and os.path.isfile(src):
        try:
            with open(src, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    # 环境变量注入密钥/开关（生产不放明文）
    cfg["api_key"] = os.environ.get("BYBIT_API_KEY", cfg.get("api_key", ""))
    cfg["api_secret"] = os.environ.get("BYBIT_API_SECRET", cfg.get("api_secret", ""))
    cfg["gpt_api_key"] = os.environ.get("GPT_API_KEY", cfg.get("gpt_api_key", ""))
    if os.environ.get("ENABLE_TRADING") is not None:
        cfg["enable_trading"] = os.environ.get("ENABLE_TRADING", "false").lower() == "true"
    else:
        cfg.setdefault("enable_trading", False)
    with open(os.path.join(_WORK, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

_materialize_config()
os.chdir(_WORK)  # 让 webapp 的 load_config("config.json") 和 logs/ 都落到可写目录

# ── 5. 复用本地 FastAPI app（webapp.py = main.py 原样拷贝）──
from webapp import app  # noqa: E402  ASGI app, Vercel @vercel/python 直接服务它
