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
        self.current = {}  # symbol -> candle dict

    def update(self, symbol, price, ts):
        """Returns completed candle (o,h,l,c) snapped to 5min/15min boundaries."""
        # Snap to candle boundary
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
            # Clean old candles
            if len(self.current) > 100:
                self.current = {}
            return completed


def main():
    cfg = load_config("config.yaml")
    symbols = cfg.get("symbols", [])
    interval = cfg.get("interval_seconds", 5)
    starting_cash = cfg.get("starting_cash_realtime", 100000)
    risk_per_trade = cfg.get("risk_per_trade", 0.01)
    slippage = cfg.get("slippage", 0.0)

    trader = PaperTrader(starting_cash=starting_cash, slippage=slippage)
    strategy = FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=5)

    tg_cfg = cfg.get("telegram", {})
    notifier = None
    if tg_cfg.get("enable", False):
        notifier = TelegramNotifier(bot_token=tg_cfg["bot_token"], chat_ids=tg_cfg.get("chat_ids", []))

    sa_cfg = cfg.get("smartapi", {})
    if sa_cfg.get("enable", False):
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

    print(f"Starting realtime bot for {symbols}")
    market_prices = {s: None for s in symbols}
    candle_5m = CandleBuilder(tf_minutes=5)
    candle_15m = CandleBuilder(tf_minutes=15)

    last_ltp_ping = 0
    ltp_ping_interval = 600  # 10 minutes

    open_trades = {}  # (symbol, trade_id) -> info
    strategy_signals = 0  # debug counter

    if notifier:
        notifier.send(f"RT BOT STARTED ✅ {len(symbols)} symbols, capital={starting_cash:,}")

    try:
        while True:
            now = datetime.now()
            weekday = now.weekday()  # 0=Mon, 6=Sun
            
            # Skip weekends for realtime bot
            if weekday >= 5:  # Sat=5, Sun=6
                time.sleep(interval)
                continue

            # Market hours check: 9:15-15:30 IST
            current_time = now.time()
            if not (datetime.strptime("09:15", "%H:%M").time() <= current_time <= datetime.strptime("15:30", "%H:%M").time()):
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

                # Build candles
                completed_5m = candle_5m.update(s, price, ts)
                completed_15m = candle_15m.update(s, price, ts)

                sig = None
                if completed_5m:
                    o, h, l, c = completed_5m
                    sig = strategy.update_candle(s, o, h, l, c, ts, tf_minutes=5)
                    if sig:
                        strategy_signals += 1
                        print(f"[{s}] 5m SIGNAL: {sig['signal']}")

                if completed_15m:
                    o, h, l, c = completed_15m
                    sig2 = strategy.update_candle(s, o, h, l, c, ts, tf_minutes=15)
                    if sig2:
                        sig = sig2
                        strategy_signals += 1
                        print(f"[{s}] 15m SIGNAL: {sig2['signal']}")

                # Entry
                if sig and sig["signal"] in ("long_entry", "short_entry"):
                    st = strategy.state[s]
                    if st.get("position"):
                        continue

                    entry, sl, tp = sig["entry"], sig["sl"], sig["tp"]
                    side_new = "long" if sig["signal"] == "long_entry" else "short"
                    risk = abs(entry - sl)
                    
                    if risk > 0:
                        risk_amount = trader.cash * risk_per_trade
                        qty = max(1, int(risk_amount / risk))
                        
                        if side_new == "long":
                            ok, ex_price = trader.buy_market(s, qty, entry)
                        else:
                            ok, ex_price = trader.sell_market(s, qty, entry)

                        if ok:
                            trade_id = sig["trade_id"]
                            st["position"] = {
                                "side": side_new, "entry": ex_price, "sl": sl, 
                                "tp": tp, "trade_id": trade_id
                            }
                            text = (
                                f"[RT ENTRY] {s} #{trade_id}\n"
                                f"Side: {side_new.upper()}\n"
                                f"Qty: {qty}\nEntry: {ex_price:.1f}\n"
                                f"SL: {sl:.1f} TP: {tp:.1f}"
                            )
                            entry_msg_ids = notifier.send(text) if notifier else {}
                            open_trades[(s, trade_id)] = {
                                "side": side_new, "qty": qty, "entry": ex_price,
                                "sl": sl, "tp": tp, "entry_msg_ids": entry_msg_ids
                            }

                # Exit check
                exit_sig = strategy.exit_signal(s, price)
                if exit_sig:
                    trade_id = exit_sig["trade_id"]
                    info = open_trades.pop((s, trade_id), None)
                    if info:
                        side, qty, entry_price = info["side"], info["qty"], info["entry"]
                        exit_price = exit_sig["exit_price"]
                        
                        if side == "short":
                            ok, ex_price = trader.buy_market(s, qty, exit_price)
                        else:
                            ok, ex_price = trader.sell_market(s, qty, exit_price)
                        
                        actual_exit = ex_price if ok else exit_price
                        pnl_trade = trader.record_realized_trade_pnl(s, side, qty, entry_price, actual_exit)
                        equity = trader.equity(market_prices)
                        
                        text = (
                            f"[RT EXIT] {s} #{trade_id} {exit_sig['signal'].upper()}\n"
                            f"Entry: {entry_price:.1f} → Exit: {actual_exit:.1f}\n"
                            f"Qty: {qty} P&L: ₹{pnl_trade:,.0f}\n"
                            f"Total Equity: ₹{equity:,.0f}"
                        )
                        reply_id = next(iter(info["entry_msg_ids"].values()), None)
                        if notifier:
                            notifier.send(text, reply_to_message_id=reply_id)

            # LTP ping every 10min for ALL symbols (9:00-16:00)
            now_ts = time.time()
            if now_ts - last_ltp_ping >= ltp_ping_interval:
                current_time = now.time()
                if datetime.strptime("09:00", "%H:%M").time() <= current_time <= datetime.strptime("16:00", "%H:%M").time():
                    lines = ["🕐 LTP UPDATE (all symbols)"]
                    for s, price in market_prices.items():
                        if price:
                            lines.append(f"{s}: ₹{price:.1f}")
                    if notifier:
                        notifier.send("\n".join(lines))
                last_ltp_ping = now_ts

            time.sleep(interval)

    except KeyboardInterrupt:
        equity = trader.equity(market_prices)
        if notifier:
            notifier.send(f"RT BOT STOPPED 📊 Final Equity: ₹{equity:,.0f}")
    except Exception as e:
        print(f"RT BOT CRASH: {e}")
        if notifier:
            notifier.send(f"RT BOT CRASHED ❌ {e}")


if __name__ == "__main__":
    main()
