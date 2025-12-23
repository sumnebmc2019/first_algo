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
            # start new candle
            self.current[symbol] = {
                "start": ts,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
            }
            return None

        # same candle window or new?
        if ts - cndl["start"] < self.bar_seconds:
            # update existing candle
            cndl["h"] = max(cndl["h"], price)
            cndl["l"] = min(cndl["l"], price)
            cndl["c"] = price
            return None
        else:
            # close old candle and start a new one
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

    # Paper trader only (no live orders)
    trader = PaperTrader(starting_cash=starting, slippage=slippage)
    strategy = {s: FiveEMA(ema_period=5, rr=1.5) for s in symbols}

    # Choose feed: SmartAPI (live NIFTY LTP) or Simulated
    sa_cfg = cfg.get("smartapi", {})
    use_smartapi = sa_cfg.get("enable", False)

    if use_smartapi:
        conn = SmartAPIConnector(
            api_key=sa_cfg.get("api_key"),
            client_id=sa_cfg.get("client_id"),
            password=sa_cfg.get("password"),
            totp=sa_cfg.get("totp"),
            exchange=sa_cfg.get("exchange"),
            tradingsymbol=sa_cfg.get("tradingsymbol"),
            symboltoken=sa_cfg.get("symboltoken"),
        )
    else:
        conn = SimulatedFeed()

    # Optional Telegram notifier
    tg_cfg = cfg.get("telegram", {})
    use_telegram = tg_cfg.get("enable", False)
    notifier = None
    if use_telegram:
        notifier = TelegramNotifier(
            bot_token=tg_cfg.get("bot_token"),
            chat_id=tg_cfg.get("chat_id"),
        )

    print(f"Starting bot in {mode} mode for symbols: {symbols}")
    market_prices = {s: None for s in symbols}

    candle_builder = CandleBuilder(bar_seconds=300)  # 5 minutes

    try:
        while True:
            for s in symbols:
                tick = conn.get_price(s)
                price = tick["price"]
                ts = tick["time"]
                market_prices[s] = price

                completed = candle_builder.update(s, price, ts)
                if completed is not None:
                    o, h, l, c = completed
                    sig = strategy[s].update_candle(o, h, l, c)

                    if sig is None:
                        continue

                    if sig["signal"] == "short_entry":
                        ok, res = trader.sell_market(s, qty, sig["entry"])
                        msg = (
                            f"[{s}] SHORT entry\n"
                            f"Qty: {qty}\n"
                            f"Entry: {sig['entry']:.2f}\n"
                            f"SL: {sig['sl']:.2f}\n"
                            f"TP: {sig['tp']:.2f}"
                        )
                        print(msg, "ok=", ok)
                        if notifier and ok:
                            notifier.send(msg)

                    elif sig["signal"] in ("exit_sl", "exit_tp"):
                        ok, res = trader.buy_market(s, qty, sig["exit_price"])
                        msg = (
                            f"[{s}] EXIT {sig['signal']}\n"
                            f"Qty: {qty}\n"
                            f"Price: {sig['exit_price']:.2f}"
                        )
                        print(msg, "ok=", ok)
                        if notifier and ok:
                            notifier.send(msg)

            pnl = trader.pnl(market_prices)
            print(
                f"Pnl: cash={pnl['cash']:.2f} "
                f"unreal={pnl['unrealized']:.2f} "
                f"total={pnl['total']:.2f}"
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Stopped by user. Trades:")
        for t in trader.trade_log:
            print(t)


if __name__ == "__main__":
    main()
