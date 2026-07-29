"""Check current positions and unrealized profit/loss, straight from Alpaca."""
import sys

try:
    import alpaca_trade_api as tradeapi
except ImportError:
    print("ERROR: alpaca-trade-api not installed")
    sys.exit(1)

from config import ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL

api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET, ALPACA_BASE_URL, api_version='v2')

try:
    positions = api.list_positions()
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

if not positions:
    print("No open positions.")
    sys.exit(0)

print(f"{'Symbol':8s} {'Qty':>10s} {'Avg Entry':>12s} {'Current':>12s} {'Mkt Value':>12s} {'P&L $':>10s} {'P&L %':>8s}")
print("-" * 76)

total_pl = 0.0
total_value = 0.0

for p in positions:
    qty = float(p.qty)
    avg_entry = float(p.avg_entry_price)
    current = float(p.current_price)
    mkt_value = float(p.market_value)
    pl_dollar = float(p.unrealized_pl)
    pl_pct = float(p.unrealized_plpc) * 100

    total_pl += pl_dollar
    total_value += mkt_value

    print(f"{p.symbol:8s} {qty:>10.4f} {avg_entry:>12.2f} {current:>12.2f} {mkt_value:>12.2f} {pl_dollar:>+10.2f} {pl_pct:>+7.2f}%")

print("-" * 76)
print(f"{'TOTAL':8s} {'':>10s} {'':>12s} {'':>12s} {total_value:>12.2f} {total_pl:>+10.2f}")
