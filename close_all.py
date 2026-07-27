# save as close_all.py
import alpaca_trade_api as tradeapi
from config import ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL, api_version='v2')
for p in api.list_positions():
    api.close_position(p.symbol)
    print(f"Closed {p.symbol}: {p.qty} shares")
print("All positions closed.")