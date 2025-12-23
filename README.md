# 5-EMA NIFTY Paper Trading Bot (Angel One SmartAPI Feed)

Minimal Python project implementing a **Power-of-Stocks–style 5 EMA short-only strategy** on **NIFTY 5-minute candles**, with:

- Live prices from **Angel One SmartAPI** (LTP only)
- Strict **paper trading only** (no real orders sent to Angel One)
- Simple 5-minute candle builder and EMA(5) logic

> This project is for **testing and education only**. Do not risk real money without thorough testing and your own validations.

---

## Overview

Architecture:

- `SmartAPIConnector` (in `data_feed.py`)  
  - Connects to Angel One SmartAPI.  
  - Fetches NIFTY LTP via `ltpData`.  
  - **Never places orders**.

- `CandleBuilder` (in `bot.py`)  
  - Aggregates ticks into 5-minute OHLC candles.

- `FiveEMA` (in `strategy.py`)  
  - Simplified Power-of-Stocks 5 EMA **short-only** logic:  
    - Signal candle above EMA (low not touching).  
    - Short on break of signal candle low.  
    - SL at signal candle high, TP at RR multiple.

- `PaperTrader` (in `paper_trader.py`)  
  - In-memory paper trading: cash, positions, trade log.  
  - **All trades are simulated**, never sent to broker.

---

## Quick start

### 1. Create virtualenv and install dependencies

python -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\activate
pip install -r requirements.txt


### 2. Configure NIFTY + SmartAPI in `config.yaml`

Edit `config.yaml`:

mode: paper # keep 'paper' so only PaperTrader is used

symbols:

NIFTY # internal symbol key

interval_seconds: 5
quantity: 50
starting_cash: 100000
slippage: 0.0

smartapi:
enable: true
api_key: "YOUR_API_KEY"
client_id: "YOUR_CLIENT_ID"
password: "YOUR_PASSWORD"
totp: "YOUR_TOTP"
exchange: "NSE"
tradingsymbol: "NIFTY 50"
symboltoken: "XXXX"


- Replace the placeholders with your actual Angel One SmartAPI credentials and Nifty token.
- Keep `mode: paper` and do not add any live order logic.

### 3. Run the smoke test (local, simulated)

python smoke_test.py


This uses `SimulatedFeed` to ensure the 5 EMA strategy and PaperTrader work end-to-end.

### 4. Run the live-feed paper bot

python bot.py


The bot will:

- Connect to SmartAPI.  
- Fetch NIFTY LTP periodically.  
- Build 5-minute candles.  
- Apply the 5 EMA short-only rules.  
- Simulate trades with `PaperTrader`.  
- Print trades and PnL to stdout.

---

## Files

- `config.yaml`  
  - Runtime configuration (mode, NIFTY symbol, quantity, cash, SmartAPI settings).

- `requirements.txt`  
  - Python dependencies (numpy, pandas, pyyaml, smartapi-python).

- `data_feed.py`  
  - `SimulatedFeed`: random walk feed for testing.  
  - `SmartAPIConnector`: live LTP feed from Angel One SmartAPI (no orders).

- `paper_trader.py`  
  - In-memory paper trading engine (cash, positions, trade log).

- `strategy.py`  
  - `FiveEMA`: simplified Power-of-Stocks style 5 EMA short-only strategy on 5-minute candles.

- `bot.py`  
  - Main runner: wires SmartAPI/Simulated feed, candle builder, strategy, and paper trader.

- `smoke_test.py`  
  - Small script using `SimulatedFeed` + `FiveEMA` + `PaperTrader` for quick testing.

---

## Important Notes

- **No real orders**: This project does not call any SmartAPI order functions. All trades are simulated through `PaperTrader`.
- **5-minute logic**: The strategy relies on **completed 5-minute candles**. Do not treat individual ticks as independent signals.
- **Risk and correctness**: This is a simplified educational approximation of the Power-of-Stocks 5 EMA idea. Always validate behaviour against charts and your own tests before using it as a reference.
