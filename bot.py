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

    tg_cfg = cfg.get("telegram", {})
    use_telegram = tg_cfg.get("enable", False)
    notifier = None
    if use_telegram:
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

    last_pnl_ping = 0
    pnl_ping_interval = 60  # seconds

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
                    sig = strategy[s].update_candle(o, h, l, c, ts, tf_minutes=5)

                if completed_15m is not None:
                    o2, h2, l2, c2 = completed_15m
                    sig2 = strategy[s].update_candle(o2, h2, l2, c2, ts, tf_minutes=15)
                    if sig2 is not None:
                        sig = sig2

                if sig is None:
                    continue

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

                elif sig["signal"] in ("exit_sl", "exit_tp"):
                    pos_qty = trader.positions.get(s, 0)
                    side = "long" if pos_qty > 0 else "short"

                    if side == "short":
                        ok, ex_price = trader.buy_market(s, qty, sig["exit_price"])
                    else:
                        ok, ex_price = trader.sell_market(s, qty, sig["exit_price"])

                    avg_entry = trader.avg_price.get(s, sig["exit_price"])
                    pnl_trade = trader.realized_trade_pnl(
                        "long" if side == "long" else "short",
                        s,
                        qty,
                        avg_entry,
                        ex_price,
                    )

                    msg = (
                        f"[{s}] EXIT {sig['signal'].upper()}\n"
                        f"Qty: {qty}\n"
                        f"Price: {ex_price:.2f}\n"
                        f"P&L: {pnl_trade:.2f}"
                    )
                    print(msg, "ok=", ok)
                    if notifier and ok:
                        notifier.send(msg)

            now = time.time()
            if now - last_pnl_ping >= pnl_ping_interval:
                pnl = trader.pnl(market_prices)
                pnl_msg = (
                    f"P&L UPDATE\n"
                    f"Cash: {pnl['cash']:.2f}\n"
                    f"Unrealized: {pnl['unrealized']:.2f}\n"
                    f"Total: {pnl['total']:.2f}"
                )
                print(
                    f"Pnl: cash={pnl['cash']:.2f} "
                    f"unreal={pnl['unrealized']:.2f} "
                    f"total={pnl['total']:.2f}"
                )
                if notifier:
                    notifier.send(pnl_msg)
                last_pnl_ping = now

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
