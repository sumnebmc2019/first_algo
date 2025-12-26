class PaperTrader:
    def __init__(self, starting_cash=100000.0, slippage=0.0):
        self.cash = starting_cash
        self.slippage = slippage
        self.positions = {}   # symbol -> qty
        self.avg_price = {}   # symbol -> avg entry price
        self.trade_log = []   # list of dicts

    def _apply_slippage(self, price, side):
        return price

    def buy_market(self, symbol, qty, price):
        price = self._apply_slippage(price, "buy")
        cost = qty * price
        if self.cash < cost:
            return False, price
        self.cash -= cost
        prev_qty = self.positions.get(symbol, 0)
        prev_avg = self.avg_price.get(symbol, 0.0)
        new_qty = prev_qty + qty
        if new_qty != 0:
            self.avg_price[symbol] = (prev_qty * prev_avg + qty * price) / new_qty
        self.positions[symbol] = new_qty
        self.trade_log.append(
            {"symbol": symbol, "side": "BUY", "qty": qty, "price": price}
        )
        return True, price

    def sell_market(self, symbol, qty, price):
        price = self._apply_slippage(price, "sell")
        qty = abs(qty)
        revenue = qty * price
        self.cash += revenue
        prev_qty = self.positions.get(symbol, 0)
        prev_avg = self.avg_price.get(symbol, 0.0)
        new_qty = prev_qty - qty
        self.positions[symbol] = new_qty
        if new_qty == 0:
            self.avg_price[symbol] = 0.0
        else:
            # for simplicity, keep avg price same when reducing position
            self.avg_price[symbol] = prev_avg
        self.trade_log.append(
            {"symbol": symbol, "side": "SELL", "qty": qty, "price": price}
        )
        return True, price

    def pnl(self, market_prices: dict):
        unrealized = 0.0
        for symbol, qty in self.positions.items():
            if qty == 0:
                continue
            last = market_prices.get(symbol)
            if last is None:
                continue
            avg = self.avg_price.get(symbol, last)
            if qty > 0:
                unrealized += (last - avg) * qty
            else:
                unrealized += (avg - last) * abs(qty)
        total = self.cash + unrealized
        return {"cash": self.cash, "unrealized": unrealized, "total": total}

    def realized_trade_pnl(self, side, symbol, qty, entry_price, exit_price):
        if side == "long":
            return (exit_price - entry_price) * qty
        else:
            return (entry_price - exit_price) * qty
