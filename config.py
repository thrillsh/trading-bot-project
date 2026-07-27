import os
from dotenv import load_dotenv

load_dotenv()

# ─── API KEYS ───
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET = os.getenv('ALPACA_SECRET', '')
ALPACA_BASE_URL = 'https://paper-api.alpaca.markets'

# ─── STRATEGY SETTINGS ───
LOOKBACK_DAYS = 20
TOP_N = 2
REBALANCE_FREQ_DAYS = 20
STARTING_CAPITAL = 10000
MAX_DEPLOYMENT = 10000  # Max $ to deploy per rebalance (safety cap)

# ─── ASSET UNIVERSE (stocks via yfinance, crypto via Alpaca) ───
# Crypto symbols use the no-slash legacy format (e.g. 'BTCUSD') rather than
# 'BTC/USD' -- Alpaca's docs confirm this format works for both trading and
# data endpoints, which sidesteps any risk of a client library not correctly
# URL-encoding a slash in a path parameter (e.g. closing a position).
ASSETS = {
    'SPY': {'type': 'stock', 'symbol': 'SPY'},
    'XLK': {'type': 'stock', 'symbol': 'XLK'},
    'XLI': {'type': 'stock', 'symbol': 'XLI'},
    'XLF': {'type': 'stock', 'symbol': 'XLF'},
    'BTC': {'type': 'crypto', 'symbol': 'BTCUSD'},
    'ETH': {'type': 'crypto', 'symbol': 'ETHUSD'},
}
