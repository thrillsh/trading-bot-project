import os
from dotenv import load_dotenv

load_dotenv()

# ─── API KEYS ───
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET = os.getenv('ALPACA_SECRET', '')
ALPACA_BASE_URL = 'https://paper-api.alpaca.markets'

# Binance testnet keys - separate from your real Binance account entirely.
# Get these from testnet.binance.vision (log in with GitHub, generate an
# HMAC key). This key should be trading-only - never give a bot's key
# withdrawal permission, even on testnet, as a matter of habit.
BINANCE_TESTNET_KEY = os.getenv('BINANCE_TESTNET_KEY', '')
BINANCE_TESTNET_SECRET = os.getenv('BINANCE_TESTNET_SECRET', '')

# ─── STRATEGY SETTINGS ───
LOOKBACK_DAYS = 20
TOP_N = 5
STARTING_CAPITAL = 10000
MAX_DEPLOYMENT = 10000  # Max $ to deploy per rebalance (safety cap)

# Legacy fixed-interval rebalance frequency. No longer used by the live bot
# (bot.py) -- superseded by CONFIRMATION_DAYS hysteresis below. Kept only so
# backtest.py can still run the OLD fixed-20-day logic as a baseline
# comparison against the new hysteresis approach.
REBALANCE_FREQ_DAYS = 20

# ─── HYSTERESIS REBALANCING ───
# The bot checks rankings every trading day, but only acts on a change once
# it's held for CONFIRMATION_DAYS CONSECUTIVE trading days -- filters out
# single-day noise (this is the fix for the XLF whipsaw: bought $57.23 ->
# sold $56.59 next check -> rebought $57.03 two days later, a pure
# noise-driven round trip that lost money on an asset that was actually up
# for the month).
#
# Backtested 2 vs 3 vs 5 days on the 9-asset pool (Aug 2024-Aug 2026 data):
#   Full range:   2-day Sharpe 1.51 / 3-day Sharpe 1.92 / 5-day Sharpe 1.59
#   First half:   2-day Sharpe 1.74 / 3-day Sharpe 2.20 / 5-day Sharpe 1.85
#   Second half:  2-day Sharpe 1.96 / 3-day Sharpe 2.49 / 5-day Sharpe 1.06
# 3-day won on BOTH independent halves of the data, not just the combined
# range -- stronger evidence than a single-window result.
CONFIRMATION_DAYS = 3

# Minimum daily-return volatility used when computing risk-adjusted momentum
# (momentum / volatility). Without this floor, an asset with unusually low
# recent volatility can produce an artificially inflated score from dividing
# by a near-zero number -- this caps how much that effect can distort ranking.
VOL_FLOOR = 0.001  # 0.1% daily

# ─── TRANSACTION COST ASSUMPTIONS (backtesting only) ───
TRANSACTION_COST_BPS = {
    'stock': 3,    # 0.03%
    'crypto': 15,  # 0.15%
}

# Maximum share of newly-deployed capital any single asset can receive in one
# rebalance, regardless of how favorable its inverse-volatility weight is.
MAX_ASSET_WEIGHT = 0.70

# ─── ASSET UNIVERSE ───
# Crypto symbol values (e.g. 'BTCUSD') are legacy from the Alpaca-only
# version and are no longer used for order routing - Binance orders use the
# asset's dict key directly (e.g. 'BTC') since binance_trader.py builds the
# 'BTC/USDT' pair internally. Left in place for backtest.py compatibility.
ASSETS = {
    'SPY': {'type': 'stock', 'symbol': 'SPY'},
    'XLK': {'type': 'stock', 'symbol': 'XLK'},
    'XLI': {'type': 'stock', 'symbol': 'XLI'},
    'XLF': {'type': 'stock', 'symbol': 'XLF'},
    'XLE': {'type': 'stock', 'symbol': 'XLE'},  # Energy
    'IWM': {'type': 'stock', 'symbol': 'IWM'},  # Russell 2000 small caps
    'BTC': {'type': 'crypto', 'symbol': 'BTCUSD'},
    'ETH': {'type': 'crypto', 'symbol': 'ETHUSD'},
    'XRP': {'type': 'crypto', 'symbol': 'XRPUSD'},
}
