import time
import yaml

from strategy import FiveEMA
from paper_trader import PaperTrader
from data_feed import SimulatedFeed, SmartAPIConnector
from telegram_notifier import TelegramNotifier


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


class CandleBuilder:
    """Build fixed-length candles (e.g., 5-minute) from tick prices."""

    def __init__(self, bar_seconds=300):
        self.bar_seconds = bar_seconds
        self.current = {}  # symbol -> candle dict

    def update(self, symbol, price, ts):
        """
        Update candle for a symbol with new tick.

        Returns completed_candle (o, h, l, c) or None.
        """
        cndl = self.current.get(symbol)
        if cndl is None:
            self.current[symbol] = {
                "start": ts,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return None

        if ts - cndl["start"] < self.bar_seconds:
            cndl["h"] = max(cndl["h"], price)
            cndl["l"] = min(cndl["l"], price)
            cndl["c"] = price
            return None
        else:
            completed = (cndl["o"], cndl["h"], cndl["l"], cndl["c"])
            self.current[symbol] = {
                "start": ts,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return completed


def main():
    cfg = load_config("config.yaml")
    mode = cfg.get("mode", "paper")
    symbols = cfg.get("symbols", [])
    interval = cfg.get("interval_seconds", 5)
    slippage = cfg.get("slippage", 0.0)
    starting_cash = cfg.get("starting_cash_realtime", 100000)
    risk_per_trade = cfg.get("risk_per_trade", 0.01)

    trader = PaperTrader(starting_cash=starting_cash, slippage=slippage)
    strategy = FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=5)

    tg_cfg = cfg.get("telegram", {})
    notifier = None
    if tg_cfg.get("enable", False):
        chat_ids = tg_cfg.get("chat_ids") or tg_cfg.get("chat_id")
        notifier = TelegramNotifier(
            bot_token=tg_cfg.get("bot_token"),
            chat_ids=chat_ids,
        )

    sa_cfg = cfg.get("smartapi", {})
    use_smartapi = sa_cfg.get("enable", False)

    if use_smartapi:
        conn = SmartAPIConnector(
            api_key=sa_cfg.get("api_key"),
            client_id=sa_cfg.get("client_id"),
            password=sa_cfg.get("password"),
            totp_secret=sa_cfg.get("totp_secret"),
            instruments=sa_cfg.get("instruments"),
            notifier=notifier,
        )
    else:
        conn = SimulatedFeed()

    print(f"Starting bot in {mode} mode for symbols: {symbols}")
    market_prices = {s: None for s in symbols}

    candle_5m = CandleBuilder(bar_seconds=300)
    candle_15m = CandleBuilder(bar_seconds=900)

    last_ltp_ping = 0
    ltp_ping_interval = 300  # seconds

    # track open trades per (symbol, trade_id)
    open_trades = {}

    if notifier:
        start_msg = (
            "BOT STARTED ✅\n"
            f"Mode: {mode}\n"
            f"Symbols: {', '.join(symbols)}\n"
            f"Start capital: {starting_cash}"
        )
        notifier.send(start_msg)

    try:
        while True:
            for s in symbols:
                try:
                    tick = conn.get_price(s)
                except Exception as e:
                    print(f"[{s}] PRICE ERROR: {e}")
                    if notifier:
                        notifier.send(f"[{s}] PRICE ERROR: {e}")
                    continue

                price = tick["price"]
                ts = tick["time"]
                market_prices[s] = price

                completed_5m = candle_5m.update(s, price, ts)
                completed_15m = candle_15m.update(s, price, ts)

                sig = None

                if completed_5m is not None:
                    o, h, l, c = completed_5m
                    sig = strategy.update_candle(s, o, h, l, c, ts, tf_minutes=5)

                if completed_15m is not None:
                    o2, h2, l2, c2 = completed_15m
                    sig2 = strategy.update_candle(s, o2, h2, l2, c2, ts, tf_minutes=15)
                    if sig2 is not None:
                        sig = sig2

                # Entry handling
                if sig and sig["signal"] in ("short_entry", "long_entry"):
                    st = strategy.state[s]
                    if st["position"] is not None:
                        continue  # already in a trade for this symbol

                    entry = sig["entry"]
                    sl = sig["sl"]
                    tp = sig["tp"]
                    side_new = "long" if sig["signal"] == "long_entry" else "short"

                    risk = abs(entry - sl)
                    if risk <= 0:
                        continue
                    risk_amount = trader.cash * risk_per_trade
                    qty = int(risk_amount / risk)
                    if qty <= 0:
                        continue

                    if side_new == "long":
                        ok, ex_price = trader.buy_market(s, qty, entry)
                    else:
                        ok, ex_price = trader.sell_market(s, qty, entry)

                    if ok:
                        trade_id = sig["trade_id"]
                        st["position"] = {
                            "side": side_new,
                            "entry": ex_price,
                            "sl": sl,
                            "tp": tp,
                            "trade_id": trade_id,
                        }
                        text = (
                            f"[RT ENTRY] {s} #{trade_id}\n"
                            f"Side: {side_new.upper()}\n"
                            f"Qty: {qty}\n"
                            f"Entry: {ex_price:.2f}\n"
                            f"SL: {sl:.2f}\n"
                            f"TP: {tp:.2f}"
                        )
                        entry_msg_ids = {}
                        if notifier:
                            entry_msg_ids = notifier.send(text)
                        open_trades[(s, trade_id)] = {
                            "side": side_new,
                            "qty": qty,
                            "entry": ex_price,
                            "sl": sl,
                            "tp": tp,
                            "entry_msg_ids": entry_msg_ids,
                        }

                # Exit handling with current price
                exit_sig = strategy.exit_signal(s, price)
                if exit_sig:
                    side = exit_sig["side"]
                    exit_price = exit_sig["exit_price"]
                    trade_id = exit_sig["trade_id"]
                    info = open_trades.get((s, trade_id))
                    if info:
                        qty = info["qty"]
                        entry_price = info["entry"]

                        if side == "short":
                            ok, ex_price = trader.buy_market(s, qty, exit_price)
                        else:
                            ok, ex_price = trader.sell_market(s, qty, exit_price)

                        actual_exit = ex_price if ok else exit_price
                        pnl_trade = trader.record_realized_trade_pnl(
                            s, side, qty, entry_price, actual_exit
                        )
                        equity_now = trader.equity(market_prices)

                        text = (
                            f"[RT EXIT] {s} #{trade_id} {exit_sig['signal'].upper()}\n"
                            f"Side: {side.upper()}\n"
                            f"Qty: {qty}\n"
                            f"Entry: {entry_price:.2f}\n"
                            f"Exit: {actual_exit:.2f}\n"
                            f"Trade P&L: {pnl_trade:.2f}\n"
                            f"Equity: {equity_now:.2f}"
                        )
                        reply_id = None
                        if info["entry_msg_ids"]:
                            reply_id = next(iter(info["entry_msg_ids"].values()))
                        if notifier:
                            notifier.send(text, reply_to_message_id=reply_id)
                        del open_trades[(s, trade_id)]

            now = time.time()
            if now - last_ltp_ping >= ltp_ping_interval:
                local_t = time.localtime(now)
                current_minutes = local_t.tm_hour * 60 + local_t.tm_min
                start_minutes = 8 * 60 + 55
                end_minutes = 16 * 60 + 5

                if start_minutes <= current_minutes <= end_minutes:
                    nifty_ltp = market_prices.get("NIFTY")
                    banknifty_ltp = market_prices.get("BANKNIFTY")

                    lines = ["SPOT LTP UPDATE (every 5 min)"]
                    if nifty_ltp is not None:
                        lines.append(f"NIFTY: {nifty_ltp:.2f}")
                    if banknifty_ltp is not None:
                        lines.append(f"BANKNIFTY: {banknifty_ltp:.2f}")

                    msg = "\n".join(lines)
                    print(msg)
                    if notifier:
                        notifier.send(msg)

                last_ltp_ping = now

            time.sleep(interval)

    except KeyboardInterrupt:
        print("Stopped by user. Trades:")
        for t in trader.trade_log:
            print(t)
        if notifier:
            notifier.send("BOT STOPPED ⛔ (KeyboardInterrupt)")

    except Exception as e:
        print(f"BOT ERROR: {e}")
        if notifier:
            notifier.send(f"BOT STOPPED ⛔ due to ERROR:\n{e}")
        raise

    else:
        if notifier:
            notifier.send("BOT STOPPED ⛔ (loop ended normally)")


if __name__ == "__main__":
    main()
