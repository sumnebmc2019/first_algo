class FiveEMA:
    """
    Simplified Power-of-Stocks style 5 EMA short-only strategy.

    - Works on NIFTY 5-minute candles.
    - Uses EMA(5) on close.
    - "Signal candle": close > EMA and low > EMA (candle entirely above EMA).
    - When a later candle closes below the signal candle low -> short entry.
    - SL = signal candle high.
    - TP = entry - rr * (signal_high - entry).

    Usage:
        result = five_ema.update_candle(o, h, l, c)
        if result is not None:
            result["signal"] in {"short_entry", "exit_sl", "exit_tp"}.

    This is a simplified version for education/paper testing only.
    """

    def __init__(self, ema_period=5, rr=1.5):
        self.ema_period = ema_period
        self.alpha = 2 / (ema_period + 1)
        self.ema = None

        self.signal_candle = None  # {"high": h, "low": l}
        self.position = None       # {"entry": e, "sl": sl, "tp": tp}
        self.rr = rr

    def update_candle(self, o, h, l, c):
        """
        Feed one completed candle (OHLC).

        Returns:
            None, or dict with:
                {"signal": "short_entry", "entry": ..., "sl": ..., "tp": ...}
                {"signal": "exit_sl", "exit_price": ...}
                {"signal": "exit_tp", "exit_price": ...}
        """
        # Update EMA on close
        if self.ema is None:
            self.ema = c
        else:
            self.ema = self.alpha * c + (1 - self.alpha) * self.ema

        # If in position, check for exits first
        if self.position is not None:
            # SL hit if high >= SL
            if h >= self.position["sl"]:
                out = {"signal": "exit_sl", "exit_price": self.position["sl"]}
                self.position = None
                return out

            # TP hit if low <= TP
            if l <= self.position["tp"]:
                out = {"signal": "exit_tp", "exit_price": self.position["tp"]}
                self.position = None
                return out

            # No exit this candle
            return None

        # If flat, look for signal candle or entry
        # 1) Identify a signal candle: above EMA, low > EMA (no touch)
        if self.signal_candle is None:
            if c > self.ema and l > self.ema:
                self.signal_candle = {"high": h, "low": l}
            return None

        # 2) Entry: candle close below signal low -> short
        if c < self.signal_candle["low"]:
            entry = c
            risk = self.signal_candle["high"] - entry
            sl = self.signal_candle["high"]
            tp = entry - self.rr * risk
            self.position = {"entry": entry, "sl": sl, "tp": tp}
            self.signal_candle = None
            return {"signal": "short_entry", "entry": entry, "sl": sl, "tp": tp}

        # 3) Invalidate signal if EMA touched again (low <= EMA)
        if l <= self.ema:
            self.signal_candle = None

        return None
