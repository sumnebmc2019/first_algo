import os
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


def load_rt_equity_state(path="rt_equity.yaml"):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        return data.get("equity")
    except Exception as e:
        print("Failed to load rt_equity.yaml:", e)
        return None


def save_rt_equity_state(equity, path="rt_equity.yaml"):
    try:
        with open(path, "w") as f:
            yaml.safe_dump({"equity": float(equity)}, f)
        print("Saved rt_equity.yaml")
    except Exception as e:
        print("Failed to save rt_equity.yaml:", e)


class CandleBuilder:
    """Build fixed 5min/15min candles aligned to clock multiples from tick prices."""

    def __init__(self, tf_minutes=5):
        self.tf_seconds = tf_minutes * 60
        self.current = {}  # symbol_timestamp -> candle dict

    def update(self, symbol, price, ts):
        """Returns completed candle snapped to 5min/15min boundaries."""
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
                self.current = {
                    k: v for k, v in self.current.items() if ts - v["start"] < 3600
                }
            return completed


def main():
    cfg = load_config("config.yaml")
    mode = cfg.get("mode", "paper")
    symbols = cfg.get("symbols", [])
    interval = cfg.get("interval_seconds", 5)
    slippage = cfg.get("slippage", 0.0)
    starting_cash_cfg = cfg.get("starting_cash_realtime", 100000)
    risk_per_trade = cfg.get("risk_per_trade", 0.01)

    # Load carry-over equity for realtime compounding
    rt_state_path = os.path.join(os.getcwd(), "rt_equity.yaml")
    equity_state = load_rt_equity_state(rt_state_path)
    if equity_state is not None:
        starting_cash = float(equity_state)
        print(f"[RT] Loaded carry-over equity: ₹{starting_cash:,.2f}")
    else:
        starting_cash = starting_cash_cfg
        print(f"[RT] Using config starting_cash_realtime: ₹{starting_cash:,.2f}")

    trader = PaperTrader(starting_cash=starting_cash, slippage=slippage)
    strategy = FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=5)

    tg_cfg = cfg.get("telegram", {})
    notifier = None    # will be used directly via notifier.send(...)
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

    # (symbol, trade_id) -> info incl entry_msg_ids
    open_trades = {}

    in_market = False
    day_start_equity = None

    if notifier:
        start_msg = (
            "🤖 <b>RT BOT STARTED</b>\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Symbols:</b> {', '.join(symbols)}\n"
            f"<b>Starting Equity:</b> ₹{starting_cash:,.0f}"
        )
        notifier.send(start_msg)

    try:
        while True:
            # Skip weekends (Sat=5, Sun=6)
            now = datetime.now()
            if now.weekday() >= 5:
                time.sleep(interval)
                continue

            # Market hours: 09:00-16:00 IST
            current_time = now.time()
            market_start = datetime.strptime("09:00", "%H:%M").time()
            market_end = datetime.strptime("16:00", "%H:%M").time()

            # Detect market open/close to send EOD summary and track daily starting equity
            if market_start <= current_time <= market_end:
                if not in_market:
                    in_market = True
                    day_start_equity = trader.equity(market_prices)
            else:
                if in_market:
                    # Market just closed — send EOD summary and save equity for next session
                    in_market = False
                    day_end_equity = trader.equity(market_prices)
                    net_pnl = day_end_equity - (day_start_equity or 0)
                    summary = (
                        "📊 <b>Daily Summary</b>\n"
                        f"<b>Start Equity:</b> ₹{(day_start_equity or 0):,.0f}\n"
                        f"<b>End Equity:</b> ₹{day_end_equity:,.0f}\n"
                        f"<b>Net P&L:</b> ₹{net_pnl:,.0f}"
                    )
                    if notifier:
                        notifier.send(summary)
                    save_rt_equity_state(day_end_equity, rt_state_path)
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
                    sig_5 = strategy.update_candle(s, o, h, l, c, ts, tf_minutes=5)
                    if sig_5:
                        sig_5 = {k: v for k, v in sig_5.items() if k != "symbol"}
                        sig = sig_5
                        print(f"[{s}] 5m SIGNAL: {sig['signal']}")

                # 15m signal (long-term, overrides 5m)
                if completed_15m is not None:
                    o2, h2, l2, c2 = completed_15m
                    sig2 = strategy.update_candle(s, o2, h2, l2, c2, ts, tf_minutes=15)
                    if sig2:
                        sig2 = {k: v for k, v in sig2.items() if k != "symbol"}
                        sig = sig2
                        print(f"[{s}] 15m SIGNAL: {sig['signal']}")

                # ENTRY handling – FiveEMA owns position
                if sig and sig.get("signal") in ("short_entry", "long_entry"):
                    st = strategy.state[s]
                    pos = st["position"]
                    trade_id = sig["trade_id"]

                    if not pos or pos.get("trade_id") != trade_id:
                        print(
                            f"[{s}] WARNING: entry signal but no matching position "
                            f"pos={pos}, sig={sig}"
                        )
                        continue

                    entry = sig["entry"]
                    sl = sig["sl"]
                    tp = sig["tp"]
                    side_new = "long" if sig["signal"] == "long_entry" else "short"

                    risk = abs(entry - sl)
                    if risk <= 0:
                        continue
                    current_equity = trader.equity(market_prices)
                    risk_amount = current_equity * risk_per_trade
                    qty = int(risk_amount / risk)
                    if qty <= 0:
                        continue

                    if side_new == "long":
                        ok, ex_price = trader.buy_market(s, qty, entry)
                    else:
                        ok, ex_price = trader.sell_market(s, qty, entry)

                    if ok:
                        text = (
                            "📈 <b>RT ENTRY</b>\n"
                            f"<b>Symbol:</b> {s}\n"
                            f"<b>Trade ID:</b> #{trade_id}\n"
                            f"<b>Side:</b> {side_new.upper()}\n"
                            f"<b>Qty:</b> {qty}\n"
                            f"<b>Entry:</b> ₹{ex_price:,.2f}\n"
                            f"<b>SL:</b> ₹{sl:,.2f}\n"
                            f"<b>TP:</b> ₹{tp:,.2f}"
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

                # EXIT handling – FiveEMA owns position
                st = strategy.state[s]
                if st["position"] is not None:
                    exit_sig = strategy.exit_signal(s, price)
                else:
                    exit_sig = None

                if exit_sig and exit_sig.get("signal"):
                    exit_sig = {k: v for k, v in exit_sig.items() if k != "symbol"}
                    side = exit_sig["side"]
                    exit_price = exit_sig["exit_price"]
                    trade_id = exit_sig["trade_id"]

                    pos = st["position"]
                    info = open_trades.get((s, trade_id))

                    if not pos or pos["trade_id"] != trade_id or not info:
                        continue

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
                        "📉 <b>RT EXIT</b>\n"
                        f"<b>Symbol:</b> {s}\n"
                        f"<b>Trade ID:</b> #{trade_id} ({exit_sig['signal'].upper()})\n"
                        f"<b>Side:</b> {side.upper()}\n"
                        f"<b>Qty:</b> {qty}\n"
                        f"<b>Entry:</b> ₹{entry_price:,.2f}\n"
                        f"<b>Exit:</b> ₹{actual_exit:,.2f}\n"
                        f"<b>Trade P&L:</b> ₹{pnl_trade:,.2f}\n"
                        f"<b>Total Equity:</b> ₹{equity_now:,.2f}"
                    )
                    reply_id = None
                    if info["entry_msg_ids"]:
                        reply_id = next(iter(info["entry_msg_ids"].values()))
                    if notifier:
                        notifier.send(text, reply_to_message_id=reply_id)

                    # flatten state
                    strategy.force_flat(s)
                    del open_trades[(s, trade_id)]

            # LTP ping every 10min for ALL symbols (9:00-16:00)
            now_ts = time.time()
            if now_ts - last_ltp_ping >= ltp_ping_interval:
                current_time = now.time()
                if market_start <= current_time <= market_end:
                    lines = ["🕐 LTP UPDATE (all symbols)"]
                    valid_prices = 0
                    for s, price in market_prices.items():
                        if price:
                            lines.append(f"{s}: ₹{price:,.1f}")
                            valid_prices += 1
                    if valid_prices > 0 and notifier:
                        notifier.send("\n".join(lines))
                        print("LTP ping sent:", lines)
                last_ltp_ping = now_ts

            time.sleep(interval)

    except KeyboardInterrupt:
        equity = trader.equity(market_prices)
        print("Stopped by user. Final Equity:", equity)
        save_rt_equity_state(equity, rt_state_path)
        if notifier:
            notifier.send(f"🛑 RT BOT STOPPED | Final Equity: ₹{equity:,.0f}")
    except Exception as e:
        equity = trader.equity(market_prices)
        save_rt_equity_state(equity, rt_state_path)
        print(f"BOT ERROR: {e}")
        if notifier:
            notifier.send(f"🚨 RT BOT CRASHED: {e}")
        raise
    else:
        equity = trader.equity(market_prices)
        save_rt_equity_state(equity, rt_state_path)
        if notifier:
            notifier.send(f"🛑 RT BOT STOPPED | Final Equity: ₹{equity:,.0f}")


if __name__ == "__main__":
    main()
