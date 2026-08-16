"""
Binance trader — handles the crypto leg (BTC/ETH/XRP) of the bot, while
AlpacaTrader continues to handle the stock leg (SPY/XLK/XLI/XLF).

Deliberately mirrors AlpacaTrader's method names/shapes (get_equity,
get_position, market_order, etc.) so bot.py can call either one through the
same interface, picking which trader to use per-asset based on its 'type'.

SECURITY: the API key used here should be trading-only, no withdrawal
permission. See README note - never give a bot's key withdrawal rights.
"""
import os
import logging

logger = logging.getLogger(__name__)

TESTNET_BASE_URL = "https://testnet.binance.vision"


class BinanceTrader:
    def __init__(self, use_testnet: bool = True, relevant_symbols=None):
        import ccxt

        api_key = os.environ.get("BINANCE_TESTNET_KEY" if use_testnet else "BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_TESTNET_SECRET" if use_testnet else "BINANCE_SECRET", "")

        if not api_key or not api_secret:
            raise RuntimeError(
                "Missing Binance API credentials. Set BINANCE_TESTNET_KEY/BINANCE_TESTNET_SECRET "
                "(or BINANCE_API_KEY/BINANCE_SECRET for live) as environment variables/GitHub secrets."
            )

        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            # HARD FIX: no timeout was set before, so a single stalled network
            # call (e.g. fetching a ticker for an irrelevant dust asset) could
            # hang the entire bot indefinitely -- exactly what happened here.
            # 15s is generous for a single REST call; ccxt raises a
            # RequestTimeout instead of hanging past this.
            "timeout": 15000,
        })
        if use_testnet:
            self.exchange.set_sandbox_mode(True)  # fake money, real prices - same pattern as paper_trade.py

        self.use_testnet = use_testnet
        # HARD FIX: get_equity() used to loop over EVERY asset in the
        # account's balance -- Binance testnet accounts commonly come
        # pre-seeded with several unrelated "dust" assets (BNB etc.), each
        # of which triggered its own separate fetch_ticker network call with
        # no way to know in advance if that pair even exists/responds
        # quickly. Scoping to only the symbols the bot actually trades
        # avoids querying anything irrelevant at all.
        self.relevant_symbols = list(relevant_symbols) if relevant_symbols else []

        balance = self.exchange.fetch_balance()
        usdt = balance.get("USDT", {}).get("total", 0)
        logger.info(f"Binance connected ({'TESTNET' if use_testnet else 'LIVE'}). USDT balance: ${usdt:,.2f}")

    def is_market_open(self):
        # Crypto markets never close - always true. Kept for interface parity with AlpacaTrader.
        return True

    def get_equity(self):
        """Total account value in USDT: free USDT plus the value of any
        HELD position in one of the bot's OWN crypto symbols (self.relevant_symbols)
        -- deliberately does NOT touch or price any other asset in the
        account, including testnet-seeded dust balances, since those aren't
        relevant to the bot and querying them is what caused past hangs."""
        try:
            balance = self.exchange.fetch_balance()
            total_usdt = balance.get("USDT", {}).get("total", 0)
            for symbol in self.relevant_symbols:
                amount = balance.get(symbol, {}).get("total", 0)
                if not amount:
                    continue
                try:
                    ticker = self.exchange.fetch_ticker(f"{symbol}/USDT")
                    total_usdt += amount * ticker["last"]
                except Exception as e:
                    logger.warning(f"Could not price held {symbol} for equity calc: {e}")
            return total_usdt
        except Exception as e:
            logger.error(f"Could not fetch Binance equity: {e}")
            return None

    def get_position(self, symbol):
        """symbol here is the coin ticker, e.g. 'BTC', 'ETH', 'XRP' (not a pair)."""
        try:
            balance = self.exchange.fetch_balance()
            return float(balance.get(symbol, {}).get("total", 0) or 0)
        except Exception as e:
            logger.error(f"get_position({symbol}) failed: {e}")
            raise

    def get_position_detail(self, symbol):
        qty = self.get_position(symbol)
        if qty <= 0:
            return 0.0, 0.0
        try:
            ticker = self.exchange.fetch_ticker(f"{symbol}/USDT")
            return qty, qty * ticker["last"]
        except Exception as e:
            logger.error(f"get_position_detail({symbol}) failed: {e}")
            return qty, 0.0

    def get_cash(self):
        """Available USDT (Binance's equivalent of Alpaca's 'cash')."""
        try:
            balance = self.exchange.fetch_balance()
            return float(balance.get("USDT", {}).get("free", 0) or 0)
        except Exception as e:
            logger.error(f"Could not fetch Binance USDT balance: {e}")
            return 0.0

    def get_buying_power(self):
        # No margin here - spot trading only, so buying power == free USDT cash.
        return self.get_cash()

    def close_position(self, symbol):
        qty = self.get_position(symbol)
        if qty <= 0:
            logger.info(f"No {symbol} position to close.")
            return True
        try:
            self.exchange.create_market_sell_order(f"{symbol}/USDT", qty)
            logger.info(f"CLOSED {symbol} ({qty})")
            return True
        except Exception as e:
            logger.error(f"Close {symbol} failed: {e}")
            return False

    def market_order(self, symbol, qty, side, time_in_force="gtc"):
        """time_in_force is accepted for interface parity with AlpacaTrader but
        ignored - Binance market orders fill immediately or not at all."""
        qty = round(abs(qty), 6)
        if qty <= 0:
            logger.warning(f"Order qty too small for {symbol}: {qty}")
            return False
        try:
            pair = f"{symbol}/USDT"
            if side.lower() == "buy":
                order = self.exchange.create_market_buy_order(pair, qty)
            else:
                order = self.exchange.create_market_sell_order(pair, qty)
            logger.info(f"{side.upper()} {qty} {symbol} (order {order.get('id')})")
            return True
        except Exception as e:
            logger.error(f"Order {symbol} failed: {e}")
            return False
