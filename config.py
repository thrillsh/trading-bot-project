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

# Minimum daily-return volatility used when computing risk-adjusted momentum
# (momentum / volatility). Without this floor, an asset with unusually low
# recent volatility can produce an artificially inflated score from dividing
# by a near-zero number -- this caps how much that effect can distort ranking.
VOL_FLOOR = 0.001  # 0.1% daily

# ─── TRANSACTION COST ASSUMPTIONS (backtesting only) ───
# Rough round-trip cost (spread + slippage) as a fraction of trade notional,
# in basis points (1 bps = 0.01%). These are ESTIMATES, not observed real
# fills -- there's no live trading history yet to calibrate against. Crypto
# is assumed wider than the stock ETFs here given typically wider spreads,
# especially on less liquid pairs. Revisit these once real fill data exists.
TRANSACTION_COST_BPS = {
    'stock': 3,    # 0.03% -- reasonable for SPY/XLK/XLI/XLF's liquidity
    'crypto': 15,  # 0.15% -- rough estimate for BTC/ETH/XRP on Alpaca
}

# Maximum share of newly-deployed capital any single asset can receive in one
# rebalance, regardless of how favorable its inverse-volatility weight is.
# Without this, a pick with unusually low recent volatility relative to the
# others being bought could end up receiving nearly all the capital -- which
# would defeat the point of holding multiple positions at all. Excess weight
# gets redistributed proportionally among the other picks.
MAX_ASSET_WEIGHT = 0.70

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
    'XRP': {'type': 'crypto', 'symbol': 'XRPUSD'},
}
