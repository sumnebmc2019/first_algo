class PaperTrader:
    def __init__(self, starting_cash: float = 100000.0, slippage: float = 0.0):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.slippage = slippage
        # positions: symbol -> qty (long > 0, short < 0)
        self.positions: dict[str, int] = {}
        # avg_price: symbol -> avg entry price of current open position
        self.avg_price: dict[str, float] = {}
        # realized P&L per symbol (only closed trades)
        self.realized_pnl: dict[str, float] = {}
        # trade log if you need it
        self.trade_log: list[dict] = []

    def _apply_slippage(self, price: float, side: str) -> float:
        if self.slippage <= 0:
            return price
        if side == "buy":
            return price * (1 + self.slippage)
        else:
            return price * (1 - self.slippage)

    def buy_market(self, symbol: str, qty: int, price: float):
        qty = int(qty)
        if qty <= 0:
            return False, price
        trade_price = self._apply_slippage(price, "buy")
        cost = qty * trade_price
        if self.cash < cost:
            return False, trade_price

        prev_qty = self.positions.get(symbol, 0)
        prev_avg = self.avg_price.get(symbol, trade_price)

        # if existing short, this may close/flip
        if prev_qty >= 0:
            new_qty = prev_qty + qty
            new_avg = (
                (prev_qty * prev_avg + qty * trade_price) / new_qty
                if new_qty != 0
                else trade_price
            )
            self.positions[symbol] = new_qty
            self.avg_price[symbol] = new_avg
        else:
            # closing or flipping short
            if qty <= abs(prev_qty):
                self.positions[symbol] = prev_qty + qty
                if self.positions[symbol] == 0:
                    self.avg_price.pop(symbol, None)
            else:
                remaining = qty + prev_qty
                self.positions[symbol] = remaining
                self.avg_price[symbol] = trade_price

        self.cash -= cost
        self.trade_log.append(
            {"symbol": symbol, "side": "BUY", "qty": qty, "price": trade_price}
        )
        return True, trade_price

    def sell_market(self, symbol: str, qty: int, price: float):
        qty = int(abs(qty))
        if qty <= 0:
            return False, price
        trade_price = self._apply_slippage(price, "sell")
        revenue = qty * trade_price

        prev_qty = self.positions.get(symbol, 0)
        prev_avg = self.avg_price.get(symbol, trade_price)

        if prev_qty <= 0:
            new_qty = prev_qty - qty
            new_avg = (
                (abs(prev_qty) * prev_avg + qty * trade_price) / abs(new_qty)
                if new_qty != 0
                else trade_price
            )
            self.positions[symbol] = new_qty
            self.avg_price[symbol] = new_avg
        else:
            if qty <= prev_qty:
                self.positions[symbol] = prev_qty - qty
                if self.positions[symbol] == 0:
                    self.avg_price.pop(symbol, None)
            else:
                remaining = prev_qty - qty
                self.positions[symbol] = remaining
                self.avg_price[symbol] = trade_price

        self.cash += revenue
        self.trade_log.append(
            {"symbol": symbol, "side": "SELL", "qty": qty, "price": trade_price}
        )
        return True, trade_price

    def record_realized_trade_pnl(
        self,
        symbol: str,
        side: str,  # 'long' or 'short'
        qty: int,
        entry_price: float,
        exit_price: float,
    ) -> float:
        if side == "long":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty
        self.realized_pnl[symbol] = self.realized_pnl.get(symbol, 0.0) + pnl
        return pnl

    def mark_to_market(self, market_prices: dict[str, float]) -> float:
        total = self.cash
        for symbol, qty in self.positions.items():
            price = market_prices.get(symbol)
            if price is None or qty == 0:
                continue
            avg = self.avg_price.get(symbol, price)
            if qty > 0:
                total += (price - avg) * qty
            else:
                total += (avg - price) * abs(qty)
        return total

    def equity(self, market_prices: dict[str, float]) -> float:
        return self.mark_to_market(market_prices)
