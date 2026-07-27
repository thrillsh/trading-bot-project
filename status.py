"""
Bot Status Checker
Shows everything at a glance.
"""

import json
import os
from datetime import datetime
from config import STARTING_CAPITAL, MAX_DEPLOYMENT, REBALANCE_FREQ_DAYS

def main():
    # Check if paused
    paused = os.path.exists('data/.paused')

    # Load state
    state = {}
    if os.path.exists('data/state.json'):
        with open('data/state.json', 'r') as f:
            state = json.load(f)

    last_rebalance = None
    if state.get('last_rebalance'):
        last_rebalance = datetime.fromisoformat(state['last_rebalance'])

    holdings = state.get('holdings', {})

    # Load dashboard data for current values
    portfolio_value = STARTING_CAPITAL
    momentums = {}
    top_ranked = []

    if os.path.exists('dashboard_data.json'):
        with open('dashboard_data.json', 'r') as f:
            dash = json.load(f)
        portfolio_value = dash.get('account', {}).get('total_equity', STARTING_CAPITAL)
        momentums = dash.get('momentums', {})
        top_ranked = dash.get('top_ranked', [])

    # Calculate days until next rebalance
    days_left = REBALANCE_FREQ_DAYS
    if last_rebalance:
        days_since = (datetime.now() - last_rebalance).days
        days_left = max(0, REBALANCE_FREQ_DAYS - days_since)

    # Calculate return
    total_return = (portfolio_value / STARTING_CAPITAL - 1) * 100

    # Print everything
    print("=" * 50)
    print("   📈 SECTOR ROTATION BOT STATUS")
    print("=" * 50)
    print()
    print(f"  Status:        {'⏸️  PAUSED' if paused else '▶️  RUNNING'}")
    print(f"  Last rebalance: {last_rebalance.strftime('%Y-%m-%d %H:%M') if last_rebalance else 'Never'}")
    print(f"  Next rebalance: {days_left} days")
    print()
    print(f"  Deployed:      ${portfolio_value:,.2f} / ${MAX_DEPLOYMENT:,.0f}")
    print(f"  Total return:  {total_return:+.2f}%")
    print()

    if holdings:
        print("  Current Holdings:")
        for asset, qty in holdings.items():
            print(f"    • {asset}: {qty} shares")
    else:
        print("  Current Holdings: None")

    if momentums:
        print()
        print("  Latest Momentum Rankings:")
        ranked = sorted(momentums.items(), key=lambda x: x[1], reverse=True)
        for asset, mom in ranked:
            marker = " ⭐" if asset in top_ranked else ""
            print(f"    {asset}: {mom:+.2f}%{marker}")

    print()
    print("=" * 50)
    print(f"  Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

if __name__ == '__main__':
    main()
