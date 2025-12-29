import os
import csv
import time
from datetime import datetime

import yaml

from strategy import FiveEMA
from paper_trader import PaperTrader
from telegram_notifier import TelegramNotifier


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_year_data(data_dir, symbol, year):
    path = os.path.join(data_dir, symbol, f"{year}_5min.csv")
    if not os.path.exists(path):
        return []

    rows = []
    with open(path, "r") as f:
        r = csv.DictReader(f)
        for row in r:
            dt_str = row["datetime"]
            if "T" in dt_str:
                dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
            else:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            rows.append((dt, o, h, l, c))
    rows.sort(key=lambda x: x[0])
    return rows


def filter_month_range(candles, start_month, months_to_run):
    if not candles:
        return candles
    year = candles[0][0].year
    end_month = min(12, start_month + months_to_run - 1)
    return [c for c in candles if c[0].year == year and start_month <= c[0].month <= end_month]


def build_15m_from_5m(candles_5m):
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
    data_dir = bt_cfg.get("data_dir", cfg.get("data_dir", "data"))
    base_year = bt_cfg.get("base_year", 2018)
    months_to_run = bt_cfg.get("months_to_run", 4)
    starting_cash = cfg.get("starting_cash_backtest", 100000)
    risk_per_trade = cfg.get("risk_per_trade", 0.01)

    backtest_year = base_year
    start_month = 1

    tg_cfg = cfg.get("backtest_telegram", {})
    notifier = None
    if tg_cfg.get("enable", False):
        notifier = TelegramNotifier(
            bot_token=tg_cfg["bot_token"],
            chat_ids=tg_cfg.get("chat_ids", []),
        )

    # Load data for all symbols
    symbol_5m = {}
    symbol_15m = {}
    total_candles = 0

    for s in symbols:
        candles_5m_all = load_year_data(data_dir, s, backtest_year)
        candles_5m = filter_month_range(candles_5m_all, start_month, months_to_run)
        if candles_5m:
            symbol_5m[s] = candles_5m
            candles_15m = build_15m_from_5m(candles_5m)
            symbol_15m[s] = candles_15m
            total_candles += len(candles_5m)
            print(f"[{s}] {len(candles_5m)} candles loaded ✓")
        else:
            print(f"[{s}] NO DATA - skipping")

    if total_candles == 0:
        msg = f"[BACKTEST] No data for {backtest_year} months {start_month}-{start_month + months_to_run - 1}"
        print(msg)
        if notifier:
            notifier.send(msg)
        return

    session_seconds = 6 * 60 * 60
    sleep_per_candle = session_seconds / total_candles
    print(f"[BACKTEST] total_candles={total_candles}, sleep_per_candle={sleep_per_candle:.4f}s")

    if notifier:
        notifier.send(
            f"[BACKTEST] START {backtest_year} months {start_month}-"
            f"{start_month + months_to_run - 1}, capital=₹{starting_cash:,} per symbol"
        )

    traders = {
        s: PaperTrader(starting_cash=starting_cash, slippage=cfg.get("slippage", 0.0))
        for s in symbols
    }
    strat = FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=10000)
    market_prices = {s: None for s in symbols}

    # Combine all 5m candles across symbols into a single time-ordered list
    events = []
    for s, candles in symbol_5m.items():
        for dt, o, h, l, c in candles:
            events.append((dt, s, o, h, l, c))
    events.sort(key=lambda x: x[0])

    # 15m index per symbol
    idx_15m = {}
    for s, candles in symbol_15m.items():
        idx_15m[s] = {dt: (o, h, l, c) for dt, o, h, l, c in candles}

    # P&L tracking per symbol and month
    monthly_pnl = {s: {} for s in symbols}
    current_month = None

    # track entry messages per (symbol, trade_id)
    open_trades = {}  # (symbol, trade_id) -> info

    wall_start = time.time()

    for dt, s, o, h, l, c in events:
        market_prices[s] = c
        if current_month is None:
            current_month = dt.month

        # 5m update (short logic) - DEBUG
        sig_5 = strat.update_candle(s, o, h, l, c, dt.timestamp(), tf_minutes=5)
        if sig_5:
            sig_5 = {k: v for k, v in sig_5.items() if k != "symbol"}
            print(f"🚨 [{s}] 5m SIGNAL: {sig_5['signal']} | EMA: {strat.state[s]['ema_short']:.1f} | C: {c:.1f}")

        # 15m update (long logic) - DEBUG
        sig_15 = None
        c15 = idx_15m[s].get(dt)
        if c15 is not None:
            o2, h2, l2, c2 = c15
            sig_15 = strat.update_candle(s, o2, h2, l2, c2, dt.timestamp(), tf_minutes=15)
            if sig_15:
                sig_15 = {k: v for k, v in sig_15.items() if k != "symbol"}
                print(f"🚨 [{s}] 15m SIGNAL: {sig_15['signal']} | EMA: {strat.state[s]['ema_long']:.1f} | C: {c2:.1f}")

        signal = sig_15 or sig_5
        if signal:
            print(f"🎯 [{s}] FINAL SIGNAL: {signal['signal']} | Entry:{signal['entry']:.1f}")

        st = strat.state[s]
        trader = traders[s]

        # Month boundary summary when month changes
        if dt.month != current_month:
            prev_month = current_month
            for sym in symbols:
                pnl_m = monthly_pnl[sym].get(prev_month, 0.0)
                if pnl_m == 0 and prev_month not in monthly_pnl[sym]:
                    continue
                msg = (
                    f"[BACKTEST] {sym} {backtest_year}-{prev_month:02d} summary\n"
                    f"Realized P&L: ₹{pnl_m:,.2f}"
                )
                print(msg)
                if notifier:
                    notifier.send(msg)
            current_month = dt.month

        # Handle entries
        if signal and signal["signal"] in ("long_entry", "short_entry"):
            if st["position"] is None:
                entry = signal["entry"]
                sl = signal["sl"]
                tp = signal["tp"]
                side_new = "long" if signal["signal"] == "long_entry" else "short"

                risk = abs(entry - sl)
                if risk <= 0:
                    continue
                risk_amount = trader.cash * risk_per_trade
                qty = int(risk_amount / risk)
                if qty <= 0:
                    continue

                if side_new == "long":
                    ok, ex_price = trader.buy_market(s, qty, entry)
                else:
                    ok, ex_price = trader.sell_market(s, qty, entry)

                if ok:
                    trade_id = signal["trade_id"]
                    st["position"] = {
                        "side": side_new,
                        "entry": ex_price,
                        "sl": sl,
                        "tp": tp,
                        "trade_id": trade_id,
                    }
                    text = (
                        f"📈 [BT ENTRY] {s} #{trade_id}\n"
                        f"Side: {side_new.upper()}\n"
                        f"Time: {dt}\n"
                        f"Qty: {qty}\n"
                        f"Entry: ₹{ex_price:,.2f}\n"
                        f"SL: ₹{sl:,.2f}\n"
                        f"TP: ₹{tp:,.2f}"
                    )
                    print(text)
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

        # Handle exits (SL/TP) using current price
        exit_sig = strat.exit_signal(s, c)
        if exit_sig and exit_sig.get("signal"):
            exit_sig = {k: v for k, v in exit_sig.items() if k != "symbol"}
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
                month_key = dt.month
                monthly_pnl[s][month_key] = monthly_pnl[s].get(month_key, 0.0) + pnl_trade

                equity = trader.equity(market_prices)
                text = (
                    f"📉 [BT EXIT] {s} #{trade_id} {exit_sig['signal'].upper()}\n"
                    f"Side: {side.upper()}\n"
                    f"Time: {dt}\n"
                    f"Qty: {qty}\n"
                    f"Entry: ₹{entry_price:,.2f}\n"
                    f"Exit: ₹{actual_exit:,.2f}\n"
                    f"Trade P&L: ₹{pnl_trade:,.2f}\n"
                    f"Symbol Equity: ₹{equity:,.2f}"
                )
                print(text)
                reply_id = None
                if info["entry_msg_ids"]:
                    reply_id = next(iter(info["entry_msg_ids"].values()))
                if notifier:
                    notifier.send(text, reply_to_message_id=reply_id)
                del open_trades[(s, trade_id)]

        time.sleep(sleep_per_candle)

    # Final month summaries
    if current_month is not None:
        last_month = current_month
        for sym in symbols:
            pnl_m = monthly_pnl[sym].get(last_month, 0.0)
            if last_month in monthly_pnl[sym]:
                msg = (
                    f"[BACKTEST] {sym} {backtest_year}-{last_month:02d} summary\n"
                    f"Realized P&L: ₹{pnl_m:,.2f}"
                )
                print(msg)
                if notifier:
                    notifier.send(msg)

    # 4-month consolidated summary per symbol
    for sym in symbols:
        total_sym_pnl = sum(monthly_pnl[sym].values())
        equity = traders[sym].equity(market_prices) if sym in traders else starting_cash
        msg = (
            f"[BACKTEST] {sym} FINAL {backtest_year} M{start_month}-{start_month + months_to_run - 1}\n"
            f"P&L: ₹{total_sym_pnl:,.2f} | Equity: ₹{equity:,.2f}"
        )
        print(msg)
        if notifier:
            notifier.send(msg)

    elapsed = time.time() - wall_start
    if notifier:
        notifier.send(
            f"[BACKTEST] ✅ COMPLETED {backtest_year} M{start_month}-"
            f"{start_month + months_to_run - 1} in {int(elapsed/60)}min"
        )


if __name__ == "__main__":
    main()
