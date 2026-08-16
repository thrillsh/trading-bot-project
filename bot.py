"""
Sector Rotation Bot - built on the verified hysteresis logic (confirmed
working in production, dashboard shows +1.98% real return as of Aug 15).

CHANGES FROM THE PREVIOUS VERSION (the one uploaded, "Alpaca-only"):
1. REAL Binance routing added. Crypto (BTC/ETH/XRP) now actually executes
   via Binance testnet instead of failing with "crypto orders not allowed
   for account" every single time - this was described as done in a prior
   session's conversation, but verified NOT actually present in that file.
2. Sell-confirmation bug fixed: previously, `del self.holdings[name]`
   happened unconditionally right after calling close_position(), even if
   the close failed. Now holdings are only cleared for orders that actually
   succeeded - a failed close no longer causes the bot to falsely believe
   it's flat.
3. Market-hours gate now only blocks STOCK orders. Crypto trades 24/7, so a
   confirmed crypto buy/sell no longer waits for NYSE hours - previously the
   whole bot (including crypto legs) was gated on Alpaca's market clock.

The hysteresis logic itself (CONFIRMATION_DAYS streak tracking) is
UNCHANGED from the verified-working version - that part was correct and is
left exactly as-is.
"""

import pandas as pd
import numpy as np
from datetime import datetime
import json
import os
import logging
import subprocess
import sys

MAX_DATA_AGE_HOURS = 26

from config import (
    ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL,
    LOOKBACK_DAYS, TOP_N, CONFIRMATION_DAYS, STARTING_CAPITAL, MAX_DEPLOYMENT, ASSETS, VOL_FLOOR,
    MAX_ASSET_WEIGHT
)
from binance_trader import BinanceTrader

os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('logs/bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def cap_weights(weights: dict, max_weight: float) -> dict:
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


# ─── Alpaca Trader (stocks) ───
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
            return False

    def get_equity(self):
        try:
            return float(self.api.get_account().equity)
        except Exception as e:
            logger.error(f"Could not fetch equity: {e}")
            return None

    def get_position(self, symbol):
        try:
            return float(self.api.get_position(symbol).qty)
        except Exception as e:
            msg = str(e).lower()
            if 'position does not exist' in msg or '404' in msg:
                return 0.0
            logger.error(f"get_position({symbol}) failed unexpectedly: {e}")
            raise

    def get_position_full(self, symbol):
        try:
            pos = self.api.get_position(symbol)
            return {
                'qty': float(pos.qty), 'avg_entry_price': float(pos.avg_entry_price),
                'current_price': float(pos.current_price), 'market_value': float(pos.market_value),
                'cost_basis': float(pos.cost_basis), 'unrealized_pl': float(pos.unrealized_pl),
                'unrealized_plpc': float(pos.unrealized_plpc) * 100,
            }
        except Exception as e:
            msg = str(e).lower()
            if 'position does not exist' in msg or '404' in msg:
                return None
            logger.error(f"get_position_full({symbol}) failed unexpectedly: {e}")
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
            order = self.api.submit_order(symbol=symbol, qty=qty, side=side.lower(),
                                           type='market', time_in_force=time_in_force)
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
        self.confirmation_days = CONFIRMATION_DAYS
        self.assets = ASSETS
        self.stock_trader = AlpacaTrader()
        crypto_symbols = [name for name, cfg in ASSETS.items() if cfg['type'] == 'crypto']
        self.crypto_trader = BinanceTrader(use_testnet=True, relevant_symbols=crypto_symbols)  # flip to False only with a non-withdrawal live key
        self.holdings = {}
        self.in_top_streak = {a: 0 for a in self.assets}
        self.out_top_streak = {a: 0 for a in self.assets}
        self.last_streak_date = None
        self.load_state()

    def _trader_for(self, name):
        return self.crypto_trader if self.assets[name]['type'] == 'crypto' else self.stock_trader

    def _order_symbol(self, name):
        """Binance wants the bare ticker (e.g. 'BTC'); Alpaca wants its
        configured symbol (e.g. 'SPY', 'XLF')."""
        cfg = self.assets[name]
        return name if cfg['type'] == 'crypto' else cfg['symbol']

    def get_total_equity(self):
        stock_eq = self.stock_trader.get_equity() or 0
        crypto_eq = self.crypto_trader.get_equity() or 0
        return stock_eq + crypto_eq

    def _reconcile_holdings(self):
        holdings = {}
        for name in self.assets:
            trader = self._trader_for(name)
            qty = trader.get_position(self._order_symbol(name))
            if qty > 0:
                holdings[name] = qty
                logger.warning(f"Found existing position: {name} = {qty}")
        return holdings

    def _refresh_dashboard_baseline(self):
        equity = self.get_total_equity()
        cash = self.stock_trader.get_cash() + self.crypto_trader.get_cash()
        holdings_list = []
        for name, qty in self.holdings.items():
            trader = self._trader_for(name)
            _, mkt_value = trader.get_position_detail(self._order_symbol(name))
            holdings_list.append({
                'asset': name, 'qty': round(qty, 4), 'value': round(mkt_value, 2),
                'pct': round(mkt_value / equity * 100, 1) if equity > 0 else 0
            })
        self._write_dashboard({
            'last_updated': datetime.now().isoformat(),
            'account': {
                'account_equity': round(equity, 2), 'account_cash': round(cash, 2),
                'deployed_market_value': 0, 'deployed_cost_basis': 0,
                'deployed_unrealized_pl': 0, 'deployed_return_pct': 0,
                'start_value': STARTING_CAPITAL,
            },
            'current_holdings': holdings_list,
        })

    def load_state(self):
        try:
            with open('data/state.json', 'r') as f:
                state = json.load(f)
            self.holdings = state['holdings']
            self.in_top_streak = state.get('in_top_streak', {a: 0 for a in self.assets})
            self.out_top_streak = state.get('out_top_streak', {a: 0 for a in self.assets})
            raw_streak_date = state.get('last_streak_date')
            self.last_streak_date = pd.to_datetime(raw_streak_date).date() if raw_streak_date else None
            logger.info(f"State loaded. Holdings: {list(self.holdings.keys()) or 'none'}")
        except FileNotFoundError:
            logger.info("No state.json found. Reconciling holdings from both brokers before starting fresh.")
            self.holdings = self._reconcile_holdings()
            self.save_state()
            self._refresh_dashboard_baseline()
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"data/state.json corrupted ({e}). Reconciling holdings from both brokers.")
            self.holdings = self._reconcile_holdings()
            self.save_state()
            self._refresh_dashboard_baseline()

    def save_state(self):
        state = {
            'holdings': self.holdings,
            'in_top_streak': self.in_top_streak,
            'out_top_streak': self.out_top_streak,
            'last_streak_date': self.last_streak_date.isoformat() if self.last_streak_date else None,
            'updated_at': datetime.now().isoformat()
        }
        os.makedirs('data', exist_ok=True)
        with open('data/state.json', 'w') as f:
            json.dump(state, f, indent=2)

    def refresh_price_data(self):
        try:
            result = subprocess.run([sys.executable, 'fetch_data.py'], capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                logger.error(f"fetch_data.py failed:\n{result.stderr}")
                return False
            logger.info("Price data refreshed.")
            return True
        except Exception as e:
            logger.error(f"Could not run fetch_data.py: {e}")
            return False

    def fetch_prices(self):
        cache = 'data/historical_prices.csv'
        if not os.path.exists(cache):
            raise FileNotFoundError(f"{cache} not found. Run: python fetch_data.py")
        age_hours = (datetime.now().timestamp() - os.path.getmtime(cache)) / 3600
        if age_hours > MAX_DATA_AGE_HOURS:
            raise RuntimeError(f"{cache} is {age_hours:.1f}h old (max {MAX_DATA_AGE_HOURS}h). Refusing stale data.")

        prices = pd.read_csv(cache)
        prices['Date'] = pd.to_datetime(prices['Date'])
        rename = {c: c.replace('-USD', '').replace('/USDT', '') for c in prices.columns if c != 'Date'}
        prices = prices.rename(columns=rename)
        keep = ['Date'] + [a for a in self.assets if a in prices.columns]
        missing = [a for a in self.assets if a not in prices.columns]
        if missing:
            logger.warning(f"Missing from cache: {missing}")
        prices = prices[keep].sort_values('Date').reset_index(drop=True)

        asset_cols = [a for a in self.assets if a in prices.columns]
        while len(prices) and prices.iloc[-1][asset_cols].isna().all():
            dropped_date = prices.iloc[-1]['Date'].date()
            prices = prices.iloc[:-1]
            logger.info(f"Dropped empty trailing row for {dropped_date}.")
        return prices.reset_index(drop=True)

    def validate_prices(self, prices):
        if prices.empty:
            return False, "price data is empty"
        latest = prices.iloc[-1]
        missing_today = [a for a in self.assets if a in prices.columns and pd.isna(latest.get(a))]
        if missing_today:
            return False, f"missing today's price for: {missing_today}"
        days_stale = (datetime.now().date() - latest['Date'].date()).days
        if days_stale > 4:
            return False, f"latest data point is {days_stale} days old ({latest['Date'].date()})"
        return True, "ok"

    def calculate_momentum(self, prices):
        for name in self.assets:
            if name in prices.columns:
                prices[f'{name}_mom'] = prices[name] / prices[name].shift(self.lookback) - 1
                daily_returns = prices[name].pct_change()
                vol = daily_returns.rolling(self.lookback).std()
                prices[f'{name}_vol'] = vol
                prices[f'{name}_score'] = prices[f'{name}_mom'] / vol.clip(lower=VOL_FLOOR)
        return prices

    def should_rebalance(self):
        today_date = datetime.now().date()
        if self.last_streak_date == today_date:
            return False
        return True

    def _update_streaks(self, top_set, scored_assets):
        for a in scored_assets:
            if a in top_set:
                self.in_top_streak[a] = self.in_top_streak.get(a, 0) + 1
                self.out_top_streak[a] = 0
            else:
                self.out_top_streak[a] = self.out_top_streak.get(a, 0) + 1
                self.in_top_streak[a] = 0

    def execute_rebalance(self, prices):
        prices = self.calculate_momentum(prices)
        today = prices.iloc[-1]
        now = datetime.now()
        trading_day = today['Date'].date()

        logger.info("=" * 50)
        logger.info(f"CHECK: {now.strftime('%Y-%m-%d %H:%M')} (trading day: {trading_day})")
        logger.info("=" * 50)

        scores = {a: today[f'{a}_score'] for a in self.assets if pd.notna(today.get(f'{a}_score'))}
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_assets = [a for a, _ in ranked[:self.top_n]]
        top_set = set(top_assets)

        detail = ", ".join(f"{a}=score:{s:+.2f}(mom:{today[f'{a}_mom']:+.1%})" for a, s in ranked)
        logger.info(f"Rankings: {detail}")
        logger.info(f"TOP {self.top_n} today: {', '.join(top_assets)}")

        if self.last_streak_date != trading_day:
            self._update_streaks(top_set, scores.keys())
            self.last_streak_date = trading_day
        else:
            logger.info("Streaks already updated for this trading day — not double-counting.")

        currently_held = set(self.holdings.keys())
        to_sell = {a for a in currently_held if self.out_top_streak.get(a, 0) >= self.confirmation_days}
        to_buy = {a for a in scores if a not in currently_held and self.in_top_streak.get(a, 0) >= self.confirmation_days}
        unchanged = currently_held - to_sell

        logger.info(f"Confirmation threshold: {self.confirmation_days} consecutive days. "
                    f"in-top: {self.in_top_streak} | out-of-top: {self.out_top_streak}")

        if not to_sell and not to_buy:
            logger.info("No confirmed changes yet — streaks building, no trades this check.")
            self.save_state()
            self.save_dashboard(prices, ranked, top_set, 'no_action_needed',
                                 'No rank change has been confirmed yet — waiting on streak.')
            return

        if unchanged:
            logger.info("--- UNCHANGED (held, not confirmed out) ---")
            for name in unchanged:
                logger.info(f"  {name}: {self.holdings[name]} — no action")

        # Stock market hours only gate STOCK orders - crypto trades 24/7.
        stock_market_open = self.stock_trader.is_market_open()
        if not stock_market_open:
            logger.warning("Stock market closed — stock-side confirmed orders will be deferred this run.")
        stock_deferred = False

        logger.info("--- SELLING ---")
        if to_sell:
            for name in list(to_sell):
                cfg = self.assets[name]
                if cfg['type'] == 'stock' and not stock_market_open:
                    logger.info(f"  {name}: deferred (stock market closed)")
                    stock_deferred = True
                    continue
                trader = self._trader_for(name)
                # BUG FIX: only clear the holding if the close actually
                # succeeded. Previously this deleted unconditionally, so a
                # failed close would make the bot falsely believe it was flat.
                if trader.close_position(self._order_symbol(name)):
                    del self.holdings[name]
                else:
                    logger.error(f"  {name}: close FAILED — still holding, will retry next check.")
        else:
            logger.info("Nothing to sell.")

        stock_bp = self.stock_trader.get_buying_power()
        crypto_cash = self.crypto_trader.get_cash()
        logger.info(f"Stock buying power: ${stock_bp:,.2f} | Crypto (USDT) cash: ${crypto_cash:,.2f}")

        if not to_buy:
            logger.info("Nothing new to buy.")
            self.save_state()
            self.save_dashboard(prices, ranked, top_set, 'no_changes_needed',
                                 'Confirmed sells only — nothing new to buy this check.')
            logger.info("Check complete.")
            return

        if stock_bp <= 0 and crypto_cash <= 0:
            logger.warning("No cash available on either broker. Skipping buys.")
            self.save_state()
            self.save_dashboard(prices, ranked, top_set, 'no_cash_available',
                                 'No buying power available on either broker.')
            return

        inv_vols = {}
        for name in to_buy:
            vol = today.get(f'{name}_vol')
            if pd.isna(vol):
                logger.warning(f"{name}: no volatility data, skipping.")
                continue
            inv_vols[name] = 1.0 / max(vol, VOL_FLOOR)
        total_inv_vol = sum(inv_vols.values())
        weights = ({n: v / total_inv_vol for n, v in inv_vols.items()} if total_inv_vol > 0
                   else {n: 1.0 / len(to_buy) for n in to_buy})
        weights = cap_weights(weights, MAX_ASSET_WEIGHT)

        logger.info("--- BUYING (inverse-volatility sized) ---")
        buy_failures = []
        attempted = []
        for name in to_buy:
            cfg = self.assets[name]
            if cfg['type'] == 'stock' and not stock_market_open:
                logger.info(f"  {name}: deferred (stock market closed)")
                stock_deferred = True
                continue
            attempted.append(name)
            price = today[name]
            weight = weights.get(name, 0)
            intended_alloc = MAX_DEPLOYMENT * weight
            broker_available = crypto_cash if cfg['type'] == 'crypto' else stock_bp
            alloc = min(intended_alloc, broker_available)
            if alloc < intended_alloc:
                logger.warning(f"  {name}: wanted ${intended_alloc:,.2f}, only ${broker_available:,.2f} "
                               f"available — buying what's available.")
            qty = round(alloc / price, 6)
            logger.info(f"  {name}: weight={weight:.1%} alloc=${alloc:,.2f}")
            if qty > 0:
                trader = self._trader_for(name)
                tif = 'gtc' if cfg['type'] == 'crypto' else 'day'
                if trader.market_order(self._order_symbol(name), qty, 'buy', time_in_force=tif):
                    self.holdings[name] = qty
                    if cfg['type'] == 'crypto':
                        crypto_cash -= alloc
                    else:
                        stock_bp -= alloc
                else:
                    buy_failures.append(name)
            else:
                logger.warning(f"Qty too small for {name} at ${price:.2f}")
                buy_failures.append(name)

        if attempted and buy_failures and len(buy_failures) == len(attempted):
            logger.error(f"All attempted buys failed ({', '.join(buy_failures)}). "
                         f"NOT clearing confirmed streak — will retry next check.")
            self.save_state()
            self.save_dashboard(prices, ranked, top_set, 'buys_failed',
                                 f"All buy orders failed: {', '.join(buy_failures)}")
            return
        elif buy_failures:
            logger.warning(f"Some buys failed: {', '.join(buy_failures)}")

        if stock_deferred:
            logger.info("Stock-side confirmed orders deferred until market open — will retry next check "
                        "(crypto orders, if any, already executed this run).")

        self.save_state()
        detail = f"Sold: {', '.join(sorted(to_sell)) or 'none'} | Bought: {', '.join(sorted(self.holdings.keys() & to_buy)) or 'none'}"
        if buy_failures:
            detail += f" | Failed: {', '.join(buy_failures)}"
        if stock_deferred:
            detail += " | Stock leg deferred (market closed)"
        self.save_dashboard(prices, ranked, top_set, 'rebalanced', detail)
        logger.info("Check complete.")

    def _write_dashboard(self, updates: dict):
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
        if 'status' not in updates:
            data['status'] = 'running'

        equity_now = self.get_total_equity()
        if equity_now is not None:
            history = data.get('history', [])
            history.append({'t': datetime.now().isoformat(), 'equity': round(equity_now, 2)})
            if len(history) > 500:
                history = history[-500:]
            data['history'] = history

        os.makedirs('data', exist_ok=True)
        with open('data/bot_data.json', 'w') as f:
            json.dump(data, f, indent=2)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    def save_dashboard(self, prices, ranked, top_set, last_action='rebalanced', last_action_detail=''):
        today = prices.iloc[-1]
        momentums = {a: today[f'{a}_mom'] for a in self.assets if pd.notna(today.get(f'{a}_mom'))}
        scores = {a: today[f'{a}_score'] for a in self.assets if pd.notna(today.get(f'{a}_score'))}

        holdings_list = []
        total_market_value = 0.0
        total_cost_basis = 0.0

        for name in self.assets:
            if self.holdings.get(name, 0) <= 0:
                continue
            cfg = self.assets[name]
            if cfg['type'] == 'stock':
                detail = self.stock_trader.get_position_full(cfg['symbol'])
                if detail is None:
                    logger.warning(f"{name}: held per state, but no Alpaca position - state may be stale.")
                    continue
            else:
                # Binance doesn't expose avg cost / unrealized P&L as simply
                # as Alpaca - showing market value only for crypto (cost
                # basis = market value, i.e. P&L shown as 0) as an honest
                # placeholder rather than a fabricated number.
                qty, mkt_value = self.crypto_trader.get_position_detail(name)
                if qty <= 0:
                    continue
                detail = {
                    'qty': qty, 'avg_entry_price': mkt_value / qty if qty else 0,
                    'current_price': mkt_value / qty if qty else 0,
                    'market_value': mkt_value, 'cost_basis': mkt_value,
                    'unrealized_pl': 0.0, 'unrealized_plpc': 0.0,
                }

            total_market_value += detail['market_value']
            total_cost_basis += detail['cost_basis']
            holdings_list.append({
                'asset': name, 'qty': round(detail['qty'], 4),
                'avg_entry_price': round(detail['avg_entry_price'], 2),
                'current_price': round(detail['current_price'], 2),
                'market_value': round(detail['market_value'], 2),
                'cost_basis': round(detail['cost_basis'], 2),
                'unrealized_pl': round(detail['unrealized_pl'], 2),
                'unrealized_plpc': round(detail['unrealized_plpc'], 2),
            })

        for h in holdings_list:
            h['pct'] = round(h['market_value'] / total_market_value * 100, 1) if total_market_value > 0 else 0

        account_equity = self.get_total_equity()
        account_cash = self.stock_trader.get_cash() + self.crypto_trader.get_cash()
        deployed_return_pct = (round((total_market_value - total_cost_basis) / total_cost_basis * 100, 2)
                                if total_cost_basis > 0 else 0.0)

        self._write_dashboard({
            'last_updated': datetime.now().isoformat(),
            'account': {
                'account_equity': round(account_equity, 2), 'account_cash': round(account_cash, 2),
                'deployed_market_value': round(total_market_value, 2),
                'deployed_cost_basis': round(total_cost_basis, 2),
                'deployed_unrealized_pl': round(total_market_value - total_cost_basis, 2),
                'deployed_return_pct': deployed_return_pct, 'start_value': STARTING_CAPITAL,
            },
            'current_holdings': holdings_list,
            'momentums': {k: round(v * 100, 2) for k, v in momentums.items()},
            'scores': {k: round(v, 3) for k, v in scores.items()},
            'top_ranked': sorted(top_set),
            'market_open': self.stock_trader.is_market_open(),
            'last_action': last_action,
            'last_action_detail': last_action_detail,
        })

    def refresh_account_snapshot(self, prices=None):
        """Keeps dashboard numbers current on checks where nothing traded."""
        holdings_list = []
        total_market_value = 0.0
        total_cost_basis = 0.0
        for name in self.assets:
            if self.holdings.get(name, 0) <= 0:
                continue
            cfg = self.assets[name]
            if cfg['type'] == 'stock':
                detail = self.stock_trader.get_position_full(cfg['symbol'])
                if detail is None:
                    continue
            else:
                qty, mkt_value = self.crypto_trader.get_position_detail(name)
                if qty <= 0:
                    continue
                detail = {'qty': qty, 'avg_entry_price': mkt_value / qty if qty else 0,
                          'current_price': mkt_value / qty if qty else 0, 'market_value': mkt_value,
                          'cost_basis': mkt_value, 'unrealized_pl': 0.0, 'unrealized_plpc': 0.0}
            total_market_value += detail['market_value']
            total_cost_basis += detail['cost_basis']
            holdings_list.append({
                'asset': name, 'qty': round(detail['qty'], 4),
                'avg_entry_price': round(detail['avg_entry_price'], 2),
                'current_price': round(detail['current_price'], 2),
                'market_value': round(detail['market_value'], 2),
                'cost_basis': round(detail['cost_basis'], 2),
                'unrealized_pl': round(detail['unrealized_pl'], 2),
                'unrealized_plpc': round(detail['unrealized_plpc'], 2),
            })
        for h in holdings_list:
            h['pct'] = round(h['market_value'] / total_market_value * 100, 1) if total_market_value > 0 else 0

        account_equity = self.get_total_equity()
        account_cash = self.stock_trader.get_cash() + self.crypto_trader.get_cash()
        deployed_return_pct = (round((total_market_value - total_cost_basis) / total_cost_basis * 100, 2)
                                if total_cost_basis > 0 else 0.0)
        self._write_dashboard({
            'account': {
                'account_equity': round(account_equity, 2), 'account_cash': round(account_cash, 2),
                'deployed_market_value': round(total_market_value, 2),
                'deployed_cost_basis': round(total_cost_basis, 2),
                'deployed_unrealized_pl': round(total_market_value - total_cost_basis, 2),
                'deployed_return_pct': deployed_return_pct, 'start_value': STARTING_CAPITAL,
            },
            'current_holdings': holdings_list,
        })

    def kill_switch(self):
        logger.info("!" * 50)
        logger.info("KILL SWITCH ACTIVATED")
        logger.info("!" * 50)
        # BUG FIX: only clear holdings that actually closed successfully -
        # previously self.holdings = {} unconditionally, even for failed closes.
        still_held = {}
        for name in list(self.holdings.keys()):
            trader = self._trader_for(name)
            if not trader.close_position(self._order_symbol(name)):
                logger.error(f"  {name}: close FAILED — still held.")
                still_held[name] = self.holdings[name]
        self.holdings = still_held
        self.save_state()
        if still_held:
            logger.warning(f"Some positions did not close: {list(still_held.keys())}")
        else:
            logger.info("All positions closed.")

    def run_once(self):
        if os.path.exists('data/.paused'):
            logger.info("Bot is paused — skipping this check.")
            self._write_dashboard({'status': 'paused', 'last_action': 'paused',
                                    'last_action_detail': 'Bot is paused. Remove data/.paused to resume.'})
            return

        if self.should_rebalance():
            if not self.refresh_price_data():
                logger.warning("Data refresh failed — falling back to cached CSV if fresh enough.")
            try:
                prices = self.fetch_prices()
            except (FileNotFoundError, RuntimeError) as e:
                logger.error(f"Aborting: {e}")
                self._write_dashboard({'last_action': 'aborted_bad_data', 'last_action_detail': str(e)})
                return

            if len(prices) < self.lookback + 1:
                logger.warning(f"Not enough data: {len(prices)} rows (need {self.lookback + 1})")
                self._write_dashboard({'last_action': 'aborted_insufficient_data',
                                        'last_action_detail': f"Only {len(prices)} rows, need {self.lookback + 1}."})
                return

            ok, reason = self.validate_prices(prices)
            if not ok:
                logger.error(f"Aborting — data quality check failed: {reason}")
                self._write_dashboard({'last_action': 'aborted_data_quality', 'last_action_detail': reason})
                return

            # NOTE: no blanket market-open gate here anymore - crypto checks
            # should proceed even when the stock market is closed. Stock
            # orders specifically defer inside execute_rebalance() instead.
            self.execute_rebalance(prices)
        else:
            logger.info(f"Already checked today's trading day ({self.last_streak_date}) — nothing new until tomorrow.")
            self.refresh_account_snapshot()
            self._write_dashboard({
                'last_action': 'no_action_needed',
                'last_action_detail': f"Already checked trading day {self.last_streak_date} — next check tomorrow.",
                'market_open': self.stock_trader.is_market_open(),
            })


if __name__ == '__main__':
    bot = LiveRotationBot()
    bot.run_once()
