"""
Market Scanner -- READ-ONLY forward-looking research tool.

This is deliberately SEPARATE from bot.py and never touches trading logic,
config.py's ASSETS pool, holdings, or orders. It exists to answer one
question honestly: "what's showing real momentum or unusual activity RIGHT
NOW" -- as a forward-looking signal you review and decide on manually, not
as a data source the bot reads automatically.

Why this exists (and why it's built this way):
Earlier in this project we considered picking new assets for the bot by
looking at what performed best in a PAST backtest window -- that's
look-ahead bias: if you choose candidates because you already know they
won, of course a backtest on them looks great, but it tells you nothing
about forward performance. This scanner sidesteps that entirely by only
ever looking at data up to "now," on a schedule, going forward. Whatever
it flags is a genuine real-time signal, not hindsight dressed up as one.

WATCHLIST is intentionally broader than the bot's live trading pool
(config.ASSETS) and intentionally NOT curated based on any known past
performance -- just a diversified, liquid set of names across sectors, so
the scan itself doesn't inherit the same bias it's meant to avoid.

Two modes, meant to be run on different schedules (see the accompanying
market_scanner.yml GitHub Actions workflow):
  --mode daily     wider lookback (20-day momentum/vol), once per day
  --mode intraday  shorter lookback (5-day), several times during market
                    hours, for catching sharper/faster-developing moves

Output is written to data/scanner/findings_<mode>.json -- for you to review
manually. Nothing here writes to state.json, holdings, or ASSETS. Adding
anything this flags to the live bot's pool is a deliberate, separate,
manual step (editing config.py yourself), same discipline as XLE/IWM were
added earlier.
"""

import argparse
import json
import os
from datetime import datetime

import pandas as pd
import yfinance as yf

from config import ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL  # noqa: F401 -- kept in case a future stock data path needs it; crypto no longer uses these

# ─── WATCHLIST ───
# Diversified across sectors, chosen for liquidity/relevance -- NOT because
# any of these already performed well historically. Includes the bot's own
# live pool too, so you can see how it compares to the wider market.
STOCK_WATCHLIST = {
    # Bot's current live pool (sector ETFs + small-cap)
    'SPY': 'S&P 500', 'XLK': 'Tech sector', 'XLI': 'Industrials sector',
    'XLF': 'Financials sector', 'XLE': 'Energy sector', 'IWM': 'Russell 2000 small-cap',
    # Broader individual-name watchlist, spread across sectors
    'AAPL': 'Apple', 'MSFT': 'Microsoft', 'GOOGL': 'Alphabet', 'NVDA': 'Nvidia',
    'AMD': 'AMD', 'META': 'Meta', 'AMZN': 'Amazon', 'TSLA': 'Tesla',
    'JPM': 'JPMorgan', 'GS': 'Goldman Sachs', 'XOM': 'Exxon', 'CVX': 'Chevron',
    'UNH': 'UnitedHealth', 'JNJ': 'Johnson & Johnson', 'WMT': 'Walmart',
    'CAT': 'Caterpillar', 'BA': 'Boeing', 'PLTR': 'Palantir',
}
CRYPTO_WATCHLIST = {
    'BTCUSDT': 'Bitcoin', 'ETHUSDT': 'Ethereum', 'XRPUSDT': 'XRP',
}

MODE_SETTINGS = {
    'daily': {'lookback_days': 20, 'period': '3mo', 'interval': '1d'},
    'intraday': {'lookback_days': 5, 'period': '1mo', 'interval': '1d'},
    # NOTE: yfinance intraday (interval < 1d) requires a paid/rate-limited
    # path for many tickers and is unreliable for a scheduled Actions job --
    # 'intraday' mode here means "shorter lookback on daily bars, run more
    # often," not true intraday candles. Documented so this isn't mistaken
    # for something it isn't.
}


def fetch_stock_history(period, interval):
    rows = {}
    for symbol in STOCK_WATCHLIST:
        try:
            df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True, timeout=30)
            if df.empty:
                print(f"  {symbol}: no data returned, skipping")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            rows[symbol] = df['Close']
        except Exception as e:
            print(f"  {symbol}: FAILED ({e})")
    return rows


def fetch_crypto_history(period_days=90):
    """Uses ccxt/Binance, consistent with binance_trader.py (the rest of the
    project moved off Alpaca for crypto -- Alpaca's crypto endpoints were
    reported non-functional). Testnet by default, same BINANCE_TESTNET_KEY/
    BINANCE_TESTNET_SECRET env vars the live bot uses -- this is read-only
    market data, so testnet vs live doesn't affect data accuracy, but using
    the same credentials/mode keeps this consistent with the rest of the
    project rather than needing a separate credential set."""
    rows = {}
    try:
        import ccxt
        api_key = os.environ.get('BINANCE_TESTNET_KEY', '')
        api_secret = os.environ.get('BINANCE_TESTNET_SECRET', '')
        exchange = ccxt.binance({
            'apiKey': api_key, 'secret': api_secret,
            'enableRateLimit': True, 'timeout': 15000,
        })
        if api_key and api_secret:
            exchange.set_sandbox_mode(True)
        # OHLCV fetch works without authentication even if keys are missing
        # (public market data) -- so this can still run even if credentials
        # aren't set, unlike order placement.

        limit = min(period_days, 1000)  # Binance's per-call candle limit
        for symbol in CRYPTO_WATCHLIST:
            base = symbol.replace('USDT', '')
            pair = f"{base}/USDT"
            try:
                ohlcv = exchange.fetch_ohlcv(pair, timeframe='1d', limit=limit)
                if not ohlcv:
                    print(f"  {symbol}: no data returned, skipping")
                    continue
                closes = pd.Series(
                    [c[4] for c in ohlcv],
                    index=pd.to_datetime([c[0] for c in ohlcv], unit='ms')
                )
                rows[symbol] = closes
            except Exception as e:
                print(f"  {symbol}: FAILED ({e})")
    except ImportError:
        print("  ccxt not installed -- skipping crypto scan (pip install ccxt)")
    return rows


def compute_signals(series: pd.Series, lookback_days: int):
    """
    Returns a dict of read-only signals for one asset's price series:
      - price: latest close
      - chg_1d_pct: most recent 1-day % change
      - momentum_pct: % change over lookback_days
      - volatility_pct: rolling daily-return std over lookback_days (annualized-ish, simple)
      - score: momentum / volatility (same style as the bot's own ranking, for familiarity)
      - dist_from_high_pct: how far below the lookback-period's high the current price sits
      - outlier_flag: True if today's 1-day move is an unusually large multiple of recent volatility
    """
    series = series.dropna()
    if len(series) < lookback_days + 2:
        return None

    price = float(series.iloc[-1])
    prev = float(series.iloc[-2])
    chg_1d_pct = (price / prev - 1) * 100

    daily_returns = series.pct_change().dropna()
    vol = daily_returns.tail(lookback_days).std()
    momentum_pct = (price / float(series.iloc[-lookback_days - 1]) - 1) * 100
    vol_pct = (vol * 100) if pd.notna(vol) else None
    score = (momentum_pct / 100) / vol if vol and vol > 0 else None

    recent_high = float(series.tail(lookback_days).max())
    dist_from_high_pct = (price / recent_high - 1) * 100

    # Outlier: today's move is > 2.5x the recent typical daily move
    daily_ret_today = daily_returns.iloc[-1] if len(daily_returns) else None
    outlier_flag = bool(vol and vol > 0 and daily_ret_today is not None and abs(daily_ret_today) > 2.5 * vol)

    return {
        'price': round(price, 4),
        'chg_1d_pct': round(chg_1d_pct, 2),
        'momentum_pct': round(momentum_pct, 2),
        'volatility_pct': round(vol_pct, 2) if vol_pct is not None else None,
        'score': round(score, 3) if score is not None else None,
        'dist_from_high_pct': round(dist_from_high_pct, 2),
        'outlier_flag': outlier_flag,
    }


def run_scan(mode: str):
    settings = MODE_SETTINGS[mode]
    print(f"Running {mode} scan (lookback={settings['lookback_days']}d, period={settings['period']})...")
    print()

    print("Fetching stocks (yfinance)...")
    stock_series = fetch_stock_history(settings['period'], settings['interval'])
    print()
    print("Fetching crypto (Binance)...")
    crypto_series = fetch_crypto_history(period_days=90)
    print()

    results = []
    for symbol, series in stock_series.items():
        sig = compute_signals(series, settings['lookback_days'])
        if sig:
            sig.update({'symbol': symbol, 'name': STOCK_WATCHLIST[symbol], 'type': 'stock'})
            results.append(sig)
    for symbol, series in crypto_series.items():
        sig = compute_signals(series, settings['lookback_days'])
        if sig:
            sig.update({'symbol': symbol, 'name': CRYPTO_WATCHLIST[symbol], 'type': 'crypto'})
            results.append(sig)

    results_with_score = [r for r in results if r['score'] is not None]
    top_movers = sorted(results_with_score, key=lambda r: r['score'], reverse=True)[:10]
    bottom_movers = sorted(results_with_score, key=lambda r: r['score'])[:5]
    outliers = [r for r in results if r['outlier_flag']]

    output = {
        'mode': mode,
        'generated_at': datetime.now().isoformat(),
        'lookback_days': settings['lookback_days'],
        'scanned_count': len(results),
        'top_movers': top_movers,
        'bottom_movers': bottom_movers,
        'outliers': outliers,
        'all_results': sorted(results, key=lambda r: r['symbol']),
    }

    os.makedirs('data/scanner', exist_ok=True)
    out_path = f'data/scanner/findings_{mode}.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"Scanned {len(results)} assets. Saved to {out_path}")
    print()
    print(f"Top {min(5, len(top_movers))} by momentum/vol score:")
    for r in top_movers[:5]:
        print(f"  {r['symbol']:6s} ({r['name']:20s}) score={r['score']:+.2f}  mom={r['momentum_pct']:+.1f}%  1d={r['chg_1d_pct']:+.1f}%")
    if outliers:
        print()
        print(f"Outlier moves flagged ({len(outliers)}):")
        for r in outliers:
            print(f"  {r['symbol']:6s} ({r['name']:20s}) 1d={r['chg_1d_pct']:+.1f}%  (vs typical vol={r['volatility_pct']:.1f}%)")
    else:
        print()
        print("No outlier moves flagged this run.")

    print()
    print("Reminder: this is research output only. Nothing here has been added")
    print("to config.py's live ASSETS pool -- that stays a deliberate manual step.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Read-only market scanner. Does not trade.")
    parser.add_argument('--mode', choices=list(MODE_SETTINGS.keys()), default='daily')
    args = parser.parse_args()
    run_scan(args.mode)
