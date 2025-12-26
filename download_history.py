import os
import csv
from datetime import datetime, timedelta

import yaml

from data_feed import SmartAPIConnector
from telegram_notifier import TelegramNotifier


def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def daterange(start_date, end_date, step_days):
    cur = start_date
    while cur < end_date:
        yield cur, min(cur + timedelta(days=step_days), end_date)
        cur += timedelta(days=step_days)


def main():
    cfg = load_config("config.yaml")
    sa_cfg = cfg["smartapi"]
    bt_cfg = cfg.get("backtest", {})
    data_dir = bt_cfg.get("data_dir", "data")

    tg_cfg = cfg.get("telegram", {})
    notifier = None
    if tg_cfg.get("enable", False):
        notifier = TelegramNotifier(
            bot_token=tg_cfg["bot_token"], chat_ids=tg_cfg.get("chat_ids", [])
        )

    conn = SmartAPIConnector(
        api_key=sa_cfg["api_key"],
        client_id=sa_cfg["client_id"],
        password=sa_cfg["password"],
        totp_secret=sa_cfg["totp_secret"],
        instruments=sa_cfg["instruments"],
        notifier=notifier,
    )

    symbols = cfg["symbols"]

    # choose window for initial download (adjust dates as you like)
    start_year = bt_cfg.get("base_year", 2018)
    end_year = datetime.now().year

    for symbol in symbols:
        inst = sa_cfg["instruments"][symbol]
        exchange = inst["exchange"]
        token = inst["symboltoken"]

        sym_dir = os.path.join(data_dir, symbol)
        os.makedirs(sym_dir, exist_ok=True)

        for year in range(start_year, end_year + 1):
            year_start = datetime(year, 1, 1, 9, 15)
            year_end = datetime(year, 12, 31, 15, 30)

            out_path = os.path.join(sym_dir, f"{year}_5min.csv")
            if os.path.exists(out_path):
                print(f"[{symbol}] {year} already exists, skipping")
                continue

            print(f"[{symbol}] Downloading {year}")
            rows = []
            for chunk_start, chunk_end in daterange(year_start, year_end, 60):
                from_str = chunk_start.strftime("%Y-%m-%d %H:%M")
                to_str = chunk_end.strftime("%Y-%m-%d %H:%M")
                try:
                    resp = conn.get_historical(
                        exchange=exchange,
                        symboltoken=token,
                        interval="FIVE_MINUTE",
                        fromdate=from_str,
                        todate=to_str,
                    )
                except Exception as e:
                    print(f"[{symbol}] HIST ERROR: {e}")
                    continue

                data = resp.get("data") or []
                for candle in data:
                    # [time, o, h, l, c, volume]
                    rows.append(candle)

            if not rows:
                print(f"[{symbol}] No data for {year}, skipping file")
                continue

            # sort and write
            rows.sort(key=lambda r: r[0])
            with open(out_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["datetime", "open", "high", "low", "close", "volume"])
                for r in rows:
                    w.writerow(r)

            print(f"[{symbol}] Saved {len(rows)} candles to {out_path}")


if __name__ == "__main__":
    main()
