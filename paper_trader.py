import time


class PaperTrader:
    """Simple in-memory paper trading engine.

    - Tracks cash, positions, and average prices.
    - buy_market / sell_market simulate executions with optional slippage.
    - No real orders are sent anywhere.
    """

    def __init__(self, starting_cash=100000, slippage=0.0):
        self.cash = float(starting_cash)
        self.slippage = float(slippage)
        self.positions = {}  # symbol -> qty
        self.avg_price = {}  # symbol -> average entry price
        self.trade_log = []

    def _record_trade(self, side, symbol, qty, price, tstamp=None):
        tstamp = tstamp or time.time()
        entry = {
            "time": tstamp,
            "side": side,
            "symbol": symbol,
            "qty": qty,
            "price": price,
        }
        self.trade_log.append(entry)

    def buy_market(self, symbol, qty, price):
        """Simulate a market BUY."""
        executed_price = price * (1 + self.slippage)
        cost = executed_price * qty
        if cost > self.cash:
            return False, "insufficient_cash"
        self.cash -= cost

        prev_qty = self.positions.get(symbol, 0)
        prev_avg = self.avg_price.get(symbol, 0)
        new_qty = prev_qty + qty
        new_avg = (
            ((prev_avg * prev_qty) + executed_price * qty) / new_qty
            if new_qty
            else 0
        )
        self.positions[symbol] = new_qty
        self.avg_price[symbol] = new_avg
        self._record_trade("buy", symbol, qty, executed_price)
        return True, executed_price

    def sell_market(self, symbol, qty, price):
        """Simulate a market SELL."""
        executed_price = price * (1 - self.slippage)
        prev_qty = self.positions.get(symbol, 0)
        if qty > prev_qty:
            return False, "insufficient_position"
        proceeds = executed_price * qty
        self.cash += proceeds

        new_qty = prev_qty - qty
        if new_qty == 0:
            self.positions.pop(symbol, None)
            self.avg_price.pop(symbol, None)
        else:
            self.positions[symbol] = new_qty

        self._record_trade("sell", symbol, qty, executed_price)
        return True, executed_price

    def pnl(self, market_prices):
        """Compute unrealized + realized PnL given current market prices dict symbol->price."""
        unreal = 0.0
        for sym, qty in self.positions.items():
            market = market_prices.get(sym)
            if market is not None:
                unreal += (market - self.avg_price.get(sym, 0)) * qty
        return {
            "cash": self.cash,
            "unrealized": unreal,
            "total": self.cash + unreal,
        }
