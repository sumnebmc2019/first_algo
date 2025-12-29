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
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            rows.append((dt, o, h, l, c))
    return sorted(rows, key=lambda x: x[0])


def filter_month_range(candles, year, start_month, months_to_run):
    if not candles:
        return []
    end_month = min(12, start_month + months_to_run - 1)
    return [c for c in candles if c[0].year == year and start_month <= c[0].month <= end_month]


def build_15m_from_5m(candles_5m):
    candles_15m = []
    i = 0
    while i + 2 < len(candles_5m):
        o = candles_5m[i][1]
        h = max(c[2] for c in candles_5m[i:i+3])
        l = min(c[3] for c in candles_5m[i:i+3])
        c = candles_5m[i+2][4]
        dt = candles_5m[i+2][0]
        candles_15m.append((dt, o, h, l, c))
        i += 3
    return candles_15m


def main():
    cfg = load_config()
    symbols = cfg["symbols"]
    bt_cfg = cfg.get("backtest", {})
    data_dir = bt_cfg.get("data_dir", "data")
    year = bt_cfg.get("base_year", 2018)
    months = bt_cfg.get("months_to_run", 4)
    starting_cash = cfg.get("starting_cash_backtest", 100000)
    risk_pct = cfg.get("risk_per_trade", 0.01)

    notifier = TelegramNotifier(
        bot_token=cfg["backtest_telegram"]["bot_token"],
        chat_ids=cfg["backtest_telegram"]["chat_ids"]
    )

    # Load data for all symbols
    symbol_data_5m = {}
    symbol_data_15m = {}
    total_candles = 0
    
    for s in symbols:
        candles_5m = load_year_data(data_dir, s, year)
        filtered_5m = filter_month_range(candles_5m, year, 1, months)
        symbol_data_5m[s] = filtered_5m
        symbol_data_15m[s] = build_15m_from_5m(filtered_5m)
        total_candles += len(filtered_5m)
        print(f"[{s}] {len(filtered_5m)} candles loaded")

    if total_candles == 0:
        notifier.send("BACKTEST: No data available")
        return

    # Compress into 6 hours (6am-12pm)
    session_duration = 6 * 3600
    sleep_per_candle = session_duration / total_candles

    notifier.send(f"BACKTEST START {year} M1-{months} | {total_candles} candles | {sleep_per_candle:.2f}s/candle")

    # Initialize per symbol
    traders = {s: PaperTrader(starting_cash, 0.0) for s in symbols}
    strategy = FiveEMA()
    market_prices = {s: 0 for s in symbols}
    
    # Create unified event stream
    events = []
    for s, candles in symbol_data_5m.items():
        for candle in candles:
            events.append((candle[0], s, *candle[1:]))  # (dt, symbol, o,h,l,c)
    events.sort(key=lambda x: x[0])

    # 15m lookup
    idx_15m = {}
    for s, candles in symbol_data_15m.items():
        idx_15m[s] = {candle[0]: candle[1:] for candle in candles}

    # Trade tracking
    open_trades = {}
    monthly_pnl = {s: {} for s in symbols}
    current_month = 1

    start_time = time.time()
    for event in events:
        dt, s, o, h, l, c = event
        market_prices[s] = c

        # Monthly summary
        if dt.month != current_month:
            for sym in symbols:
                pnl = sum(monthly_pnl[sym].values())
                if pnl != 0:
                    notifier.send(f"[{sym}] M{current_month} P&L: ₹{pnl:,.0f}")
            current_month = dt.month

        # Strategy updates
        sig_5m = strategy.update_candle(s, o, h, l, c, dt.timestamp(), 5)
        sig_15m = None
        if dt in idx_15m.get(s, {}):
            o15, h15, l15, c15 = idx_15m[s][dt]
            sig_15m = strategy.update_candle(s, o15, h15, l15, c15, dt.timestamp(), 15)
        sig = sig_15m or sig_5m

        trader = traders[s]
        st = strategy.state[s]

        # ENTRY
        if sig and sig["signal"] in ("long_entry", "short_entry"):
            if st.get("position"):
                continue
                
            entry, sl, tp = sig["entry"], sig["sl"], sig["tp"]
            side = "long" if sig["signal"] == "long_entry" else "short"
            risk = abs(entry - sl)
            
            if risk > 0:
                qty = max(1, int((trader.cash * risk_pct) / risk))
                ok, fill_price = trader.buy_market(s, qty, entry) if side == "long" else trader.sell_market(s, qty, entry)
                
                if ok:
                    trade_id = sig["trade_id"]
                    st["position"] = {"side": side, "entry": fill_price, "sl": sl, "tp": tp, "trade_id": trade_id}
                    
                    msg = (
                        f"[BT ENTRY] {s} #{trade_id}\n"
                        f"{side.upper()} {qty}@{fill_price:.1f}\n"
                        f"SL:{sl:.1f} TP:{tp:.1f}"
                    )
                    entry_msgs = notifier.send(msg)
                    open_trades[(s, trade_id)] = {
                        "side": side, "qty": qty, "entry": fill_price,
                        "entry_msgs": entry_msgs
                    }

        # EXIT
        exit_sig = strategy.exit_signal(s, c)
        if exit_sig:
            trade_id = exit_sig["trade_id"]
            trade_info = open_trades.pop((s, trade_id), None)
            if trade_info:
                side, qty, entry_price = trade_info["side"], trade_info["qty"], trade_info["entry"]
                exit_price = exit_sig["exit_price"]
                
                ok, fill_price = (
                    trader.buy_market(s, qty, exit_price) if side == "short" 
                    else trader.sell_market(s, qty, exit_price)
                )
                
                actual_exit = fill_price if ok else exit_price
                pnl = trader.record_realized_trade_pnl(s, side, qty, entry_price, actual_exit)
                monthly_pnl[s][dt.month] = monthly_pnl[s].get(dt.month, 0) + pnl
                
                equity = trader.equity(market_prices)
                msg = (
                    f"[BT EXIT] {s} #{trade_id} {exit_sig['signal']}\n"
                    f"{entry_price:.1f}→{actual_exit:.1f} | ₹{pnl:,.0f}\n"
                    f"Symbol Equity: ₹{equity:,.0f}"
                )
                reply_id = next(iter(trade_info["entry_msgs"].values()), None)
                notifier.send(msg, reply_to_message_id=reply_id)

        time.sleep(sleep_per_candle)

    # Final summaries
    for s in symbols:
        total_pnl = sum(monthly_pnl[s].values())
        equity = traders[s].equity(market_prices)
        notifier.send(f"[BT FINAL] {s}: ₹{total_pnl:,.0f} | Equity: ₹{equity:,.0f}")
    
    elapsed = time.time() - start_time
    notifier.send(f"BACKTEST COMPLETE | {elapsed/60:.0f}min | {len(events)} candles")


if __name__ == "__main__":
    main()
