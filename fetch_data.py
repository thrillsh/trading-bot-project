import pandas as pd
import yfinance as yf
from datetime import datetime
import os
import time
from functools import reduce

from config import ASSETS, ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL

stock_assets = {name: cfg['symbol'] for name, cfg in ASSETS.items() if cfg['type'] == 'stock'}
crypto_assets = {name: cfg['symbol'] for name, cfg in ASSETS.items() if cfg['type'] == 'crypto'}

print("Fetching stock data (yfinance)...")
dfs = []

for name, ticker in stock_assets.items():
    print(f"  {name}...", end=" ", flush=True)

    for attempt in range(5):
        try:
            df = yf.download(
                ticker, 
                period='2y', 
                interval='1d', 
                progress=False, 
                auto_adjust=True,
                timeout=30
            )
            if df.empty:
                raise ValueError("Empty data")
            break
        except Exception as e:
            if attempt < 4:
                print(f"retry {attempt+1}...", end=" ", flush=True)
                time.sleep(10)
            else:
                print(f"FAILED after 5 attempts: {e}")
                print("\nYour internet connection may be too slow for yfinance.")
                print("Alternative: Use the pre-downloaded data file instead.")
                exit(1)

    df = df.reset_index()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [' '.join(col).strip() if col[1] else col[0] for col in df.columns.values]

    close_cols = [c for c in df.columns if 'Close' in c or 'close' in c]
    if not close_cols:
        print(f"ERROR: No Close column found. Columns: {list(df.columns)}")
        exit(1)

    df = df[['Date', close_cols[0]]].rename(columns={close_cols[0]: name})
    df['Date'] = pd.to_datetime(df['Date']).dt.date
    df = df.drop_duplicates('Date')
    dfs.append(df)
    print("OK")

if crypto_assets:
    print("Fetching crypto data (Alpaca)...")
    import alpaca_trade_api as tradeapi
    from alpaca_trade_api.rest import TimeFrame
    from datetime import timedelta

    api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL, api_version='v2')
    end = datetime.now()
    start = end - timedelta(days=730)  # ~2y, matching the stock fetch's period='2y'

    for name, symbol in crypto_assets.items():
        print(f"  {name}...", end=" ", flush=True)
        # Alpaca's trading endpoints (orders, positions) require the no-slash
        # format ('BTCUSD', as stored in config.py), but the market-data
        # endpoint (get_crypto_bars) requires the slash format ('BTC/USD') --
        # confirmed the hard way via a live "invalid symbol" error. Assumes a
        # 3-letter quote currency (USD), true for everything in this config.
        data_symbol = symbol[:-3] + '/' + symbol[-3:]
        try:
            # NOTE: limit= alone was unreliable and returned only a single
            # (today's) bar regardless of the requested limit -- an explicit
            # start/end range is required for a real historical pull.
            bars = api.get_crypto_bars(
                data_symbol, TimeFrame.Day,
                start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d')
            ).df
            if bars.empty:
                raise ValueError("Empty data returned")
            if 'symbol' in bars.columns:
                bars = bars[bars['symbol'] == data_symbol]

            df = bars[['close']].reset_index().rename(columns={'timestamp': 'Date', 'close': name})
            df['Date'] = pd.to_datetime(df['Date']).dt.date
            df = df.drop_duplicates('Date')
            dfs.append(df)
            print(f"OK ({len(df)} rows)")
        except Exception as e:
            print(f"FAILED: {e}")
            print(f"NOTE: crypto data via Alpaca is new in this bot -- if this keeps failing, "
                  f"check the exact column/response shape your installed alpaca-trade-api version "
                  f"returns from get_crypto_bars() and adjust accordingly.")
            exit(1)

# NOTE: this inner join restricts the combined dataset to dates present in
# EVERY asset -- since stocks only trade weekdays, this effectively drops
# crypto's weekend bars from the merged dataset too. That's intentional here:
# the bot's rebalance logic (and its market-hours gate) only acts on weekdays
# anyway, so a 7-days-a-week crypto price history wouldn't currently be used
# even if it were kept.
prices = reduce(lambda l, r: pd.merge(l, r, on='Date', how='inner'), dfs)
prices['Date'] = pd.to_datetime(prices['Date'])
prices = prices.sort_values('Date').reset_index(drop=True)

os.makedirs('data', exist_ok=True)
prices.to_csv('data/historical_prices.csv', index=False)
print(f"\nSaved {len(prices)} rows to data/historical_prices.csv")
print(f"Date range: {prices['Date'].min().date()} to {prices['Date'].max().date()}")
print("\nNow run: python backtest.py")
