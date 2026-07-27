"""
Clean Sector Rotation Bot — Alpaca-only, cached data
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os
import logging
import subprocess
import sys

# How old data/historical_prices.csv is allowed to be before the bot refuses to trade.
# Daily bars only update once per trading day, so this just needs to catch "fetch never ran"
# or "fetch has been silently failing" rather than every minor delay.
MAX_DATA_AGE_HOURS = 26

from config import (
    ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL,
    LOOKBACK_DAYS, TOP_N, REBALANCE_FREQ_DAYS, STARTING_CAPITAL, MAX_DEPLOYMENT, ASSETS
)

# ─── Logging ───
os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ─── Alpaca Trader ───
class AlpacaTrader:
    def __init__(self):
        import alpaca_trade_api as tradeapi
        self.api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL, api_version='v2')
        acct = self.api.get_account()
        logger.info(f"Alpaca connected. Equity: ${float(acct.equity):,.2f}")

    def is_market_open(self):
        try:
            return self.api.get_clock().is_open
        except Exception as e:
            logger.error(f"Could not check market clock: {e}")
            return False  # fail safe: assume closed if we can't confirm otherwise

    def get_position(self, symbol):
        try:
            return float(self.api.get_position(symbol).qty)
        except Exception as e:
            # Alpaca raises an APIError with a 404 when no position exists — that's
            # the only case where "treat as zero" is correct. Anything else (auth
            # failure, network error, rate limit) must NOT be silently treated as
            # "no position," since that risks the bot double-buying an asset it
            # already holds.
            msg = str(e).lower()
            if 'position does not exist' in msg or '404' in msg:
                return 0.0
            logger.error(f"get_position({symbol}) failed unexpectedly: {e}")
            raise

    def get_cash(self):
        return float(self.api.get_account().cash)

    def get_buying_power(self):
        return float(self.api.get_account().buying_power)

    def close_position(self, symbol):
        try:
            self.api.close_position(symbol)
            logger.info(f"CLOSED {symbol}")
            return True
        except Exception as e:
            logger.error(f"Close {symbol} failed: {e}")
            return False

    def market_order(self, symbol, qty, side, time_in_force='day'):
        qty = round(abs(qty), 4)
        if qty < 0.0001:
            logger.warning(f"Order qty too small for {symbol}: {qty}")
            return False
        try:
            order = self.api.submit_order(
                symbol=symbol, qty=qty, side=side.lower(),
                type='market', time_in_force=time_in_force
            )
            logger.info(f"{side.upper()} {qty} shares of {symbol} (order {order.id})")
            return True
        except Exception as e:
            logger.error(f"Order {symbol} failed: {e}")
            return False


# ─── Main Bot ───
class LiveRotationBot:
    def __init__(self):
        self.lookback = LOOKBACK_DAYS
        self.top_n = TOP_N
        self.rebalance_freq = REBALANCE_FREQ_DAYS
        self.assets = ASSETS
        self.trader = AlpacaTrader()
        self.last_rebalance = None
        self.holdings = {}
        self.load_state()

    def _reconcile_holdings_from_alpaca(self):
        """Ground-truth check: query Alpaca directly for any open positions in our
        tracked assets. Used whenever local state can't be trusted (missing or
        corrupted), so we never buy on top of positions we forgot about."""
        holdings = {}
        for name, cfg in self.assets.items():
            qty = self.trader.get_position(cfg['symbol'])
            if qty > 0:
                holdings[name] = qty
                logger.warning(f"Found existing position: {name} = {qty}")
        return holdings

    def load_state(self):
        try:
            with open('data/state.json', 'r') as f:
                state = json.load(f)
            raw_last_rebalance = state.get('last_rebalance')
            self.last_rebalance = datetime.fromisoformat(raw_last_rebalance) if raw_last_rebalance else None
            self.holdings = state['holdings']
            if self.last_rebalance:
                logger.info(f"State loaded. Last rebalance: {self.last_rebalance.date()}")
            else:
                logger.info("State loaded. No prior rebalance recorded — one is due.")
        except FileNotFoundError:
            # No local record — but don't assume that means no real positions exist.
            logger.info("No state.json found. Reconciling holdings from Alpaca before starting fresh.")
            self.last_rebalance = None
            self.holdings = self._reconcile_holdings_from_alpaca()
            self.save_state()
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # state.json exists but is corrupted/unreadable — same risk, same fix.
            logger.error(f"data/state.json is corrupted ({e}). Reconciling holdings from Alpaca directly.")
            self.last_rebalance = None
            self.holdings = self._reconcile_holdings_from_alpaca()
            self.save_state()

    def save_state(self):
        state = {
            'last_rebalance': self.last_rebalance.isoformat() if self.last_rebalance else None,
            'holdings': self.holdings,
            'updated_at': datetime.now().isoformat()
        }
        os.makedirs('data', exist_ok=True)
        with open('data/state.json', 'w') as f:
            json.dump(state, f, indent=2)

    def refresh_price_data(self):
        """Run fetch_data.py to pull the latest prices. Returns True on success."""
        try:
            result = subprocess.run(
                [sys.executable, 'fetch_data.py'],
                capture_output=True, text=True, timeout=180
            )
            if result.returncode != 0:
                logger.error(f"fetch_data.py failed:\n{result.stderr}")
                return False
            logger.info("Price data refreshed.")
            return True
        except Exception as e:
            logger.error(f"Could not run fetch_data.py: {e}")
            return False

    def fetch_prices(self):
        """Load cached CSV, rename columns, return clean DataFrame.

        Raises if the file is missing or older than MAX_DATA_AGE_HOURS — the bot
        must never trade on silently stale data.
        """
        cache = 'data/historical_prices.csv'
        if not os.path.exists(cache):
            raise FileNotFoundError(f"{cache} not found. Run: python fetch_data.py")

        age_hours = (datetime.now().timestamp() - os.path.getmtime(cache)) / 3600
        if age_hours > MAX_DATA_AGE_HOURS:
            raise RuntimeError(
                f"{cache} is {age_hours:.1f}h old (max allowed: {MAX_DATA_AGE_HOURS}h). "
                f"Refusing to trade on stale data."
            )

        prices = pd.read_csv(cache)
        prices['Date'] = pd.to_datetime(prices['Date'])

        # Rename BTC-USD -> BTC, etc.
        rename = {c: c.replace('-USD', '').replace('/USDT', '') for c in prices.columns if c != 'Date'}
        prices = prices.rename(columns=rename)

        # Keep only assets in config
        keep = ['Date'] + [a for a in self.assets if a in prices.columns]
        missing = [a for a in self.assets if a not in prices.columns]
        if missing:
            logger.warning(f"Missing from cache: {missing}")
        prices = prices[keep].sort_values('Date').reset_index(drop=True)

        # yfinance occasionally appends a placeholder row for "today" with no real
        # data yet (e.g. weekends, or before the day's first print) -- every asset
        # column comes back NaN. Drop trailing rows like that rather than letting
        # them reach validate_prices() as a hard failure.
        asset_cols = [a for a in self.assets if a in prices.columns]
        while len(prices) and prices.iloc[-1][asset_cols].isna().all():
            dropped_date = prices.iloc[-1]['Date'].date()
            prices = prices.iloc[:-1]
            logger.info(f"Dropped empty trailing row for {dropped_date} (no data yet).")

        return prices.reset_index(drop=True)

    def validate_prices(self, prices):
        """Sanity-check fetched data before it's allowed to drive a trade.
        Returns (ok: bool, reason: str)."""
        if prices.empty:
            return False, "price data is empty"

        latest = prices.iloc[-1]

        # Every configured asset should have a real (non-NaN) latest price.
        missing_today = [a for a in self.assets if a in prices.columns and pd.isna(latest.get(a))]
        if missing_today:
            return False, f"missing today's price for: {missing_today}"

        # The most recent date *in the data* should be recent relative to today —
        # catches the case where fetch_data.py runs and rewrites the file "successfully"
        # but the source (yfinance, etc.) actually handed back a stale trading day.
        # Allow slack for weekends/holidays.
        days_stale = (datetime.now().date() - latest['Date'].date()).days
        if days_stale > 4:
            return False, f"latest data point is {days_stale} days old ({latest['Date'].date()})"

        return True, "ok"

    def calculate_momentum(self, prices):
        for name in self.assets:
            if name in prices.columns:
                prices[f'{name}_mom'] = prices[name] / prices[name].shift(self.lookback) - 1
        return prices

    def should_rebalance(self):
        if self.last_rebalance is None:
            return True
        return (datetime.now() - self.last_rebalance).days >= self.rebalance_freq

    def execute_rebalance(self, prices):
        prices = self.calculate_momentum(prices)
        today = prices.iloc[-1]
        now = datetime.now()

        logger.info("=" * 50)
        logger.info(f"REBALANCE: {now.strftime('%Y-%m-%d %H:%M')}")
        logger.info("=" * 50)

        # Rank assets by momentum
        momentums = {a: today[f'{a}_mom'] for a in self.assets if pd.notna(today.get(f'{a}_mom'))}
        ranked = sorted(momentums.items(), key=lambda x: x[1], reverse=True)
        top_assets = [a for a, _ in ranked[:self.top_n]]
        top_set = set(top_assets)

        logger.info(f"Momentums: " + ", ".join([f"{a}={m:+.1%}" for a, m in ranked]))
        logger.info(f"TOP {self.top_n}: {', '.join(top_assets)}")

        # Trade only the delta. Selling something and immediately rebuying the
        # exact same symbol gets rejected by Alpaca as a potential wash trade
        # (the sell hasn't settled before the buy request lands) -- and even
        # when it wouldn't be rejected, it's pure unnecessary slippage/cost.
        currently_held = set(self.holdings.keys())
        to_sell = currently_held - top_set
        to_buy = top_set - currently_held
        unchanged = currently_held & top_set

        if unchanged:
            logger.info(f"--- UNCHANGED (already held, still top {self.top_n}) ---")
            for name in unchanged:
                logger.info(f"  {name}: {self.holdings[name]} shares — no action")

        logger.info("--- SELLING ---")
        if to_sell:
            for name in to_sell:
                cfg = self.assets[name]
                self.trader.close_position(cfg['symbol'])
                del self.holdings[name]
        else:
            logger.info("Nothing to sell.")

        # Get available buying power, capped at MAX_DEPLOYMENT
        raw_bp = self.trader.get_buying_power()
        cash = min(raw_bp, MAX_DEPLOYMENT)
        logger.info(f"Buying power: ${raw_bp:,.2f} | Deploying: ${cash:,.2f} (cap: ${MAX_DEPLOYMENT:,.2f})")

        if not to_buy:
            logger.info("Nothing new to buy — top-ranked assets unchanged from current holdings.")
            self.last_rebalance = now
            self.save_state()
            self.save_dashboard(cash, prices, 'no_changes_needed', 'Top-ranked assets already held — no trades needed.')
            logger.info("Rebalance complete.")
            return

        if cash <= 0:
            logger.warning("No cash available. Skipping buys.")
            self.last_rebalance = now
            self.save_state()
            self.save_dashboard(cash, prices, 'no_cash_available', 'No buying power available for new positions.')
            return

        # BUY only the newly-entered top assets
        logger.info("--- BUYING ---")
        alloc = cash / len(to_buy)
        buy_failures = []
        for name in to_buy:
            cfg = self.assets[name]
            price = today[name]
            qty = round(alloc / price, 4)
            if qty > 0:
                tif = 'gtc' if cfg['type'] == 'crypto' else 'day'
                success = self.trader.market_order(cfg['symbol'], qty, 'buy', time_in_force=tif)
                if success:
                    self.holdings[name] = qty
                else:
                    buy_failures.append(name)
            else:
                logger.warning(f"Calculated qty too small for {name} at ${price:.2f}")
                buy_failures.append(name)

        if buy_failures and len(buy_failures) == len(to_buy):
            # Every intended buy failed -- don't mark this rebalance complete, or
            # the bot will sit on uninvested cash until the next full cycle
            # (up to REBALANCE_FREQ_DAYS days) before trying again.
            logger.error(
                f"All buy orders failed ({', '.join(buy_failures)}). "
                f"NOT advancing last_rebalance -- will retry on next check instead "
                f"of waiting {self.rebalance_freq} days."
            )
            self.save_state()
            self.save_dashboard(
                cash, prices, 'buys_failed',
                f"All buy orders failed: {', '.join(buy_failures)}"
            )
            return
        elif buy_failures:
            logger.warning(f"Some buys failed and were skipped: {', '.join(buy_failures)}")

        self.last_rebalance = now
        self.save_state()
        detail = f"Sold: {', '.join(sorted(to_sell)) or 'none'} | Bought: {', '.join(sorted(self.holdings.keys() & to_buy)) or 'none'}"
        if buy_failures:
            detail += f" | Failed: {', '.join(buy_failures)}"
        self.save_dashboard(cash, prices, 'rebalanced', detail)
        logger.info("Rebalance complete.")

    def _write_dashboard(self, updates: dict):
        """Merge `updates` into dashboard_data.json rather than overwriting it.
        Every run_once() path (success, skip, or failure) calls this so the
        dashboard always reflects the bot's current state -- not just whatever
        the last successful rebalance happened to look like."""
        path = 'dashboard_data.json'
        data = {}
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}

        data.update(updates)
        data['last_checked'] = datetime.now().isoformat()
        data.setdefault('status', 'running')

        os.makedirs('data', exist_ok=True)
        with open('data/bot_data.json', 'w') as f:
            json.dump(data, f, indent=2)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    def save_dashboard(self, cash, prices, last_action='rebalanced', last_action_detail=''):
        today = prices.iloc[-1]
        momentums = {a: today[f'{a}_mom'] for a in self.assets if pd.notna(today.get(f'{a}_mom'))}
        ranked = sorted(momentums.items(), key=lambda x: x[1], reverse=True)

        portfolio_value = cash + sum(
            self.holdings.get(a, 0) * today[a] for a in self.assets if a in prices.columns
        )

        holdings_list = []
        for a in self.assets:
            qty = self.holdings.get(a, 0)
            if qty > 0 and a in prices.columns:
                value = qty * today[a]
                holdings_list.append({
                    'asset': a, 'qty': round(qty, 4), 'value': round(value, 2),
                    'pct': round(value / portfolio_value * 100, 1) if portfolio_value > 0 else 0
                })

        self._write_dashboard({
            'last_updated': datetime.now().isoformat(),
            'account': {
                'total_equity': round(portfolio_value, 2),
                'cash': round(cash, 2),
                'start_value': STARTING_CAPITAL,
                'total_return_pct': round((portfolio_value / STARTING_CAPITAL - 1) * 100, 2)
            },
            'current_holdings': holdings_list,
            'momentums': {k: round(v * 100, 2) for k, v in momentums.items()},
            'top_ranked': [a for a, _ in ranked[:self.top_n]],
            'market_open': True,  # save_dashboard is only reached when the market-open gate already passed
            'last_action': last_action,
            'last_action_detail': last_action_detail,
        })

    def kill_switch(self):
        logger.info("!" * 50)
        logger.info("KILL SWITCH ACTIVATED")
        logger.info("!" * 50)
        for name in list(self.holdings.keys()):
            cfg = self.assets[name]
            self.trader.close_position(cfg['symbol'])
        self.holdings = {}
        self.save_state()
        logger.info("All positions closed.")

    def run_once(self):
        if self.should_rebalance():
            if not self.refresh_price_data():
                logger.warning("Data refresh failed — will fall back to cached CSV if it's still fresh enough.")

            try:
                prices = self.fetch_prices()
            except (FileNotFoundError, RuntimeError) as e:
                logger.error(f"Aborting rebalance: {e}")
                self._write_dashboard({
                    'last_action': 'aborted_bad_data',
                    'last_action_detail': str(e),
                })
                return

            if len(prices) >= self.lookback + 1:
                ok, reason = self.validate_prices(prices)
                if not ok:
                    logger.error(f"Aborting rebalance — data quality check failed: {reason}")
                    self._write_dashboard({
                        'last_action': 'aborted_data_quality',
                        'last_action_detail': reason,
                    })
                    return

                market_open = self.trader.is_market_open()
                if not market_open:
                    logger.warning(
                        "Market is closed — skipping rebalance for now. Orders submitted "
                        "while closed just queue for next open and can cause conflicts "
                        "(e.g. wash-trade rejections) with orders from prior attempts."
                    )
                    self._write_dashboard({
                        'last_action': 'skipped_market_closed',
                        'last_action_detail': 'Market is closed. Will retry on next scheduled check.',
                        'market_open': False,
                    })
                    return  # don't advance last_rebalance -- retry on next check

                self.execute_rebalance(prices)
            else:
                logger.warning(f"Not enough data: {len(prices)} rows (need {self.lookback + 1})")
                self._write_dashboard({
                    'last_action': 'aborted_insufficient_data',
                    'last_action_detail': f"Only {len(prices)} rows available, need {self.lookback + 1}.",
                })
        else:
            days_left = self.rebalance_freq - (datetime.now() - self.last_rebalance).days
            logger.info(f"Next rebalance in {days_left} days")
            self._write_dashboard({
                'last_action': 'no_action_needed',
                'last_action_detail': f"Within holding period — next rebalance in {days_left} day(s).",
                'market_open': self.trader.is_market_open(),
            })


if __name__ == '__main__':
    bot = LiveRotationBot()
    bot.run_once()
