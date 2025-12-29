import time
import yaml
from datetime import datetime

from strategy import FiveEMA
from paper_trader import PaperTrader
from data_feed import SimulatedFeed, SmartAPIConnector
from telegram_notifier import TelegramNotifier


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


class CandleBuilder:
    """Build fixed 5min/15min candles aligned to clock multiples from tick prices."""

    def __init__(self, tf_minutes=5):
        self.tf_seconds = tf_minutes * 60
        self.current = {}  # symbol_timestamp -> candle dict

    def update(self, symbol, price, ts):
        """Returns completed candle snapped to 5min/15min boundaries."""
        # Snap to candle boundary: floor to nearest 5min/15min
        candle_start = (int(ts) // self.tf_seconds) * self.tf_seconds
        
        cndl_key = f"{symbol}_{candle_start}"
        cndl = self.current.get(cndl_key)
        
        if cndl is None:
            self.current[cndl_key] = {
                "start": candle_start,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return None

        if ts - candle_start < self.tf_seconds:
            cndl["h"] = max(cndl["h"], price)
            cndl["l"] = min(cndl["l"], price)
            cndl["c"] = price
            return None
        else:
            completed = (cndl["o"], cndl["h"], cndl["l"], cndl["c"])
            # Clean old candles (keep last 20 per symbol)
            if len(self.current) > 100:
                self.current = {k: v for k, v in self.current.items() if ts - v["start"] < 3600}
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

    candle_5m = CandleBuilder(tf_minutes=5)
    candle_15m = CandleBuilder(tf_minutes=15)

    last_ltp_ping = 0
    ltp_ping_interval = 600  # 10 minutes

    open_trades = {}  # (symbol, trade_id) -> info

    if notifier:
        start_msg = (
            "🤖 RT BOT STARTED ✅\n"
            f"Mode: {mode}\n"
            f"Symbols: {', '.join(symbols)}\n"
            f"Capital: ₹{starting_cash:,}"
        )
        notifier.send(start_msg)

    try:
        while True:
            # Skip weekends (Sat=5, Sun=6)
            now = datetime.now()
            if now.weekday() >= 5:
                time.sleep(interval)
                continue

            # Market hours: 9:15-15:30 IST
            current_time = now.time()
            market_start = datetime.strptime("09:15", "%H:%M").time()
            market_end = datetime.strptime("15:30", "%H:%M").time()
            
            if not (market_start <= current_time <= market_end):
                time.sleep(interval)
                continue

            for s in symbols:
                try:
                    tick = conn.get_price(s)
                    price = tick["price"]
                    ts = tick["time"]
                except Exception as e:
                    print(f"[{s}] PRICE ERROR: {e}")
                    continue

                market_prices[s] = price

                # Build clock-aligned candles
                completed_5m = candle_5m.update(s, price, ts)
                completed_15m = candle_15m.update(s, price, ts)

                sig = None

                # 5m signal (short-term)
                if completed_5m is not None:
                    o, h, l, c = completed_5m
                    sig = strategy.update_candle(s, o, h, l, c, ts, tf_minutes=5)
                    if sig:
                        sig = {k: v for k in sig if k != "symbol"}
                        print(f"[{s}] 5m SIGNAL: {sig['signal']}")

                # 15m signal (long-term, overrides 5m)
                if completed_15m is not None:
                    o2, h2, l2, c2 = completed_15m
                    sig2 = strategy.update_candle(s, o2, h2, l2, c2, ts, tf_minutes=15)
                    if sig2:
                        sig = {k: v for k in sig2 if k != "symbol"}
                        print(f"[{s}] 15m SIGNAL: {sig['signal']}")

                # Entry handling
                if sig and sig.get("signal") in ("short_entry", "long_entry"):
                    st = strategy.state[s]
                    if st["position"] is not None:
                        continue  # already in trade

                    entry = sig["entry"]
                    sl = sig["sl"]
                    tp = sig["tp"]
                    side_new = "long" if sig["signal"] == "long_entry" else "short"

                    risk = abs(entry - sl)
                    if risk <= 0:
                        continue
                    risk_amount = trader.cash * risk_per_trade
                    qty = max(1, int(risk_amount / risk))

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
                            f"📈 [RT ENTRY] {s} #{trade_id}\n"
                            f"Side: {side_new.upper()}\n"
                            f"Qty: {qty}\n"
                            f"Entry: ₹{ex_price:,.1f}\n"
                            f"SL: ₹{sl:,.1f}\n"
                            f"TP: ₹{tp:,.1f}"
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

                # Exit handling
                exit_sig = strategy.exit_signal(s, price)
                if exit_sig and exit_sig.get("signal"):
                    exit_sig = {k: v for k in exit_sig if k != "symbol"}
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
                            f"📉 [RT EXIT] {s} #{trade_id} {exit_sig['signal'].upper()}\n"
                            f"Entry: ₹{entry_price:,.1f} → Exit: ₹{actual_exit:,.1f}\n"
                            f"Qty: {qty} | P&L: ₹{pnl_trade:,.0f}\n"
                            f"💰 Total Equity: ₹{equity_now:,.0f}"
                        )
                        reply_id = None
                        if info["entry_msg_ids"]:
                            reply_id = next(iter(info["entry_msg_ids"].values()))
                        if notifier:
                            notifier.send(text, reply_to_message_id=reply_id)
                        del open_trades[(s, trade_id)]

            # LTP ping every 10min for ALL symbols (9:00-16:00)
            now_ts = time.time()
            if now_ts - last_ltp_ping >= ltp_ping_interval:
                current_time = now.time()
                if datetime.strptime("09:00", "%H:%M").time() <= current_time <= datetime.strptime("16:00", "%H:%M").time():
                    lines = ["🕐 LTP UPDATE (all symbols)"]
                    valid_prices = 0
                    for s, price in market_prices.items():
                        if price:
                            lines.append(f"{s}: ₹{price:,.1f}")
                            valid_prices += 1
                    if valid_prices > 0:
                        if notifier:
                            notifier.send("\n".join(lines))
                        print("LTP ping sent:", lines)
                last_ltp_ping = now_ts

            time.sleep(interval)

    except KeyboardInterrupt:
        equity = trader.equity(market_prices)
        print("Stopped by user. Final Equity:", equity)
        if notifier:
            notifier.send(f"🛑 RT BOT STOPPED | Final Equity: ₹{equity:,.0f}")
    except Exception as e:
        print(f"BOT ERROR: {e}")
        if notifier:
            notifier.send(f"🚨 RT BOT CRASHED: {e}")
        raise
    else:
        if notifier:
            equity = trader.equity(market_prices)
            notifier.send(f"🛑 RT BOT STOPPED | Final Equity: ₹{equity:,.0f}")


if __name__ == "__main__":
    main()
