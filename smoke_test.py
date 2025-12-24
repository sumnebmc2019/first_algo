import time
import yaml

from strategy import FiveEMA
from paper_trader import PaperTrader
from data_feed import SimulatedFeed
from telegram_notifier import TelegramNotifier


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def smoke_run(iterations=200, bar_seconds=5):
    """
    Quick local smoke test using SimulatedFeed and 5-EMA long+short strategy.

    - Uses short 'bar_seconds' so you don't wait real 5/15 minutes.
    - Builds mini 5s-candles and feeds them as 5m + 15m equivalents.
    - Sends TEST messages to all Telegram chat_ids if enabled.
    """

    cfg = load_config("config.yaml")
    tg_cfg = cfg.get("telegram", {})
    use_telegram = tg_cfg.get("enable", False)

    notifier = None
    if use_telegram:
        chat_ids = tg_cfg.get("chat_ids") or tg_cfg.get("chat_id")
        notifier = TelegramNotifier(
            bot_token=tg_cfg.get("bot_token"),
            chat_ids=chat_ids,
        )

    # use two test symbols to mimic NIFTY / BANKNIFTY
    symbols = ["NIFTY_TEST", "BANKNIFTY_TEST"]
    feed = SimulatedFeed(start_price=100.0, volatility=0.5)
    trader = PaperTrader(starting_cash=100000, slippage=0.0)
    strategies = {s: FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=5) for s in symbols}
    market_prices = {s: None for s in symbols}

    # simple per-symbol candle builders for test
    current_5s = {s: None for s in symbols}

    def update_candle(symbol, price, ts):
        """
        Build pseudo-candles of length `bar_seconds` for each symbol
        (used as both 5m and 15m candles in this smoke test).
        """
        cndl = current_5s[symbol]
        if cndl is None:
            current_5s[symbol] = {
                "start": ts,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return None

        if ts - cndl["start"] < bar_seconds:
            cndl["h"] = max(cndl["h"], price)
            cndl["l"] = min(cndl["l"], price)
            cndl["c"] = price
            return None
        else:
            completed = (cndl["o"], cndl["h"], cndl["l"], cndl["c"])
            current_5s[symbol] = {
                "start": ts,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return completed

    for i in range(iterations):
        for s in symbols:
            tick = feed.get_price(s)
            price = tick["price"]
            ts = tick["time"]
            market_prices[s] = price

            cndl = update_candle(s, price, ts)
            if cndl is None:
                continue

            o, h, l, c = cndl

            # feed once as "5m" candle
            sig = strategies[s].update_candle(o, h, l, c, ts, tf_minutes=5)
            print(f"[{s}] bar_close 5m price={c:.2f} signal={sig}")

            # feed again as "15m" candle to exercise long logic
            sig2 = strategies[s].update_candle(o, h, l, c, ts, tf_minutes=15)
            if sig2 is not None:
                sig = sig2
                print(f"[{s}] bar_close 15m price={c:.2f} signal={sig}")

            if sig is None:
                continue

            if sig["signal"] == "short_entry":
                ok, res = trader.sell_market(s, 1, sig["entry"])
                msg = (
                    f"TEST: SHORT entry\n"
                    f"Symbol: {s}\n"
                    f"Qty: 1\n"
                    f"Entry: {sig['entry']:.2f}\n"
                    f"SL: {sig['sl']:.2f}\n"
                    f"TP: {sig['tp']:.2f}"
                )
                print("  SHORT executed:", ok, res)
                if notifier and ok:
                    notifier.send(msg)

            elif sig["signal"] == "long_entry":
                ok, res = trader.buy_market(s, 1, sig["entry"])
                msg = (
                    f"TEST: LONG entry\n"
                    f"Symbol: {s}\n"
                    f"Qty: 1\n"
                    f"Entry: {sig['entry']:.2f}\n"
                    f"SL: {sig['sl']:.2f}\n"
                    f"TP: {sig['tp']:.2f}"
                )
                print("  LONG executed:", ok, res)
                if notifier and ok:
                    notifier.send(msg)

            elif sig["signal"] in ("exit_sl", "exit_tp"):
                pos_qty = trader.positions.get(s, 0)
                side = "long" if pos_qty > 0 else "short"

                if side == "short":
                    ok, res = trader.buy_market(s, 1, sig["exit_price"])
                else:
                    ok, res = trader.sell_market(s, 1, sig["exit_price"])

                avg_entry = trader.avg_price.get(s, sig["exit_price"])
                from_side = "long" if side == "long" else "short"
                pnl_trade = trader.realized_trade_pnl(
                    from_side, s, 1, avg_entry, res if ok else sig["exit_price"]
                )

                msg = (
                    f"TEST: EXIT {sig['signal']}\n"
                    f"Symbol: {s}\n"
                    f"Qty: 1\n"
                    f"Price: {sig['exit_price']:.2f}\n"
                    f"P&L: {pnl_trade:.2f}"
                )
                print("  EXIT executed:", ok, res)
                if notifier and ok:
                    notifier.send(msg)

        time.sleep(0.2)

    print("\nFinal PnL:", trader.pnl(market_prices))
    print("Trade log:")
    for t in trader.trade_log:
        print(" ", t)


if __name__ == "__main__":
    smoke_run(200, bar_seconds=5)
