import random
import time

from SmartApi import SmartConnect  # requires smartapi-python


class SimulatedFeed:
    """Generate a simple random-walk price series for a symbol.

    Call `get_price(symbol)` to obtain the latest price (float) and timestamp.
    """

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
        # random walk step
        step = random.normalvariate(0, self.volatility)
        p = max(0.01, p * (1 + step / 100.0))
        self.state[symbol]["price"] = p
        return {"symbol": symbol, "price": p, "time": time.time()}


class SmartAPIConnector:
    """Angel One SmartAPI connector for live quotes only (no orders).

    This class is ONLY for fetching prices (LTP). All trades are handled by
    PaperTrader, so no real orders are placed.
    """

    def __init__(
        self,
        api_key=None,
        client_id=None,
        password=None,
        totp=None,
        exchange=None,
        tradingsymbol=None,
        symboltoken=None,
    ):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp = totp
        self.exchange = exchange
        self.tradingsymbol = tradingsymbol
        self.symboltoken = symboltoken

        self.connected = False
        self.smart = None
        self._login()

    def _login(self):
        """Create SmartConnect session for market data."""
        self.smart = SmartConnect(api_key=self.api_key)
        # Adjust according to the current SmartAPI auth flow.
        # Common pattern: generateSession(client_id, password, totp)
        self.smart.generateSession(self.client_id, self.password, self.totp)
        self.connected = True

    def get_price(self, symbol):
        """Return dict: {'symbol': symbol, 'price': float, 'time': timestamp}."""
        if not self.connected:
            self._login()

        # For now ignore the incoming symbol and use configured instrument.
        resp = self.smart.ltpData(
            self.exchange,
            self.tradingsymbol,
            self.symboltoken,
        )
        # LTP is returned here as per SmartAPI docs.
        ltp = float(resp["data"]["ltp"])
        return {"symbol": symbol, "price": ltp, "time": time.time()}
