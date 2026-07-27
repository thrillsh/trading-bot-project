"""
Backtest Script
Run historical backtests to validate the strategy before going live.
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
from config import LOOKBACK_DAYS, TOP_N, REBALANCE_FREQ_DAYS, STARTING_CAPITAL, ASSETS

def run_backtest(prices_csv='data/historical_prices.csv'):
    prices = pd.read_csv(prices_csv)
    prices['Date'] = pd.to_datetime(prices['Date'])
    prices = prices.sort_values('Date').reset_index(drop=True)

    assets = list(ASSETS.keys())
    for a in assets:
        prices[f'{a}_mom'] = prices[a] / prices[a].shift(LOOKBACK_DAYS) - 1

    portfolio_value = STARTING_CAPITAL
    holdings = {a: 0.0 for a in assets}
    history = []
    rebalance_dates = []

    for i in range(LOOKBACK_DAYS, len(prices)):
        today = prices.iloc[i]
        date = today['Date']
        days_since = i - LOOKBACK_DAYS

        if days_since % REBALANCE_FREQ_DAYS == 0:
            # Sell
            for a in assets:
                portfolio_value += holdings[a] * today[a]
                holdings[a] = 0.0

            # Rank & buy
            momentums = {a: today[f'{a}_mom'] for a in assets}
            ranked = sorted(momentums.items(), key=lambda x: x[1], reverse=True)
            top = [a for a, _ in ranked[:TOP_N]]

            alloc = portfolio_value / TOP_N
            for a in top:
                holdings[a] = alloc / today[a]
            portfolio_value = 0.0
            rebalance_dates.append(date)

        total = portfolio_value + sum(holdings[a] * today[a] for a in assets)
        spy_bh = STARTING_CAPITAL * (today['SPY'] / prices.iloc[LOOKBACK_DAYS]['SPY'])
        history.append({'Date': date, 'Portfolio': total, 'SPY': spy_bh})

    results = pd.DataFrame(history)
    print(f"Rotation: ${results['Portfolio'].iloc[-1]:,.0f} ({results['Portfolio'].iloc[-1]/STARTING_CAPITAL-1:+.2%})")
    print(f"SPY:      ${results['SPY'].iloc[-1]:,.0f} ({results['SPY'].iloc[-1]/STARTING_CAPITAL-1:+.2%})")

    plt.figure(figsize=(12, 6))
    plt.plot(results['Date'], results['Portfolio'], label='Rotation', linewidth=2)
    plt.plot(results['Date'], results['SPY'], label='SPY Buy&Hold', linewidth=2, alpha=0.7)
    for rd in rebalance_dates:
        plt.axvline(rd, color='gray', alpha=0.2, linestyle='--')
    plt.legend()
    plt.title('Backtest Results')
    plt.grid(True, alpha=0.3)
    plt.savefig('data/backtest_chart.png')
    plt.show()

if __name__ == '__main__':
    run_backtest()
