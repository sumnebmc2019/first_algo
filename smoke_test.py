import time

from strategy import FiveEMA
from paper_trader import PaperTrader
from data_feed import SimulatedFeed


def smoke_run(iterations=50, bar_seconds=5):
    """
    Quick local smoke test using SimulatedFeed and 5-EMA short-only strategy.

    - Uses a very short 'bar_seconds' so you don't wait 5 real minutes per bar.
    - Builds mini-candles and feeds them into FiveEMA.update_candle.
    """

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
                print("  SHORT executed:", ok, res)
            elif sig and sig["signal"] in ("exit_sl", "exit_tp"):
                ok, res = trader.buy_market("TEST", 1, sig["exit_price"])
                print("  EXIT executed:", ok, res)

        time.sleep(0.2)

    print("\nFinal PnL:", trader.pnl(market_prices))
    print("Trade log:")
    for t in trader.trade_log:
        print(" ", t)


if __name__ == "__main__":
    smoke_run(50, bar_seconds=5)
