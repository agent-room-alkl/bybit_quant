# -*- coding: utf-8 -*-
import os, time, json, hmac, hashlib, logging
from typing import Dict, Any, Optional, Tuple, List
import requests
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

API_MAIN = "https://api.bybit.com"
API_TEST = "https://api-testnet.bybit.com"

def _setup_logger():
    log = logging.getLogger("bybit_client")
    if log.handlers:
        return log
    level = os.getenv("BYBIT_LOG_LEVEL", "INFO").upper()
    log.setLevel(getattr(logging, level, logging.INFO))
    try:
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler("logs/app.log", encoding="utf-8")
        fh.setLevel(getattr(logging, level, logging.INFO))
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] bybit_client: %(message)s")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler()
    sh.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] bybit_client: %(message)s")
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log.propagate = False
    return log

log = _setup_logger()

def _q(value: float, step: float, mode: str) -> Decimal:
    d = Decimal(str(value))
    s = Decimal(str(step))
    if s == 0:
        return d
    units = d / s
    if mode == "ceil":
        units = units.to_integral_value(rounding=ROUND_CEILING)
    else:
        units = units.to_integral_value(rounding=ROUND_FLOOR)
    return (units * s).quantize(s)

class BybitClient:
    def __init__(self, api_key: Optional[str]=None, api_secret: Optional[str]=None, testnet: bool=True, recv_window_ms: int=5000, timeout: int=30, account_type: str="UNIFIED", max_retries: int=3) -> None:
        self.api_key = api_key or os.getenv("BYBIT_API_KEY","")
        self.api_secret = (api_secret or os.getenv("BYBIT_API_SECRET","")).encode("utf-8")
        self.recv_window_ms = str(int(recv_window_ms))
        self.timeout = timeout
        self.base = API_TEST if testnet else API_MAIN
        self.session = requests.Session()
        self.account_type = account_type
        self.max_retries = max(0, int(max_retries))
        self._instr_cache: Dict[str, Any] = {}  # symbol -> (result, timestamp) for get_instruments_info
        self._filter_cache: Dict[str, Dict[str, float]] = {}  # symbol -> {tick_size, qty_step, ...}

    def _ts_ms(self) -> str:
        return str(int(time.time()*1000))

    def _sign(self, payload: str) -> str:
        return hmac.new(self.api_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def _headers(self, sign: str, ts: str) -> Dict[str,str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self.recv_window_ms,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: Optional[Dict[str,Any]]=None, body: Optional[Dict[str,Any]]=None, private: bool=False) -> Dict[str,Any]:
        import urllib.parse
        url = self.base + path
        attempt = 0
        while True:
            attempt += 1
            try:
                if private:
                    ts = self._ts_ms()
                    if method.upper()=="GET":
                        items: List[Tuple[str,Any]] = [(k,(params or {})[k]) for k in sorted(params or {}) if (params or {})[k] is not None]
                        qs = urllib.parse.urlencode(items, doseq=True)
                        payload = f"{ts}{self.api_key}{self.recv_window_ms}{qs}"
                        sign = self._sign(payload)
                        headers = self._headers(sign, ts)
                        resp = self.session.get(url, params=items, headers=headers, timeout=self.timeout)
                    else:
                        body_str = json.dumps(body or {}, separators=(",",":"), ensure_ascii=False)
                        payload = f"{ts}{self.api_key}{self.recv_window_ms}{body_str}"
                        sign = self._sign(payload)
                        headers = self._headers(sign, ts)
                        resp = self.session.post(url, data=body_str.encode("utf-8"), headers=headers, timeout=self.timeout)
                else:
                    if method.upper()=="GET":
                        resp = self.session.get(url, params=params or {}, timeout=self.timeout)
                    else:
                        resp = self.session.post(url, data=json.dumps(body or {}), timeout=self.timeout)

                try:
                    data = resp.json()
                except Exception:
                    text = (resp.text or "")[:500]
                    log.error(f"Non-JSON response {resp.status_code} {url}: {text}")
                    data = {"retCode": -10001, "retMsg": f"Non-JSON response (status {resp.status_code})", "text": text}

                if resp.status_code>=500 or data.get("retCode") in {10006,10007,10016}:
                    if attempt<=self.max_retries:
                        wait_time = min(1.0 * attempt, 3.0)
                        log.warning(f"服务器错误 {resp.status_code} 或业务错误 {data.get('retCode')} (尝试 {attempt}/{self.max_retries + 1}): {url}, 等待 {wait_time:.1f} 秒后重试...")
                        time.sleep(wait_time)
                        continue
                    log.error(f"服务器错误，已达到最大重试次数: {url}")
                return data
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as e:
                log.warning(f"连接超时 (尝试 {attempt}/{self.max_retries + 1}): {method} {url} - {type(e).__name__}: {e}")
                if attempt <= self.max_retries:
                    wait_time = min(2.0 * attempt, 10.0)  # 最多等待10秒
                    log.info(f"等待 {wait_time:.1f} 秒后重试...")
                    time.sleep(wait_time)
                    continue
                log.error(f"连接超时，已达到最大重试次数: {method} {url}")
                return {"retCode": -10000, "retMsg": f"连接超时: {type(e).__name__}: {str(e)}"}
            except requests.RequestException as e:
                log.warning(f"HTTP错误 (尝试 {attempt}/{self.max_retries + 1}): {method} {url} - {type(e).__name__}: {e}")
                if attempt <= self.max_retries:
                    wait_time = min(1.5 * attempt, 5.0)  # 最多等待5秒
                    time.sleep(wait_time)
                    continue
                log.error(f"HTTP错误，已达到最大重试次数: {method} {url}")
                return {"retCode": -10000, "retMsg": f"HTTP错误: {type(e).__name__}: {str(e)}"}

    # ---------- public ----------
    def _public_get(self, path: str, params: Dict[str,Any]) -> Dict[str,Any]:
        return self._request("GET", path, params=params, private=False)

    # ---------- private ----------
    def _private_get(self, path: str, params: Dict[str,Any]) -> Dict[str,Any]:
        return self._request("GET", path, params=params, private=True)

    def _private_post(self, path: str, body: Dict[str,Any]) -> Dict[str,Any]:
        return self._request("POST", path, body=body, private=True)

    # ---------- market ----------
    def get_kline(self, symbol: str, interval: str="60", start: Optional[int]=None, end: Optional[int]=None, limit: int=500) -> Dict[str,Any]:
        params = {"category":"spot","symbol":symbol,"interval":interval,"limit":limit}
        if start is not None: params["start"]=start
        if end   is not None: params["end"]=end
        return self._public_get("/v5/market/kline", params)

    def get_ticker(self, symbol: str) -> Dict[str,Any]:
        return self._public_get("/v5/market/tickers", {"category":"spot","symbol":symbol})

    def get_orderbook(self, symbol: str, limit: int=50) -> Dict[str,Any]:
        return self._public_get("/v5/market/orderbook", {"category":"spot","symbol":symbol,"limit":limit})

    def get_instruments_info(self, symbol: Optional[str]=None) -> Dict[str,Any]:
        # v4.4: 缓存instruments_info, 交易对参数几乎不变, 缓存1小时
        cache_key = symbol or "__all__"
        now = time.time()
        if cache_key in self._instr_cache:
            cached_data, cached_time = self._instr_cache[cache_key]
            if now - cached_time < 3600:  # 1小时缓存
                return cached_data
        params = {"category":"spot"}
        if symbol: params["symbol"]=symbol
        result = self._public_get("/v5/market/instruments-info", params)
        if result.get("retCode") == 0:
            self._instr_cache[cache_key] = (result, now)
        return result

    # ---------- account / trade ----------
    def get_wallet_balance(self, coins: Optional[str]=None) -> Dict[str,Any]:
        params = {"accountType": self.account_type}
        if coins: params["coin"]=coins
        return self._private_get("/v5/account/wallet-balance", params)
    
    def get_spot_cost_price(self, coin: str) -> Optional[float]:
        """
        尝试从钱包余额API获取现货成本价
        注意：根据测试，Bybit API不直接提供成本价，此方法返回None
        成本价需要通过交易历史计算（见cost.py中的get_spot_avg_cost函数）
        """
        # 根据测试结果，Bybit API响应中不包含成本价字段
        # 可能的字段位置都已检查，但未找到相关字段
        # 因此直接返回None，让调用方使用交易历史计算方法
        return None

    def get_trade_history(self, symbol: Optional[str]=None, start_ms: Optional[int]=None, end_ms: Optional[int]=None, limit: int=50, cursor: Optional[str]=None) -> Dict[str,Any]:
        params = {"category":"spot","symbol":symbol,"startTime":start_ms,"endTime":end_ms,"limit":limit}
        if cursor: params["cursor"]=cursor
        return self._private_get("/v5/execution/list", params)
    
    def get_open_orders(self, symbol: Optional[str]=None, limit: int=20) -> Dict[str,Any]:
        """获取当前挂单（未成交订单）"""
        params = {"category": "spot", "limit": limit}
        if symbol: params["symbol"] = symbol
        return self._private_get("/v5/order/realtime", params)

    def get_account_info(self) -> Dict[str,Any]:
        """获取账户信息，包括杠杆交易状态"""
        return self._private_get("/v5/account/info", {})
    
    def get_spot_leverage_status(self) -> Dict[str,Any]:
        """获取现货杠杆交易状态（通过尝试查询杠杆账户余额）"""
        # 尝试获取杠杆账户余额，如果返回错误说明未开通杠杆
        try:
            # 查询现货杠杆账户信息
            params = {"category": "spot"}
            return self._private_get("/v5/account/wallet-balance", params)
        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}

    def place_order(self, symbol: str, side: str, order_type: str, qty: str, price: Optional[str]=None, tif: str="IOC", order_link_id: Optional[str]=None, isLeverage: int=0) -> Dict[str,Any]:
        # --- Hard guard: quantize price/qty using instrument filters ---
        filt = self._get_instr_filters_cached(symbol)
        step = filt["qty_step"]
        tick = filt["tick_size"]
        
        # 如果qty和price已经是字符串格式（来自trade_logic），直接使用
        # 否则进行量化处理
        def _decimals(step_val):
            """从step/tick推算需要的小数位数"""
            s = f"{step_val:.10f}".rstrip('0')
            return len(s.split('.')[-1]) if '.' in s else 0

        try:
            qty_float = float(qty)
            q = _q(qty_float, step, "floor")
            dec = _decimals(step)
            qty = f"{float(q):.{dec}f}"
        except Exception as e:
            log.warning(f"量化数量失败: {e}, 使用原始值 {qty}")

        if price is not None:
            try:
                price_float = float(price)
                mode = "ceil" if side=="Buy" else "floor"
                p = _q(price_float, tick, mode)
                dec = _decimals(tick)
                price = f"{float(p):.{dec}f}"
            except Exception as e:
                log.warning(f"量化价格失败: {e}, 使用原始值 {price}")
        body = {"category":"spot","symbol":symbol,"side":side,"orderType":order_type,"qty":str(qty),"timeInForce":tif,"isLeverage":isLeverage}
        if price is not None: body["price"]=str(price)
        if order_link_id: body["orderLinkId"]=order_link_id
        log.info(f"place_order payload (quantized): {body}")
        return self._private_post("/v5/order/create", body)

    # ---------- funding / balances ----------
    def get_all_coins_balance(self, account_type: str, coins: str=None) -> Dict[str,Any]:
        params = {"accountType":account_type}
        if coins: params["coin"]=coins
        return self._private_get("/v5/asset/transfer/query-account-coins-balance", params)

    def get_single_coin_balance(self, account_type: str, coin: str) -> Dict[str,Any]:
        return self._private_get("/v5/asset/transfer/query-account-coin-balance", {"accountType":account_type,"coin":coin})

    def get_unified_transferable(self, coin_names: str) -> Dict[str,Any]:
        return self._private_get("/v5/account/withdrawal", {"coinName": coin_names})

    # ---------- helpers ----------
    @staticmethod
    def extract_best_prices(orderbook_json: Dict[str,Any]) -> Tuple[Optional[float], Optional[float]]:
        try:
            res = (orderbook_json.get("result") or {})
            bids = res.get("b") or []
            asks = res.get("a") or []
            bid1 = float(bids[0][0]) if bids else None
            ask1 = float(asks[0][0]) if asks else None
            return bid1, ask1
        except Exception:
            return None, None

    def _get_instr_filters_cached(self, symbol: str) -> Dict[str,float]:
        s = symbol.upper()
        if s in self._filter_cache:
            return self._filter_cache[s]
        info = self.get_instruments_info(s)
        try:
            row = (info.get("result") or {}).get("list")[0]
            price_filter = row.get("priceFilter", {}) or {}
            lot_filter = row.get("lotSizeFilter", {}) or {}
            tick = float(price_filter.get("tickSize", "0.00000001"))
            step = float(lot_filter.get("qtyStep", "0.00000001"))
            min_qty = float(lot_filter.get("minOrderQty", "0"))
            min_notional = float(lot_filter.get("minNotionalValue", "0"))
            self._filter_cache[s] = {"tick_size": tick, "qty_step": step, "min_qty": min_qty, "min_notional": min_notional}
        except Exception:
            self._filter_cache[s] = {"tick_size": 0.00000001, "qty_step": 0.00000001, "min_qty": 0.0, "min_notional": 0.0}
        return self._filter_cache[s]

    @staticmethod
    def ok(resp: Dict[str,Any]) -> bool:
        return resp and resp.get("retCode")==0
