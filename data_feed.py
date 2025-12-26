import time
import json

import pyotp
from SmartApi import SmartConnect


class SimulatedFeed:
    def __init__(self, start_price=100.0, volatility=0.5):
        self.price = start_price
        self.volatility = volatility

    def get_price(self, symbol: str):
        import random

        move = random.uniform(-self.volatility, self.volatility)
        self.price = max(1.0, self.price + move)
        return {"symbol": symbol, "price": self.price, "time": time.time()}


class SmartAPIConnector:
    def __init__(
        self,
        api_key: str,
        client_id: str,
        password: str,
        totp_secret: str,
        instruments: dict,
        notifier=None,
    ):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret
        self.instruments = instruments
        self.notifier = notifier
        self.smart = None
        self.last_login = 0
        self.login()

    def login(self):
        self.smart = SmartConnect(api_key=self.api_key)
        totp = pyotp.TOTP(self.totp_secret).now()
        data = self.smart.generateSession(self.client_id, self.password, totp)
        self.last_login = time.time()
        if self.notifier:
            self.notifier.send("SMARTAPI LOGIN ✅")
        print("SMARTAPI LOGIN OK", data.get("status"))

    def _ensure_logged_in(self):
        # relogin every 6 hours as a simple safety
        if time.time() - self.last_login > 6 * 60 * 60:
            self.login()

    def _normalize_resp(self, resp):
        if isinstance(resp, str):
            return json.loads(resp)
        return resp

    def _handle_invalid_token_and_retry(self, func, *args, **kwargs):
        try:
            resp = func(*args, **kwargs)
        except Exception as e:
            if "AG8001" in str(e) or "Invalid Token" in str(e):
                if self.notifier:
                    self.notifier.send("SMARTAPI: Invalid Token AG8001, re-logging in…")
                self.login()
                resp = func(*args, **kwargs)
            else:
                raise
        resp = self._normalize_resp(resp)
        if not resp.get("success", True):
            msg = resp.get("message", "")
            code = resp.get("errorCode", "")
            if "AG8001" in code or "Invalid Token" in msg:
                if self.notifier:
                    self.notifier.send(
                        "SMARTAPI: Invalid Token AG8001 (resp), re-logging in…"
                    )
                self.login()
                resp = func(*args, **kwargs)
                resp = self._normalize_resp(resp)
            if not resp.get("success", True):
                raise RuntimeError(f"SmartAPI error: {resp}")
        return resp

    def get_price(self, symbol: str):
        self._ensure_logged_in()
        inst = self.instruments.get(symbol)
        if inst is None:
            raise ValueError(f"No SmartAPI instrument config for symbol {symbol}")
        exchange = inst["exchange"]
        tradingsymbol = inst["tradingsymbol"]
        symboltoken = inst["symboltoken"]

        def _ltp():
            return self.smart.ltpData(exchange, tradingsymbol, symboltoken)

        resp = self._handle_invalid_token_and_retry(_ltp)
        ltp = float(resp["data"]["ltp"])
        return {"symbol": symbol, "price": ltp, "time": time.time()}

    def get_historical(
        self,
        exchange: str,
        symboltoken: str,
        interval: str,
        fromdate: str,
        todate: str,
    ):
        """
        Wrapper around SmartAPI getCandleData.
        interval example: 'FIVE_MINUTE', 'FIFTEEN_MINUTE', 'ONE_MINUTE', etc.
        fromdate/todate: 'YYYY-MM-DD HH:MM'
        """
        self._ensure_logged_in()

        def _hist():
            params = {
                "exchange": exchange,
                "symboltoken": symboltoken,
                "interval": interval,
                "fromdate": fromdate,
                "todate": todate,
            }
            return self.smart.getCandleData(params)

        resp = self._handle_invalid_token_and_retry(_hist)
        return resp
