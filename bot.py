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

        Returns:
            completed_candle (o, h, l, c) or None.
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
    symbols_cfg = cfg.get("symbols", [])
    interval = cfg.get("interval_seconds", 5)
    qty = cfg.get("quantity", 1)
    starting = cfg.get("starting_cash", 100000)
    slippage = cfg.get("slippage", 0.0)

    # Flatten symbols list
    symbols = []
    for item in symbols_cfg:
        if isinstance(item, dict):
            symbols.extend(item.keys())
        else:
            symbols.append(item)

    trader = PaperTrader(starting_cash=starting, slippage=slippage)
    strategy = {s: FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=5) for s in symbols}

    # Telegram notifier (multi chat_ids)
    tg_cfg = cfg.get("telegram", {})
    use_telegram = tg_cfg.get("enable", False)
    notifier = None
    if use_telegram:
        chat_ids = tg_cfg.get("chat_ids") or tg_cfg.get("chat_id")
        notifier = TelegramNotifier(
            bot_token=tg_cfg.get("bot_token"),
            chat_ids=chat_ids,
        )

    # SmartAPI / Simulated feed
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

    # 5-minute LTP update timer
    last_ltp_ping = 0
    ltp_ping_interval = 300  # seconds

    if notifier:
        start_msg = (
            "BOT STARTED ✅\n"
            f"Mode: {mode}\n"
            f"Symbols: {', '.join(symbols)}"
        )
        notifier.send(start_msg)

    try:
        while True:
            for s in symbols:
                # get latest price
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

                # build 5m and 15m candles
                completed_5m = candle_5m.update(s, price, ts)
                completed_15m = candle_15m.update(s, price, ts)

                sig = None

                # short side (5m)
                if completed_5m is not None:
                    o, h, l, c = completed_5m
                    sig = strategy[s].update_candle(o, h, l, c, ts, tf_minutes=5)

                # long side (15m)
                if completed_15m is not None:
                    o2, h2, l2, c2 = completed_15m
                    sig2 = strategy[s].update_candle(o2, h2, l2, c2, ts, tf_minutes=15)
                    if sig2 is not None:
                        sig = sig2

                if sig is None:
                    continue

                # handle entries
                if sig["signal"] == "short_entry":
                    ok, ex_price = trader.sell_market(s, qty, sig["entry"])
                    msg = (
                        f"[{s}] SHORT ENTRY\n"
                        f"Qty: {qty}\n"
                        f"Entry: {ex_price:.2f}\n"
                        f"SL: {sig['sl']:.2f}\n"
                        f"TP: {sig['tp']:.2f}"
                    )
                    print(msg, "ok=", ok)
                    if notifier and ok:
                        notifier.send(msg)

                elif sig["signal"] == "long_entry":
                    ok, ex_price = trader.buy_market(s, qty, sig["entry"])
                    msg = (
                        f"[{s}] LONG ENTRY\n"
                        f"Qty: {qty}\n"
                        f"Entry: {ex_price:.2f}\n"
                        f"SL: {sig['sl']:.2f}\n"
                        f"TP: {sig['tp']:.2f}"
                    )
                    print(msg, "ok=", ok)
                    if notifier and ok:
                        notifier.send(msg)

                # handle exits with accurate per-trade P&L
                elif sig["signal"] in ("exit_sl", "exit_tp"):
                    pos_qty = trader.positions.get(s, 0)
                    side = "long" if pos_qty > 0 else "short"

                    if side == "short":
                        ok, ex_price = trader.buy_market(s, qty, sig["exit_price"])
                    else:
                        ok, ex_price = trader.sell_market(s, qty, sig["exit_price"])

                    # use avg entry price from PaperTrader for P&L
                    # note: after this trade, position may be 0, so capture avg before trade
                    entry_price = None
                    if side == "long":
                        # for long, avg_price was the buy price
                        entry_price = sig["exit_price"] if s not in trader.avg_price else trader.avg_price.get(s, sig["exit_price"])
                    else:
                        entry_price = sig["exit_price"] if s not in trader.avg_price else trader.avg_price.get(s, sig["exit_price"])

                    # safer: get entry from last opposing trade in trade_log
                    if entry_price is None and trader.trade_log:
                        for t in reversed(trader.trade_log):
                            if t["symbol"] == s:
                                entry_price = t["price"]
                                break

                    if entry_price is None:
                        entry_price = sig["exit_price"]

                    pnl_trade = trader.realized_trade_pnl(
                        "long" if side == "long" else "short",
                        s,
                        qty,
                        entry_price,
                        ex_price if ok else sig["exit_price"],
                    )

                    msg = (
                        f"[{s}] EXIT {sig['signal'].upper()}\n"
                        f"Side: {side.upper()}\n"
                        f"Qty: {qty}\n"
                        f"Price: {ex_price:.2f}\n"
                        f"P&L: {pnl_trade:.2f}"
                    )
                    print(msg, "ok=", ok)
                    if notifier and ok:
                        notifier.send(msg)

            # every 5 minutes: send LTP of NIFTY and BANKNIFTY only,
            # but only between 08:55 and 16:05 IST
            now = time.time()
            if now - last_ltp_ping >= ltp_ping_interval:
                local_t = time.localtime(now)
                current_minutes = local_t.tm_hour * 60 + local_t.tm_min
                start_minutes = 8 * 60 + 55   # 08:55
                end_minutes = 16 * 60 + 5     # 16:05

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
