import os
import csv
import time
from datetime import datetime

import yaml

from strategy import FiveEMA
from paper_trader import PaperTrader
from telegram_notifier import TelegramNotifier


STARTING_CASH = 100000  # 1 lakh


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_year_data(data_dir, symbol, year):
    """
    Load 5-minute candles for one symbol and year from CSV.

    Expected file: data/<symbol>/<year>_5min.csv
    Columns: datetime, open, high, low, close, volume
    """
    path = os.path.join(data_dir, symbol, f"{year}_5min.csv")
    if not os.path.exists(path):
        return []

    rows = []
    with open(path, "r") as f:
        r = csv.DictReader(f)
        for row in r:
            dt_str = row["datetime"]
            # handle both "YYYY-MM-DD HH:MM:SS" and ISO with T
            if "T" in dt_str:
                dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
                dt = dt.replace(tzinfo=None)
            else:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            rows.append((dt, o, h, l, c))
    rows.sort(key=lambda x: x[0])
    return rows


def filter_first_n_months(candles, n_months):
    """
    Keep only first n_months of data in that calendar year.
    """
    if not candles:
        return candles
    first_year = candles[0][0].year
    max_month = min(12, n_months)
    out = [c for c in candles if c[0].year == first_year and c[0].month <= max_month]
    return out


def build_15m_from_5m(candles_5m):
    """
    Build synthetic 15-minute candles by grouping 3 consecutive 5m candles.
    """
    candles_15m = []
    bucket = []
    for dt, o, h, l, c in candles_5m:
        bucket.append((dt, o, h, l, c))
        if len(bucket) == 3:
            dt0, o0, h0, l0, c0 = bucket[0]
            _, _, h1, l1, _ = bucket[1]
            dt2, _, h2, l2, c2 = bucket[2]
            o15 = o0
            h15 = max(h0, h1, h2)
            l15 = min(l0, l1, l2)
            c15 = c2
            candles_15m.append((dt2, o15, h15, l15, c15))
            bucket = []
    return candles_15m


def main():
    cfg = load_config("config.yaml")
    symbols = cfg["symbols"]

    bt_cfg = cfg.get("backtest", {})
    data_dir = bt_cfg.get("data_dir", "data")

    # ---- CONTROL THESE TWO ----
    backtest_year = 2023        # year that exists in your CSVs
    months_to_run = 4           # run first 4 months of that year
    # ---------------------------

    # Backtest Telegram bot
    bt_tg_cfg = cfg.get("backtest_telegram", {})
    notifier = None
    if bt_tg_cfg.get("enable", False):
        notifier = TelegramNotifier(
            bot_token=bt_tg_cfg["bot_token"],
            chat_ids=bt_tg_cfg.get("chat_ids", []),
        )

    print(f"Backtest year: {backtest_year}, months: 1–{months_to_run}")
    if notifier:
        try:
            notifier.send(
                f"BACKTEST STARTED for {backtest_year}, months 1–{months_to_run}, capital={STARTING_CASH}"
            )
        except Exception as e:
            print("Backtest Telegram send error:", e)

    # Load 5m data and build 15m (only first N months)
    symbol_data_5m = {}
    symbol_data_15m = {}
    total_candles = 0
    for s in symbols:
        candles_5m_all = load_year_data(data_dir, s, backtest_year)
        candles_5m = filter_first_n_months(candles_5m_all, months_to_run)
        symbol_data_5m[s] = candles_5m
        candles_15m = build_15m_from_5m(candles_5m)
        symbol_data_15m[s] = candles_15m
        total_candles += len(candles_5m)
        print(f"[{s}] loaded {len(candles_5m)} candles for {backtest_year} (first {months_to_run} months)")

    if total_candles == 0:
        msg = f"No data for any symbol in year {backtest_year} (months 1–{months_to_run})"
        print(msg)
        if notifier:
            try:
                notifier.send(msg)
            except Exception as e:
                print("Backtest Telegram send error:", e)
        return

    # Pace: compress N months into one day session 09:00–16:00 (7 hours)
    session_seconds = 7 * 60 * 60
    sleep_per_candle = session_seconds / total_candles
    print(f"Total candles: {total_candles}, sleep_per_candle: {sleep_per_candle:.4f}s")

    # Set up traders and strategies per symbol
    traders = {
        s: PaperTrader(starting_cash=STARTING_CASH, slippage=cfg.get("slippage", 0.0))
        for s in symbols
    }
    strategies = {
        s: FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=10000)
        for s in symbols
    }
    market_prices = {s: None for s in symbols}

    # Merge all 5m candles across symbols into a single chronological stream
    events = []
    for s, candles in symbol_data_5m.items():
        for dt, o, h, l, c in candles:
            events.append((dt, s, o, h, l, c))
    events.sort(key=lambda x: x[0])

    # Index 15m candles by datetime for each symbol
    idx_15m = {}
    for s, candles in symbol_data_15m.items():
        idx_15m[s] = {dt: (o, h, l, c) for dt, o, h, l, c in candles}

    qty = cfg.get("quantity", 1)
    processed = 0
    wall_start = time.time()

    # Helper: compute trade P&L explicitly from trader state
    def compute_trade_pnl(trader, side, symbol, qty, entry_price, exit_price):
        """
        side: 'long' or 'short'
        PnL = (exit - entry) * qty for long
        PnL = (entry - exit) * qty for short
        """
        if side == "long":
            return (exit_price - entry_price) * qty
        else:
            return (entry_price - exit_price) * qty

    for dt, s, o, h, l, c in events:
        market_prices[s] = c

        # 5m update
        sig = strategies[s].update_candle(o, h, l, c, dt.timestamp(), tf_minutes=5)

        # 15m update if a 15m candle closes at this dt
        c15 = idx_15m[s].get(dt)
        if c15 is not None:
            o2, h2, l2, c2 = c15
            sig2 = strategies[s].update_candle(
                o2, h2, l2, c2, dt.timestamp(), tf_minutes=15
            )
            if sig2 is not None:
                sig = sig2

        if sig is not None:
            trader = traders[s]

            if sig["signal"] == "short_entry":
                ok, ex_price = trader.sell_market(s, qty, sig["entry"])
                msg = (
                    f"[{s}] SHORT ENTRY (BACKTEST)\n"
                    f"Time: {dt}\n"
                    f"Qty: {qty}\n"
                    f"Entry: {ex_price:.2f}\n"
                    f"SL: {sig['sl']:.2f}\n"
                    f"TP: {sig['tp']:.2f}"
                )
                print(msg, "ok=", ok)
                if notifier and ok:
                    try:
                        notifier.send(msg)
                    except Exception as e:
                        print("Backtest Telegram send error:", e)

            elif sig["signal"] == "long_entry":
                ok, ex_price = trader.buy_market(s, qty, sig["entry"])
                msg = (
                    f"[{s}] LONG ENTRY (BACKTEST)\n"
                    f"Time: {dt}\n"
                    f"Qty: {qty}\n"
                    f"Entry: {ex_price:.2f}\n"
                    f"SL: {sig['sl']:.2f}\n"
                    f"TP: {sig['tp']:.2f}"
                )
                print(msg, "ok=", ok)
                if notifier and ok:
                    try:
                        notifier.send(msg)
                    except Exception as e:
                        print("Backtest Telegram send error:", e)

            elif sig["signal"] in ("exit_sl", "exit_tp"):
                # Determine side from current position
                pos_qty = trader.positions.get(s, 0)
                side = "long" if pos_qty > 0 else "short"

                if side == "short":
                    ok, ex_price = trader.buy_market(s, abs(pos_qty), sig["exit_price"])
                else:
                    ok, ex_price = trader.sell_market(s, abs(pos_qty), sig["exit_price"])

                # Use stored avg_price as entry, fallback to signal if missing
                entry_price = trader.avg_price.get(s, sig["exit_price"])
                pnl_trade = compute_trade_pnl(
                    trader, side, s, abs(pos_qty), entry_price, ex_price if ok else sig["exit_price"]
                )

                msg = (
                    f"[{s}] EXIT {sig['signal'].upper()} (BACKTEST)\n"
                    f"Time: {dt}\n"
                    f"Side: {side.upper()}\n"
                    f"Qty: {abs(pos_qty)}\n"
                    f"Entry: {entry_price:.2f}\n"
                    f"Exit: {ex_price:.2f}\n"
                    f"Trade P&L: {pnl_trade:.2f}"
                )
                print(msg, "ok=", ok)
                if notifier and ok:
                    try:
                        notifier.send(msg)
                    except Exception as e:
                        print("Backtest Telegram send error:", e)

        processed += 1
        time.sleep(sleep_per_candle)

    # Summary
    elapsed = time.time() - wall_start
    if notifier:
        try:
            notifier.send(
                f"BACKTEST COMPLETED for {backtest_year}, months 1–{months_to_run} in {int(elapsed)} seconds"
            )
        except Exception as e:
            print("Backtest Telegram send error:", e)

    for s, trader in traders.items():
        pnl = trader.pnl(market_prices)
        print(f"{s} FINAL PNL (mark-to-market): {pnl:.2f}")


if __name__ == "__main__":
    main()
