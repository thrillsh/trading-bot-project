"""Check Alpaca account balance"""
import sys

try:
    import alpaca_trade_api as tradeapi
except ImportError:
    print("ERROR: alpaca-trade-api not installed")
    print("Run: pip install alpaca-trade-api")
    sys.exit(1)

from config import ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL, api_version='v2')

try:
    acct = api.get_account()
    print(f"Status:        {acct.status}")
    print(f"Equity:        ${float(acct.equity):,.2f}")
    print(f"Cash:          ${float(acct.cash):,.2f}")
    print(f"Buying Power:  ${float(acct.buying_power):,.2f}")
    print(f"Portfolio:     ${float(acct.portfolio_value):,.2f}")
    print(f"Daytrade Count:{acct.daytrade_count}")
except Exception as e:
    print(f"ERROR: {e}")
    print(f"API Key used:  {ALPACA_API_KEY[:10]}...")
    print(f"Base URL:      {ALPACA_BASE_URL}")