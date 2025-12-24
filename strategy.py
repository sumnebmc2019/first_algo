import time


class FiveEMA:
    """
    Power-of-Stocks style 5 EMA strategy, long + short, with:
    - Short side on 5-minute candles
    - Long side on 15-minute candles
    - Signal candle: full candle away from EMA (no wick touch)
    - Entry: next candle close breaks signal high/low
    - Signal rolls forward if not broken and EMA not touched
    - Signal cancels on EMA touch
    - SL = signal candle high (short) / low (long)
    - TP = entry +/- rr * risk
    """

    def __init__(self, ema_period=5, rr=3.0, max_trades_per_day=5):
        self.ema_period = ema_period
        self.alpha = 2 / (ema_period + 1)

        self.ema_short = None  # for 5m (short)
        self.ema_long = None   # for 15m (long)

        self.signal_short = None  # {"high": h, "low": l}
        self.signal_long = None   # {"high": h, "low": l}

        # open position dict: {"side": "long"/"short", "entry": e, "sl": sl, "tp": tp}
        self.position = None

        self.rr = rr
        self.max_trades_per_day = max_trades_per_day
        self.trades_today = 0
        self.current_day = None

    def _reset_day_if_needed(self, ts):
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if self.current_day != day:
            self.current_day = day
            self.trades_today = 0

    def can_trade(self, ts):
        self._reset_day_if_needed(ts)
        return self.trades_today < self.max_trades_per_day

    def _record_trade(self):
        self.trades_today += 1

    def update_candle(self, o, h, l, c, ts, tf_minutes):
        """
        Feed one completed candle (OHLC) for a given timeframe.

        Args:
            o, h, l, c: float
            ts: candle close timestamp (epoch seconds)
            tf_minutes: timeframe of this candle in minutes (5 or 15)

        Returns:
            None, or dict:
              {"signal": "long_entry"/"short_entry",
               "entry": ..., "sl": ..., "tp": ...}
              {"signal": "exit_sl"/"exit_tp", "exit_price": ...}
        """
        self._reset_day_if_needed(ts)

        # Update EMA for corresponding timeframe
        if tf_minutes == 5:
            if self.ema_short is None:
                self.ema_short = c
            else:
                self.ema_short = self.alpha * c + (1 - self.alpha) * self.ema_short
        elif tf_minutes == 15:
            if self.ema_long is None:
                self.ema_long = c
            else:
                self.ema_long = self.alpha * c + (1 - self.alpha) * self.ema_long
        else:
            return None

        # If in position, exits have priority
        if self.position is not None:
            if self.position["side"] == "short":
                if h >= self.position["sl"]:
                    out = {"signal": "exit_sl", "exit_price": self.position["sl"]}
                    self.position = None
                    return out
                if l <= self.position["tp"]:
                    out = {"signal": "exit_tp", "exit_price": self.position["tp"]}
                    self.position = None
                    return out

            elif self.position["side"] == "long":
                if l <= self.position["sl"]:
                    out = {"signal": "exit_sl", "exit_price": self.position["sl"]}
                    self.position = None
                    return out
                if h >= self.position["tp"]:
                    out = {"signal": "exit_tp", "exit_price": self.position["tp"]}
                    self.position = None
                    return out

            return None

        # If flat but daily limit reached, ignore new entries
        if not self.can_trade(ts):
            return None

        # SHORT SIDE (5m)
        if tf_minutes == 5 and self.ema_short is not None:
            if self.signal_short is None:
                if h > self.ema_short and l > self.ema_short:
                    self.signal_short = {"high": h, "low": l}
                return None

            if c < self.signal_short["low"]:
                entry = c
                sl = self.signal_short["high"]
                risk = sl - entry
                if risk <= 0:
                    self.signal_short = None
                    return None
                tp = entry - self.rr * risk
                self.position = {
                    "side": "short",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                }
                self.signal_short = None
                self._record_trade()
                return {
                    "signal": "short_entry",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                }

            if l > self.ema_short and c >= self.signal_short["low"]:
                self.signal_short = {"high": h, "low": l}
                return None

            if l <= self.ema_short:
                self.signal_short = None
            return None

        # LONG SIDE (15m)
        if tf_minutes == 15 and self.ema_long is not None:
            if self.signal_long is None:
                if l < self.ema_long and h < self.ema_long:
                    self.signal_long = {"high": h, "low": l}
                return None

            if c > self.signal_long["high"]:
                entry = c
                sl = self.signal_long["low"]
                risk = entry - sl
                if risk <= 0:
                    self.signal_long = None
                    return None
                tp = entry + self.rr * risk
                self.position = {
                    "side": "long",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                }
                self.signal_long = None
                self._record_trade()
                return {
                    "signal": "long_entry",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                }

            if h < self.ema_long and c <= self.signal_long["high"]:
                self.signal_long = {"high": h, "low": l}
                return None

            if h >= self.ema_long:
                self.signal_long = None
            return None

        return None
