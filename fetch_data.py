import pandas as pd
import yfinance as yf
from datetime import datetime
import os
import time
from functools import reduce

from config import ASSETS

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
    print("Fetching crypto data (Binance via ccxt)...")
    try:
        import ccxt
    except ImportError:
        print("ERROR: ccxt not installed. Run: pip install ccxt")
        exit(1)

    # Same approach as market_scanner.py: testnet credentials if available,
    # but public market data works without authentication too.
    api_key = os.environ.get('BINANCE_TESTNET_KEY', '')
    api_secret = os.environ.get('BINANCE_TESTNET_SECRET', '')
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'timeout': 15000,
    })
    if api_key and api_secret:
        exchange.set_sandbox_mode(True)

    # NOTE: config.py stores crypto symbols as 'BTCUSD' (legacy no-slash
    # format for Alpaca trading compatibility). Binance data endpoint needs
    # the slash format with USDT quote: 'BTC/USDT'. Same conversion logic
    # market_scanner.py uses, adapted for the config's symbol format.
    for name, symbol in crypto_assets.items():
        print(f"  {name}...", end=" ", flush=True)
        # Convert 'BTCUSD' -> 'BTC/USDT' for Binance
        base = symbol.replace('USD', '')
        pair = f"{base}/USDT"

        try:
            # Fetch ~2 years of daily bars (limit 1000 per call)
            ohlcv = exchange.fetch_ohlcv(pair, timeframe='1d', limit=1000)
            if not ohlcv:
                raise ValueError("Empty data returned")

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['Date'] = pd.to_datetime(df['timestamp'], unit='ms').dt.date
            df = df[['Date', 'close']].rename(columns={'close': name})
            df = df.drop_duplicates('Date')
            dfs.append(df)
            print(f"OK ({len(df)} rows)")
        except Exception as e:
            print(f"FAILED: {e}")
            print(f"NOTE: Check that '{pair}' is valid on Binance.")
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
