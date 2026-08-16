"""
Backtest Script
Run historical backtests to validate the strategy before going live.

Runs three comparisons side by side:
  1. Full universe (stocks + crypto) -- what the live bot actually uses
  2. Stocks-only -- isolates how much of any outperformance comes from the
     risk-adjusted rotation logic itself, vs. from crypto happening to have
     had a strong run during the backtest window
  3. SPY buy & hold -- the passive baseline

Reports total return AND risk metrics (max drawdown, volatility, Sharpe),
since a headline return number alone hides how rough the ride actually was.
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
from config import LOOKBACK_DAYS, TOP_N, REBALANCE_FREQ_DAYS, STARTING_CAPITAL, ASSETS, VOL_FLOOR, TRANSACTION_COST_BPS, MAX_ASSET_WEIGHT


def cap_weights(weights: dict, max_weight: float) -> dict:
    """Cap each weight at max_weight, redistributing any excess proportionally
    among the remaining (uncapped) assets, re-checking in case that pushes
    another asset over the cap too. If max_weight * n < 1 (cap infeasible
    even split evenly across n assets), falls back to an equal split.
    Kept identical to bot.py's version so backtest and live logic match."""
    weights = dict(weights)
    n = len(weights)
    if n == 0:
        return weights
    if max_weight * n < 1 - 1e-9:
        return {k: 1.0 / n for k in weights}

    fixed = {}
    remaining = set(weights.keys())
    while remaining:
        leftover_mass = 1.0 - sum(fixed.values())
        remaining_total = sum(weights[k] for k in remaining)
        if remaining_total <= 0:
            share = leftover_mass / len(remaining)
            for k in remaining:
                fixed[k] = share
            break

        renormalized = {k: weights[k] / remaining_total * leftover_mass for k in remaining}
        over_cap = [k for k, w in renormalized.items() if w > max_weight + 1e-9]

        if not over_cap:
            fixed.update(renormalized)
            break

        for k in over_cap:
            fixed[k] = max_weight
            remaining.discard(k)

    return fixed


def run_backtest(prices_csv='data/historical_prices.csv', asset_names=None):
    """Runs the rotation backtest over the given asset subset (defaults to
    every asset in config.ASSETS). Returns (results_df, rebalance_dates, total_costs)."""
    prices = pd.read_csv(prices_csv)
    prices['Date'] = pd.to_datetime(prices['Date'])
    prices = prices.sort_values('Date').reset_index(drop=True)

    assets = asset_names if asset_names is not None else list(ASSETS.keys())
    for a in assets:
        prices[f'{a}_mom'] = prices[a] / prices[a].shift(LOOKBACK_DAYS) - 1
        # Same risk-adjustment as the live bot: rank by momentum / volatility,
        # not raw momentum, so crypto's structurally larger swings don't
        # dominate the ranking just by being more volatile.
        daily_returns = prices[a].pct_change()
        vol = daily_returns.rolling(LOOKBACK_DAYS).std()
        prices[f'{a}_vol'] = vol
        prices[f'{a}_score'] = prices[f'{a}_mom'] / vol.clip(lower=VOL_FLOOR)

    portfolio_value = STARTING_CAPITAL
    holdings = {a: 0.0 for a in assets}
    history = []
    rebalance_dates = []
    total_costs = 0.0

    for i in range(LOOKBACK_DAYS, len(prices)):
        today = prices.iloc[i]
        date = today['Date']
        days_since = i - LOOKBACK_DAYS

        if days_since % REBALANCE_FREQ_DAYS == 0:
            # Sell -- deduct estimated transaction cost from proceeds
            for a in assets:
                if holdings[a] > 0:
                    gross = holdings[a] * today[a]
                    cost = gross * TRANSACTION_COST_BPS[ASSETS[a]['type']] / 10000
                    portfolio_value += gross - cost
                    total_costs += cost
                holdings[a] = 0.0

            # Rank & buy -- by risk-adjusted score, matching live bot logic
            scores = {a: today[f'{a}_score'] for a in assets if pd.notna(today[f'{a}_score'])}
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            top = [a for a, _ in ranked[:TOP_N]]

            # Inverse-volatility position sizing: calmer picks get more
            # capital, wilder ones get less -- matches the live bot's sizing.
            inv_vols = {a: 1.0 / max(today[f'{a}_vol'], VOL_FLOOR) for a in top if pd.notna(today[f'{a}_vol'])}
            total_inv_vol = sum(inv_vols.values())
            weights = (
                {a: iv / total_inv_vol for a, iv in inv_vols.items()}
                if total_inv_vol > 0
                else {a: 1.0 / len(top) for a in top}  # fallback; shouldn't normally trigger
            )
            weights = cap_weights(weights, MAX_ASSET_WEIGHT)

            cash_pool = portfolio_value
            for a in top:
                w = weights.get(a, 1.0 / len(top))
                notional = cash_pool * w
                cost = notional * TRANSACTION_COST_BPS[ASSETS[a]['type']] / 10000
                holdings[a] = (notional - cost) / today[a]
                total_costs += cost
            portfolio_value = 0.0
            rebalance_dates.append(date)

        total = portfolio_value + sum(holdings[a] * today[a] for a in assets)
        history.append({'Date': date, 'Portfolio': total})

    return pd.DataFrame(history), rebalance_dates, total_costs


def run_backtest_hysteresis(prices_csv='data/historical_prices.csv', asset_names=None, confirmation_days=2):
    """
    Same scoring/sizing logic as run_backtest(), but rebalances differently:
    checks EVERY trading day (not every REBALANCE_FREQ_DAYS), and only acts
    on a rank change once it has held for `confirmation_days` CONSECUTIVE
    trading days -- filtering out single-day noise (the XLF whipsaw pattern:
    bought, dropped one day, rebought two days later).

    An asset must be in the top N for `confirmation_days` consecutive days
    before it's bought (if not already held). An asset must be OUT of the
    top N for `confirmation_days` consecutive days before it's sold (if
    currently held). This is intentionally asymmetric-safe: something
    already held doesn't get evicted on a single bad day, and something
    not yet held doesn't get chased on a single good day.
    """
    prices = pd.read_csv(prices_csv)
    prices['Date'] = pd.to_datetime(prices['Date'])
    prices = prices.sort_values('Date').reset_index(drop=True)

    assets = asset_names if asset_names is not None else list(ASSETS.keys())
    for a in assets:
        prices[f'{a}_mom'] = prices[a] / prices[a].shift(LOOKBACK_DAYS) - 1
        daily_returns = prices[a].pct_change()
        vol = daily_returns.rolling(LOOKBACK_DAYS).std()
        prices[f'{a}_vol'] = vol
        prices[f'{a}_score'] = prices[f'{a}_mom'] / vol.clip(lower=VOL_FLOOR)

    portfolio_value = STARTING_CAPITAL
    holdings = {a: 0.0 for a in assets}
    history = []
    rebalance_dates = []
    total_costs = 0.0
    trade_count = 0

    # Streak counters: how many consecutive days each asset has been
    # in-top-N (positive streak) or out-of-top-N (tracked separately below).
    in_top_streak = {a: 0 for a in assets}
    out_top_streak = {a: 0 for a in assets}

    for i in range(LOOKBACK_DAYS, len(prices)):
        today = prices.iloc[i]
        date = today['Date']

        scores = {a: today[f'{a}_score'] for a in assets if pd.notna(today[f'{a}_score'])}
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        current_top = set(a for a, _ in ranked[:TOP_N])

        # Update streaks for every scored asset
        for a in scores:
            if a in current_top:
                in_top_streak[a] += 1
                out_top_streak[a] = 0
            else:
                out_top_streak[a] += 1
                in_top_streak[a] = 0

        currently_held = {a for a in assets if holdings[a] > 0}

        # Confirmed sells: held, out of top N for confirmation_days straight
        confirmed_sells = {a for a in currently_held if out_top_streak.get(a, 0) >= confirmation_days}
        # Confirmed buys: not held, in top N for confirmation_days straight
        confirmed_buys = {a for a in scores if a not in currently_held and in_top_streak.get(a, 0) >= confirmation_days}

        trade_happened = False

        # Sell confirmed drop-outs (always frees cash, always safe to do)
        for a in confirmed_sells:
            gross = holdings[a] * today[a]
            cost = gross * TRANSACTION_COST_BPS[ASSETS[a]['type']] / 10000
            portfolio_value += gross - cost
            total_costs += cost
            holdings[a] = 0.0
            trade_count += 1
            trade_happened = True

        # Only attempt buys if there's actual uninvested cash to deploy.
        # BUG FIX: previously this ran unconditionally even when
        # portfolio_value was already 0 (fully deployed, no matching sell
        # this same day) -- that produced a ~$0 buy whose resulting qty
        # never exceeded 0, so the asset never registered as "held" and
        # kept getting flagged as a fresh confirmed_buy candidate on every
        # subsequent day it stayed in the top ranks, silently inflating
        # trade_count. Now: skip the buy and leave the asset queued --
        # it stays a valid confirmed_buy candidate (matches what the live
        # bot does when Alpaca reports no buying power) but doesn't get
        # counted as a trade or touch holdings until cash actually exists.
        if confirmed_buys and portfolio_value > 1e-6:
            inv_vols = {a: 1.0 / max(today[f'{a}_vol'], VOL_FLOOR) for a in confirmed_buys if pd.notna(today[f'{a}_vol'])}
            total_inv_vol = sum(inv_vols.values())
            weights = (
                {a: iv / total_inv_vol for a, iv in inv_vols.items()}
                if total_inv_vol > 0
                else {a: 1.0 / len(confirmed_buys) for a in confirmed_buys}
            )
            weights = cap_weights(weights, MAX_ASSET_WEIGHT)

            cash_pool = portfolio_value
            any_buy_filled = False
            for a in confirmed_buys:
                w = weights.get(a, 1.0 / len(confirmed_buys))
                notional = cash_pool * w
                cost = notional * TRANSACTION_COST_BPS[ASSETS[a]['type']] / 10000
                qty = (notional - cost) / today[a]
                if qty > 0:
                    holdings[a] = qty
                    total_costs += cost
                    trade_count += 1
                    trade_happened = True
                    any_buy_filled = True
            if any_buy_filled:
                portfolio_value = 0.0

        if trade_happened:
            rebalance_dates.append(date)

        total = portfolio_value + sum(holdings[a] * today[a] for a in assets)
        history.append({'Date': date, 'Portfolio': total})

    return pd.DataFrame(history), rebalance_dates, total_costs, trade_count


def compute_metrics(results_df, starting_capital):
    """Total return, CAGR, annualized volatility, a rough Sharpe ratio
    (risk-free rate assumed 0 for simplicity), and max drawdown."""
    final = results_df['Portfolio'].iloc[-1]
    total_return = final / starting_capital - 1

    days = (results_df['Date'].iloc[-1] - results_df['Date'].iloc[0]).days
    years = days / 365.25 if days > 0 else float('nan')
    cagr = (final / starting_capital) ** (1 / years) - 1 if years and years > 0 else float('nan')

    daily_returns = results_df['Portfolio'].pct_change().dropna()
    ann_vol = daily_returns.std() * (252 ** 0.5) if len(daily_returns) > 1 else float('nan')
    sharpe = (daily_returns.mean() * 252) / ann_vol if ann_vol and ann_vol > 0 else float('nan')

    running_max = results_df['Portfolio'].cummax()
    drawdown = (results_df['Portfolio'] - running_max) / running_max
    max_dd = drawdown.min()

    return {
        'final_value': final,
        'total_return': total_return,
        'cagr': cagr,
        'annualized_vol': ann_vol,
        'sharpe': sharpe,
        'max_drawdown': max_dd,
    }


def print_metrics(label, m, total_costs=None):
    print(f"{label}")
    print(f"  Final value:     ${m['final_value']:,.0f}")
    print(f"  Total return:    {m['total_return']:+.2%}")
    print(f"  CAGR:            {m['cagr']:+.2%}")
    print(f"  Annualized vol:  {m['annualized_vol']:.2%}")
    print(f"  Sharpe (rf=0):   {m['sharpe']:.2f}")
    print(f"  Max drawdown:    {m['max_drawdown']:.2%}")
    if total_costs is not None:
        print(f"  Est. costs paid: ${total_costs:,.0f}  ({total_costs / m['final_value']:.2%} of final value)")
    print()


if __name__ == '__main__':
    prices_csv = 'data/historical_prices.csv'

    # Guard: if config.py's ASSETS lists something not yet in the cached
    # price data (e.g. XLE/IWM just added to the pool but fetch_data.py
    # hasn't been re-run since), fail clearly here rather than crashing
    # deep inside a KeyError, or silently scoring on a smaller universe
    # than what config.py actually says it's testing.
    _cached_cols = set(pd.read_csv(prices_csv, nrows=0).columns)
    _missing = [a for a in ASSETS if a not in _cached_cols]
    if _missing:
        print(f"ERROR: config.py's ASSETS includes {_missing}, but these columns")
        print(f"aren't in {prices_csv} yet.")
        print(f"Run: python fetch_data.py   (pulls fresh history for everything in ASSETS)")
        print(f"then re-run this backtest.")
        raise SystemExit(1)

    stock_assets = [a for a, cfg in ASSETS.items() if cfg['type'] == 'stock']
    crypto_assets = [a for a, cfg in ASSETS.items() if cfg['type'] == 'crypto']

    print(f"Transaction cost assumptions (estimates, not observed fills): {TRANSACTION_COST_BPS}")
    print()

    results_full, rebal_full, costs_full = run_backtest(prices_csv)
    print_metrics(f"ROTATION -- full universe, fixed {REBALANCE_FREQ_DAYS}-day rebalance ({', '.join(ASSETS.keys())})", compute_metrics(results_full, STARTING_CAPITAL), costs_full)

    print("=" * 70)
    print("HYSTERESIS COMPARISON -- daily checks, N-day confirmation before trading")
    print("=" * 70)
    print()
    hysteresis_results = {}
    for conf_days in (2, 3, 5):
        r, rebal, costs, n_trades = run_backtest_hysteresis(prices_csv, confirmation_days=conf_days)
        hysteresis_results[conf_days] = (r, rebal, costs, n_trades)
        m = compute_metrics(r, STARTING_CAPITAL)
        print_metrics(f"HYSTERESIS -- {conf_days}-day confirmation ({len(rebal)} rebalance events, {n_trades} trades)", m, costs)

    if crypto_assets:
        results_stocks, _, costs_stocks = run_backtest(prices_csv, asset_names=stock_assets)
        print_metrics(f"ROTATION -- stocks only ({', '.join(stock_assets)})", compute_metrics(results_stocks, STARTING_CAPITAL), costs_stocks)
    else:
        results_stocks = None

    # SPY buy & hold, for reference (no recurring transaction costs -- a
    # single buy-and-hold position, unlike the rotation strategy's repeated
    # sell/buy cycles, so cost modeling doesn't meaningfully apply here)
    raw_prices = pd.read_csv(prices_csv)
    raw_prices['Date'] = pd.to_datetime(raw_prices['Date'])
    raw_prices = raw_prices.sort_values('Date').reset_index(drop=True)
    spy_start_idx = LOOKBACK_DAYS
    spy_bh = STARTING_CAPITAL * (raw_prices['SPY'] / raw_prices.iloc[spy_start_idx]['SPY'])
    results_spy = pd.DataFrame({'Date': raw_prices['Date'].iloc[spy_start_idx:], 'Portfolio': spy_bh.iloc[spy_start_idx:]}).reset_index(drop=True)
    print_metrics("SPY buy & hold", compute_metrics(results_spy, STARTING_CAPITAL))

    plt.figure(figsize=(12, 6))
    plt.plot(results_full['Date'], results_full['Portfolio'], label='Rotation (full universe)', linewidth=2)
    if results_stocks is not None:
        plt.plot(results_stocks['Date'], results_stocks['Portfolio'], label='Rotation (stocks only)', linewidth=2, linestyle='--')
    plt.plot(results_spy['Date'], results_spy['Portfolio'], label='SPY Buy&Hold', linewidth=2, alpha=0.7)
    for rd in rebal_full:
        plt.axvline(rd, color='gray', alpha=0.15, linestyle='--')
    plt.legend()
    plt.title('Backtest Comparison')
    plt.grid(True, alpha=0.3)
    plt.savefig('data/backtest_chart.png')
    plt.show()
