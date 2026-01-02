import time
from collections import defaultdict


class FiveEMA:
    """
    Power-of-Stocks style 5 EMA strategy, long + short, with:
    - Short side on 5-minute candles
    - Long side on 15-minute candles
    - Only one open position per symbol at a time.
    - Exit via SL/TP checked externally using exit_signal().
    """

    def __init__(self, ema_period=5, rr=3.0, max_trades_per_day=5):
        self.ema_period = ema_period
        self.alpha = 2 / (ema_period + 1)
        self.rr = rr
        self.max_trades_per_day = max_trades_per_day

        # Per-symbol state so multiple symbols can share one object
        self.state = defaultdict(self._new_symbol_state)

    def _new_symbol_state(self):
        return {
            "ema_short": None,
            "ema_long": None,
            "signal_short": None,
            "signal_long": None,
            # strategy owns position; backtest/bot reads it and calls force_flat on exit
            "position": None,  # {"side": "long"/"short", "entry": e, "sl": sl, "tp": tp, "trade_id": int}
            "trades_today": 0,
            "current_day": None,
            "next_trade_id": 1,
        }

    def _reset_day_if_needed(self, st, ts):
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if st["current_day"] != day:
            st["current_day"] = day
            st["trades_today"] = 0

    def _can_trade(self, st, ts):
        self._reset_day_if_needed(st, ts)
        return st["trades_today"] < self.max_trades_per_day

    def _record_trade(self, st):
        st["trades_today"] += 1
        trade_id = st["next_trade_id"]
        st["next_trade_id"] += 1
        return trade_id

    def force_flat(self, symbol):
        """
        Utility for external code to force strategy state flat,
        e.g. after an exit has been executed by the backtest/bot.
        """
        st = self.state[symbol]
        st["position"] = None
        st["signal_short"] = None
        st["signal_long"] = None

    def update_candle(self, symbol, o, h, l, c, ts, tf_minutes):
        """
        Feed one completed candle (OHLC) for a given symbol and timeframe.

        Returns:
            None, or dict:
              {
                "symbol": symbol,
                "signal": "long_entry"/"short_entry",
                "entry": float,
                "sl": float,
                "tp": float,
                "trade_id": int
              }
        Exit SL/TP is handled via exit_signal() separately.
        """
        st = self.state[symbol]
        self._reset_day_if_needed(st, ts)

        # Update EMA
        if tf_minutes == 5:
            if st["ema_short"] is None:
                st["ema_short"] = c
            else:
                st["ema_short"] = self.alpha * c + (1 - self.alpha) * st["ema_short"]
        elif tf_minutes == 15:
            if st["ema_long"] is None:
                st["ema_long"] = c
            else:
                st["ema_long"] = self.alpha * c + (1 - self.alpha) * st["ema_long"]
        else:
            return None

        # If already in position, do not generate new entries here
        if st["position"] is not None:
            return None

        # If flat but daily limit reached, ignore new entries
        if not self._can_trade(st, ts):
            return None

        # SHORT SIDE (5m)
        if tf_minutes == 5 and st["ema_short"] is not None:
            ema_short = st["ema_short"]

            if st["signal_short"] is None:
                if h > ema_short and l > ema_short:
                    st["signal_short"] = {"high": h, "low": l}
                return None

            sig = st["signal_short"]

            if c < sig["low"]:
                entry = c
                sl = sig["high"]
                risk = sl - entry
                if risk <= 0:
                    st["signal_short"] = None
                    return None
                tp = entry - self.rr * risk
                trade_id = self._record_trade(st)
                st["position"] = {
                    "side": "short",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "trade_id": trade_id,
                }
                st["signal_short"] = None
                return {
                    "symbol": symbol,
                    "signal": "short_entry",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "trade_id": trade_id,
                }

            if l > ema_short and c >= sig["low"]:
                st["signal_short"] = {"high": h, "low": l}
                return None

            if l <= ema_short:
                st["signal_short"] = None
            return None

        # LONG SIDE (15m)
        if tf_minutes == 15 and st["ema_long"] is not None:
            ema_long = st["ema_long"]

            if st["signal_long"] is None:
                if l < ema_long and h < ema_long:
                    st["signal_long"] = {"high": h, "low": l}
                return None

            sig = st["signal_long"]

            if c > sig["high"]:
                entry = c
                sl = sig["low"]
                risk = entry - sl
                if risk <= 0:
                    st["signal_long"] = None
                    return None
                tp = entry + self.rr * risk
                trade_id = self._record_trade(st)
                st["position"] = {
                    "side": "long",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "trade_id": trade_id,
                }
                st["signal_long"] = None
                return {
                    "symbol": symbol,
                    "signal": "long_entry",
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "trade_id": trade_id,
                }

            if h < ema_long and c <= sig["high"]:
                st["signal_long"] = {"high": h, "low": l}
                return None

            if h >= ema_long:
                st["signal_long"] = None
            return None

        return None

    def exit_signal(self, symbol, price):
        """
        Check if current price triggers SL/TP for the open position.

        Returns:
            None or dict:
              {
                "symbol": symbol,
                "signal": "exit_sl"/"exit_tp",
                "exit_price": float,
                "trade_id": int,
                "side": "long"/"short"
              }
        """
        st = self.state[symbol]
        pos = st["position"]
        if pos is None:
            return None

        side = pos["side"]
        sl = pos["sl"]
        tp = pos["tp"]

        if side == "short":
            if price >= sl:
                exit_price = sl
                sig = "exit_sl"
            elif price <= tp:
                exit_price = tp
                sig = "exit_tp"
            else:
                return None
        else:  # long
            if price <= sl:
                exit_price = sl
                sig = "exit_sl"
            elif price >= tp:
                exit_price = tp
                sig = "exit_tp"
            else:
                return None

        trade_id = pos["trade_id"]

        # Strategy keeps position until force_flat() is called externally
        return {
            "symbol": symbol,
            "signal": sig,
            "exit_price": exit_price,
            "trade_id": trade_id,
            "side": side,
        }
