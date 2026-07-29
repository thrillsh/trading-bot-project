"""
Read-only momentum check.
Prints the current risk-adjusted ranking (momentum / volatility) for every
asset in config.ASSETS (stocks AND crypto) using whatever is currently in
data/historical_prices.csv. Does NOT place any orders, does NOT touch
Alpaca's trading endpoints at all -- pure verification.

Run fetch_data.py first if you want this to reflect today's prices.
"""

import pandas as pd
from config import ASSETS, LOOKBACK_DAYS, TOP_N, VOL_FLOOR

CACHE = 'data/historical_prices.csv'

prices = pd.read_csv(CACHE)
prices['Date'] = pd.to_datetime(prices['Date'])
prices = prices.sort_values('Date').reset_index(drop=True)

print(f"Loaded {len(prices)} rows, {prices['Date'].min().date()} to {prices['Date'].max().date()}")
print(f"Assets configured: {list(ASSETS.keys())}")
print()

missing = [a for a in ASSETS if a not in prices.columns]
if missing:
    print(f"WARNING: these configured assets have no data in the CSV: {missing}")

scores = {}
raw_moms = {}
for name in ASSETS:
    if name not in prices.columns:
        continue
    prices[f'{name}_mom'] = prices[name] / prices[name].shift(LOOKBACK_DAYS) - 1
    daily_returns = prices[name].pct_change()
    vol = daily_returns.rolling(LOOKBACK_DAYS).std()
    prices[f'{name}_score'] = prices[f'{name}_mom'] / vol.clip(lower=VOL_FLOOR)

    mom_val = prices[f'{name}_mom'].iloc[-1]
    score_val = prices[f'{name}_score'].iloc[-1]
    vol_val = vol.iloc[-1]
    if pd.notna(score_val):
        scores[name] = score_val
        raw_moms[name] = (mom_val, vol_val)

ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

print(f"Risk-adjusted ranking (lookback={LOOKBACK_DAYS} days, top {TOP_N} would be selected):")
print("Score = momentum / volatility -- NOT the same as raw momentum %.")
print("-" * 70)
for i, (name, score) in enumerate(ranked):
    kind = ASSETS[name]['type']
    mom, vol = raw_moms[name]
    marker = "★ TOP" if i < TOP_N else "     "
    print(f"{marker}  {name:6s} ({kind:6s})  score={score:+.2f}   mom={mom:+.2%}   vol={vol:.2%}")

crypto_in_ranking = any(ASSETS[n]['type'] == 'crypto' for n, _ in ranked)
crypto_in_top = any(ASSETS[n]['type'] == 'crypto' for n, _ in ranked[:TOP_N])
print()
print(f"Crypto present in ranking: {crypto_in_ranking}")
print(f"Crypto currently in top {TOP_N}: {crypto_in_top}")
