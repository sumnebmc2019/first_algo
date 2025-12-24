import random
import time

from SmartApi import SmartConnect  # requires smartapi-python
import requests
import pyotp


class SimulatedFeed:
    """Generate a simple random-walk price series for a symbol."""

    def __init__(self, start_price=100.0, volatility=0.2):
        self.state = {}
        self.start_price = float(start_price)
        self.volatility = float(volatility)

    def _ensure(self, symbol):
        if symbol not in self.state:
            self.state[symbol] = {"price": self.start_price}

    def get_price(self, symbol):
        self._ensure(symbol)
        p = self.state[symbol]["price"]
        step = random.normalvariate(0, self.volatility)
        p = max(0.01, p * (1 + step / 100.0))
        self.state[symbol]["price"] = p
        return {"symbol": symbol, "price": p, "time": time.time()}


class SmartAPIConnector:
    """Angel One SmartAPI connector for live quotes only (no orders)."""

    def __init__(
        self,
        api_key=None,
        client_id=None,
        password=None,
        totp_secret=None,
        instruments=None,   # dict: symbol -> {exchange, tradingsymbol, symboltoken}
        notifier=None,
    ):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret
        self.instruments = instruments or {}
        self.notifier = notifier

        self.connected = False
        self.smart = None
        self._login(first=True)

    def _send_notify(self, text):
        if self.notifier:
            self.notifier.send(text)

    def _login(self, first=False):
        self.smart = SmartConnect(api_key=self.api_key)
        if not self.totp_secret:
            raise RuntimeError("TOTP secret not configured in config.yaml")

        totp = pyotp.TOTP(self.totp_secret).now()  # auto-generate TOTP[web:214][web:218]
        data = self.smart.generateSession(self.client_id, self.password, totp)
        self.connected = True
        tag = "INITIAL LOGIN ✅" if first else "RE-LOGIN ✅"
        self._send_notify(f"SMARTAPI {tag}\nStatus: {data.get('status', True)}")

    def _ensure_login(self):
        if not self.connected:
            self._login()

    def get_price(self, symbol):
        """Return dict: {'symbol': symbol, 'price': float, 'time': timestamp}."""
        self._ensure_login()

        inst = self.instruments.get(symbol)
        if inst is None:
            raise ValueError(f"No SmartAPI instrument config for symbol {symbol}")

        exchange = inst["exchange"]
        tradingsymbol = inst["tradingsymbol"]
        symboltoken = inst["symboltoken"]

        resp = None
        for attempt in range(3):
            try:
                resp = self.smart.ltpData(
                    exchange,
                    tradingsymbol,
                    symboltoken,
                )
                break
            except requests.exceptions.ReadTimeout as e:
                if attempt == 2:
                    raise RuntimeError(f"LTP ReadTimeout for {symbol}: {e}")
                time.sleep(1)
            except Exception as e:
                msg = str(e)
                # if token/auth issue, re-login once and retry
                if any(k in msg.lower() for k in ["token", "jwt", "unauthorized", "session"]):
                    self.connected = False
                    self._login()
                    continue
                if attempt == 2:
                    raise

        if resp is None or "data" not in resp or resp["data"] is None:
            raise RuntimeError(f"LTP response invalid for {symbol}: {resp}")

        ltp = float(resp["data"]["ltp"])
        return {"symbol": symbol, "price": ltp, "time": time.time()}
