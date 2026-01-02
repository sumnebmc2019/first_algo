import os
import csv
import time
from datetime import datetime

import yaml
import traceback

from strategy import FiveEMA
from paper_trader import PaperTrader
from telegram_notifier import TelegramNotifier


def load_config(path: str = "config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_year_data(data_dir: str, symbol: str, year: int):
    path = os.path.join(data_dir, symbol, f"{year}_5min.csv")
    if not os.path.exists(path):
        print(f"[DEBUG] Data file not found for {symbol}: {path}")
        return []

    rows = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row["datetime"]
            if "T" in dt_str:
                dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z").replace(
                    tzinfo=None
                )
            else:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            rows.append((dt, o, h, l, c))
    rows.sort(key=lambda x: x[0])
    return rows


def filter_month_range(candles, start_month: int, months_to_run: int):
    if not candles:
        return candles
    year = candles[0][0].year
    end_month = min(12, start_month + months_to_run - 1)
    filtered = [
        c
        for c in candles
        if c[0].year == year and start_month <= c[0].month <= end_month
    ]
    print(
        f"[DEBUG] filter_month_range: year={year} start={start_month} "
        f"months={months_to_run} -> {len(filtered)} candles"
    )
    return filtered


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
    print(f"[DEBUG] build_15m_from_5m: {len(candles_5m)} -> {len(candles_15m)} candles")
    return candles_15m


def load_capital_state(path: str = "capital_state.yaml"):
    if not os.path.exists(path):
        print("[DEBUG] capital_state.yaml not found, starting fresh")
        return {}
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            print("[DEBUG] capital_state.yaml invalid format, ignoring")
            return {}
        print(f"[DEBUG] Loaded capital_state.yaml: {data}")
        return data
    except Exception as e:
        print("Failed to load capital_state.yaml:", e)
        print(traceback.format_exc())
        return {}


def save_capital_state(state: dict, path: str = "capital_state.yaml"):
    try:
        with open(path, "w") as f:
            yaml.safe_dump(state, f)
        print(f"[DEBUG] Saved capital_state.yaml: {state}")
    except Exception as e:
        print("Failed to save capital_state:", e)
        print(traceback.format_exc())


def safe_send_telegram(
    notifier,
    text,
    reply_to_message_id=None,
    tag="GENERIC",
    reply_map=None,  # dict {chat_id: msg_id} for per-chat replies
):
    """
    Wrapper to log every Telegram attempt and error per chat.

    - If reply_map is provided, it must be {chat_id: reply_to_msg_id} and
      overrides reply_to_message_id per chat.
    """
    if notifier is None:
        print(f"[DEBUG][TG][{tag}] Notifier is None, skipping Telegram send")
        return {}

    print(f"[DEBUG][TG][{tag}] SENDING -> reply_to={reply_to_message_id}")
    results = {}
    for chat_id in notifier.chat_ids:
        # choose per-chat reply id if map given, otherwise global one
        if reply_map is not None:
            chat_reply_id = reply_map.get(chat_id)
        else:
            chat_reply_id = reply_to_message_id

        res = notifier.send_to_chat(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=chat_reply_id,
            parse_mode="HTML",
        )
        results[chat_id] = res
    print(f"[DEBUG][TG][{tag}] RESULT -> {results}")
    return results


def main():
    print("[DEBUG] backtest.py main() starting")

    cfg = load_config("config.yaml")
    print(f"[DEBUG] Loaded config keys: {list(cfg.keys())}")

    symbols = cfg["symbols"]
    bt_cfg = cfg.get("backtest", {})
    data_dir = bt_cfg.get("data_dir", cfg.get("data_dir", "data"))
    base_year = bt_cfg.get("base_year", 2018)
    months_to_run = bt_cfg.get("months_to_run", 4)
    starting_cash_default = cfg.get("starting_cash_backtest", 100000)
    risk_per_trade = cfg.get("risk_per_trade", 0.01)

    backtest_year = base_year
    start_month = 1

    print(
        f"[DEBUG] BACKTEST PARAMS -> year={backtest_year}, "
        f"months={start_month}-{start_month + months_to_run - 1}, "
        f"risk_per_trade={risk_per_trade}"
    )

    # -------- TELEGRAM BACKTEST BOT --------
    tg_cfg = cfg.get("backtest_telegram", {})
    notifier = None
    if tg_cfg.get("enable", False):
        print("[DEBUG] backtest_telegram enabled")
        notifier = TelegramNotifier(
            bot_token=tg_cfg["bot_token"],
            chat_ids=tg_cfg.get("chat_ids", []),
        )
        print(
            f"[DEBUG] TelegramNotifier created for backtest_telegram, "
            f"chat_ids={tg_cfg.get('chat_ids', [])}"
        )
    else:
        print("[DEBUG] backtest_telegram disabled in config")

    # -------- CAPITAL CARRY-OVER --------
    cap_state_path = os.path.join(os.getcwd(), "capital_state.yaml")
    cap_state = load_capital_state(cap_state_path)

    # -------- LOAD DATA --------
    symbol_5m = {}
    symbol_15m = {}
    total_candles = 0

    for s in symbols:
        print(f"[DEBUG] Loading data for symbol={s}")
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
        msg = (
            f"[BACKTEST] No data for {backtest_year} "
            f"months {start_month}-{start_month + months_to_run - 1}"
        )
        print(msg)
        safe_send_telegram(notifier, msg, tag="NO_DATA")
        return

    active_symbols = list(symbol_5m.keys())
    print(f"[DEBUG] Active symbols with data: {active_symbols}")

    session_seconds = 6 * 60 * 60
    sleep_per_candle = session_seconds / total_candles
    print(
        f"[BACKTEST] total_candles={total_candles}, "
        f"sleep_per_candle={sleep_per_candle:.4f}s"
    )

    safe_send_telegram(
        notifier,
        "📊 <b>BACKTEST START</b>\n"
        f"<b>Year:</b> {backtest_year}\n"
        f"<b>Months:</b> {start_month}–{start_month + months_to_run - 1}\n"
        f"<b>Starting Capital:</b> ₹{starting_cash_default:,} per symbol",
        tag="START",
    )

    # -------- PER-SYMBOL TRADERS --------
    traders = {}
    for s in active_symbols:
        starting_cash_symbol = cap_state.get(s, starting_cash_default)
        traders[s] = PaperTrader(
            starting_cash=starting_cash_symbol,
            slippage=cfg.get("slippage", 0.0),
        )
        print(f"[BACKTEST] {s} starting capital: ₹{starting_cash_symbol:,.2f}")

    strat = FiveEMA(ema_period=5, rr=3.0, max_trades_per_day=10000)
    market_prices = {s: None for s in active_symbols}

    # all 5m candles as events
    events = []
    for s, candles in symbol_5m.items():
        for dt, o, h, l, c in candles:
            events.append((dt, s, o, h, l, c))
    events.sort(key=lambda x: x[0])
    print(f"[DEBUG] Total merged events: {len(events)}")

    # 15m lookup
    idx_15m = {}
    for s, candles in symbol_15m.items():
        idx_15m[s] = {dt: (o, h, l, c) for dt, o, h, l, c in candles}
        print(f"[DEBUG] 15m index for {s}: {len(idx_15m[s])} keys")

    # P&L tracking
    monthly_pnl = {s: {} for s in active_symbols}
    last_month_seen = {s: None for s in active_symbols}
    month_start_capital = {s: {} for s in active_symbols}

    # entry messages per trade
    open_trades = {}  # (symbol, trade_id) -> {qty, entry, entry_msg_ids, ...}

    # debug counters
    debug_stats = {
        "entry_signals": 0,
        "entries_executed": 0,
        "exit_signals": 0,
        "exits_executed": 0,
        "exit_skipped_no_position": 0,
        "exit_skipped_mismatch": 0,
        "tg_sends": 0,
        "tg_errors": 0,
    }

    wall_start = time.time()

    for idx, (dt, s, o, h, l, c) in enumerate(events):
        market_prices[s] = c
        trader = traders[s]

        # small progress heartbeat
        if idx % 5000 == 0:
            print(
                f"[DEBUG] Event {idx}/{len(events)} at {dt} symbol={s} "
                f"price={c:.2f}"
            )

        # ----- MONTH ROLLOVER -----
        mon = dt.month
        if last_month_seen[s] is None:
            last_month_seen[s] = mon
            month_start_capital[s][mon] = trader.equity(market_prices)
        elif mon != last_month_seen[s]:
            prev_month = last_month_seen[s]
            pnl_m = monthly_pnl[s].get(prev_month, 0.0)
            start_cap = month_start_capital[s].get(prev_month, trader.starting_cash)
            end_cap = trader.equity(market_prices)
            msg = (
                "📆 <b>Monthly P&L</b>\n"
                f"<b>Symbol:</b> {s}\n"
                f"<b>Period:</b> {backtest_year}-{prev_month:02d}\n"
                f"<b>Start Capital:</b> ₹{start_cap:,.2f}\n"
                f"<b>Realized P&L:</b> ₹{pnl_m:,.2f}\n"
                f"<b>End Capital:</b> ₹{end_cap:,.2f}"
            )
            print(msg)
            safe_send_telegram(notifier, msg, tag="MONTHLY")
            last_month_seen[s] = mon
            month_start_capital[s][mon] = trader.equity(market_prices)

        # ----- 5m + 15m SIGNALS -----
        sig_5 = strat.update_candle(s, o, h, l, c, dt.timestamp(), tf_minutes=5)
        if sig_5:
            sig_5 = {k: v for k, v in sig_5.items() if k != "symbol"}

        sig_15 = None
        c15 = idx_15m[s].get(dt)
        if c15 is not None:
            o2, h2, l2, c2 = c15
            sig_15 = strat.update_candle(s, o2, h2, l2, c2, dt.timestamp(), tf_minutes=15)
            if sig_15:
                sig_15 = {k: v for k, v in sig_15.items() if k != "symbol"}

        signal = sig_15 or sig_5
        st = strat.state[s]

        # ----- ENTRY (FiveEMA owns position) -----
        if signal and signal["signal"] in ("long_entry", "short_entry"):
            debug_stats["entry_signals"] += 1
            print(f"[DEBUG] ENTRY_SIGNAL {dt} {s} -> {signal}")

            pos = st["position"]
            if not pos or pos.get("trade_id") != signal["trade_id"]:
                print(
                    f"[DEBUG] WARNING: strategy entry but no matching position "
                    f"{dt} {s} state_pos={st['position']}"
                )
                continue

            entry = signal["entry"]
            sl = signal["sl"]
            tp = signal["tp"]
            side_new = "long" if signal["signal"] == "long_entry" else "short"

            risk = abs(entry - sl)
            qty = 0
            if risk > 0:
                current_equity = trader.equity(market_prices)
                risk_amount = current_equity * risk_per_trade
                qty = int(risk_amount / risk)
            else:
                print(
                    f"[DEBUG] SKIP entry (zero/neg risk) {dt} {s} "
                    f"entry={entry} sl={sl}"
                )

            if qty > 0:
                if side_new == "long":
                    ok, ex_price = trader.buy_market(s, qty, entry)
                else:
                    ok, ex_price = trader.sell_market(s, qty, entry)

                if ok:
                    debug_stats["entries_executed"] += 1
                    trade_id = signal["trade_id"]

                    text = (
                        "📈 <b>BT ENTRY</b>\n"
                        f"<b>Symbol:</b> {s}\n"
                        f"<b>Trade ID:</b> #{trade_id}\n"
                        f"<b>Side:</b> {side_new.upper()}\n"
                        f"<b>Time:</b> {dt}\n"
                        f"<b>Qty:</b> {qty}\n"
                        f"<b>Entry:</b> ₹{ex_price:,.2f}\n"
                        f"<b>SL:</b> ₹{sl:,.2f}\n"
                        f"<b>TP:</b> ₹{tp:,.2f}"
                    )
                    print(text)
                    entry_msg_ids = safe_send_telegram(
                        notifier, text, tag="ENTRY"
                    )
                    debug_stats["tg_sends"] += 1
                    if not entry_msg_ids:
                        debug_stats["tg_errors"] += 1

                    open_trades[(s, trade_id)] = {
                        "side": side_new,
                        "qty": qty,
                        "entry": ex_price,
                        "sl": sl,
                        "tp": tp,
                        "entry_msg_ids": entry_msg_ids,
                    }
                else:
                    print(f"[DEBUG] Entry order failed {dt} {s}")
            else:
                print(f"[DEBUG] SKIP entry (qty=0) {dt} {s}")

        # ----- EXIT (FiveEMA owns position) -----
        if st["position"] is not None:
            exit_sig = strat.exit_signal(s, c)
        else:
            exit_sig = None

        if exit_sig and exit_sig.get("signal"):
            debug_stats["exit_signals"] += 1
            exit_sig = {k: v for k, v in exit_sig.items() if k != "symbol"}
            side = exit_sig["side"]
            exit_price = exit_sig["exit_price"]
            trade_id = exit_sig["trade_id"]

            pos = st["position"]
            info = open_trades.get((s, trade_id))

            if not pos or pos["trade_id"] != trade_id:
                debug_stats["exit_skipped_no_position"] += 1
                print(
                    f"[DEBUG] EXIT_SIGNAL but position mismatch "
                    f"{dt} {s} exit_sig={exit_sig} pos={pos}"
                )
            elif not info:
                debug_stats["exit_skipped_mismatch"] += 1
                print(
                    f"[DEBUG] EXIT_SIGNAL but no open_trades info "
                    f"{dt} {s} exit_sig={exit_sig}"
                )
            else:
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
                debug_stats["exits_executed"] += 1

                month_key = dt.month
                monthly_pnl[s][month_key] = (
                    monthly_pnl[s].get(month_key, 0.0) + pnl_trade
                )

                equity = trader.equity(market_prices)
                text = (
                    "📉 <b>BT EXIT</b>\n"
                    f"<b>Symbol:</b> {s}\n"
                    f"<b>Trade ID:</b> #{trade_id} ({exit_sig['signal'].upper()})\n"
                    f"<b>Side:</b> {side.upper()}\n"
                    f"<b>Time:</b> {dt}\n"
                    f"<b>Qty:</b> {qty}\n"
                    f"<b>Entry:</b> ₹{entry_price:,.2f}\n"
                    f"<b>Exit:</b> ₹{actual_exit:,.2f}\n"
                    f"<b>Trade P&L:</b> ₹{pnl_trade:,.2f}\n"
                    f"<b>Symbol Equity:</b> ₹{equity:,.2f}"
                )
                print(text)

                # build per-chat reply map so each chat replies to its own entry
                entry_msg_ids = info.get("entry_msg_ids") or {}
                reply_map = {
                    chat_id: msg_id
                    for chat_id, msg_id in entry_msg_ids.items()
                    if msg_id is not None
                }

                res = safe_send_telegram(
                    notifier,
                    text,
                    tag="EXIT",
                    reply_map=reply_map,
                )
                debug_stats["tg_sends"] += 1
                if not res:
                    debug_stats["tg_errors"] += 1

                # tell strategy to flatten its own state
                strat.force_flat(s)
                del open_trades[(s, trade_id)]

        time.sleep(sleep_per_candle)

    # -------- FINAL MONTHLY SUMMARIES --------
    for s in active_symbols:
        last_m = last_month_seen.get(s)
        if last_m is not None:
            trader = traders[s]
            pnl_m = monthly_pnl[s].get(last_m, 0.0)
            start_cap = month_start_capital[s].get(last_m, trader.starting_cash)
            end_cap = trader.equity(market_prices)
            msg = (
                "📆 <b>Monthly P&L</b>\n"
                f"<b>Symbol:</b> {s}\n"
                f"<b>Period:</b> {backtest_year}-{last_m:02d}\n"
                f"<b>Start Capital:</b> ₹{start_cap:,.2f}\n"
                f"<b>Realized P&L:</b> ₹{pnl_m:,.2f}\n"
                f"<b>End Capital:</b> ₹{end_cap:,.2f}"
            )
            print(msg)
            safe_send_telegram(notifier, msg, tag="MONTHLY_FINAL")

    # -------- 4-MONTH SUMMARY --------
    for sym in active_symbols:
        trader = traders[sym]
        total_sym_pnl = sum(monthly_pnl[sym].values())
        equity = trader.equity(market_prices)
        msg = (
            "✅ <b>4-Month Summary</b>\n"
            f"<b>Symbol:</b> {sym}\n"
            f"<b>Year:</b> {backtest_year}\n"
            f"<b>Months:</b> {start_month}–{start_month + months_to_run - 1}\n"
            f"<b>Start Capital:</b> ₹{trader.starting_cash:,.2f}\n"
            f"<b>Total P&L:</b> ₹{total_sym_pnl:,.2f}\n"
            f"<b>Ending Equity:</b> ₹{equity:,.2f}"
        )
        print(msg)
        safe_send_telegram(notifier, msg, tag="SUMMARY")

    # -------- SAVE CAPITAL STATE --------
    cap_state_out = {s: traders[s].equity(market_prices) for s in active_symbols}
    save_capital_state(cap_state_out, cap_state_path)

    elapsed = time.time() - wall_start
    done_msg = (
        "🏁 <b>BACKTEST COMPLETED</b>\n"
        f"<b>Year:</b> {backtest_year}\n"
        f"<b>Months:</b> {start_month}–{start_month + months_to_run - 1}\n"
        f"<b>Elapsed:</b> {int(elapsed / 60)} min\n"
        f"<b>DEBUG:</b> {debug_stats}"
    )
    print(done_msg)
    safe_send_telegram(notifier, done_msg, tag="DONE")

    print("[DEBUG] Final debug_stats:", debug_stats)


if __name__ == "__main__":
    main()
