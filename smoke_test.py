import time
import yaml

from strategy import FiveEMA
from paper_trader import PaperTrader
from data_feed import SimulatedFeed
from telegram_notifier import TelegramNotifier


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def smoke_run(iterations=50, bar_seconds=5):
    """
    Quick local smoke test using SimulatedFeed and 5-EMA short-only strategy.

    - Uses a very short 'bar_seconds' so you don't wait 5 real minutes per bar.
    - Builds mini-candles and feeds them into FiveEMA.update_candle.
    - Sends TEST trade messages to Telegram if enabled.
    """

    cfg = load_config("config.yaml")
    tg_cfg = cfg.get("telegram", {})
    use_telegram = tg_cfg.get("enable", False)

    notifier = None
    if use_telegram:
        notifier = TelegramNotifier(
            bot_token=tg_cfg.get("bot_token"),
            chat_id=tg_cfg.get("chat_id"),
        )

    feed = SimulatedFeed(start_price=100.0, volatility=0.5)
    trader = PaperTrader(starting_cash=10000, slippage=0.0)
    strat = FiveEMA(ema_period=5, rr=1.5)
    market_prices = {}

    # simple candle builder for the test
    current = None

    def update_candle(price, ts):
        nonlocal current
        if current is None:
            current = {"start": ts, "o": price, "h": price, "l": price, "c": price}
            return None

        if ts - current["start"] < bar_seconds:
            current["h"] = max(current["h"], price)
            current["l"] = min(current["l"], price)
            current["c"] = price
            return None
        else:
            completed = (current["o"], current["h"], current["l"], current["c"])
            current = {
                "start": ts,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return completed

    for i in range(iterations):
        tick = feed.get_price("TEST")
        price = tick["price"]
        ts = tick["time"]
        market_prices["TEST"] = price

        cndl = update_candle(price, ts)
        if cndl is not None:
            o, h, l, c = cndl
            sig = strat.update_candle(o, h, l, c)
            print(f"bar_close price={c:.2f} signal={sig}")

            if sig and sig["signal"] == "short_entry":
                ok, res = trader.sell_market("TEST", 1, sig["entry"])
                msg = (
                    "TEST: SHORT entry\n"
                    f"Symbol: TEST\n"
                    f"Qty: 1\n"
                    f"Entry: {sig['entry']:.2f}\n"
                    f"SL: {sig['sl']:.2f}\n"
                    f"TP: {sig['tp']:.2f}"
                )
                print("  SHORT executed:", ok, res)
                if notifier and ok:
                    notifier.send(msg)

            elif sig and sig["signal"] in ("exit_sl", "exit_tp"):
                ok, res = trader.buy_market("TEST", 1, sig["exit_price"])
                msg = (
                    "TEST: EXIT " + sig["signal"] + "\n"
                    f"Symbol: TEST\n"
                    f"Qty: 1\n"
                    f"Price: {sig['exit_price']:.2f}"
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
    smoke_run(50, bar_seconds=5)
