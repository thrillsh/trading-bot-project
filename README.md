# Sector Rotation Trading Bot

A momentum-based sector rotation bot that ranks assets by recent performance and holds the top performers.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up API keys
cp .env.example .env
# Edit .env with your real keys

# 3. Run backtest first
python backtest.py

# 4. Start the bot (paper trading by default)
python scheduler.py
```

## Project Structure

```
trading-bot/
├── config.py           # Settings & API keys
├── bot.py              # Core trading engine
├── scheduler.py        # Hourly scheduler
├── backtest.py         # Historical backtest
├── dashboard.html      # Web dashboard
├── requirements.txt    # Python dependencies
├── .env                # API keys (gitignored)
├── logs/               # Bot logs
└── data/               # State & historical data
```

## Safety

- Paper trading is enabled by default
- Set `BINANCE_TESTNET = False` only when ready for live
- Always start with small amounts
- Use the kill switch if needed

## Dashboard

Open `dashboard.html` in a browser or host on GitHub Pages.
The bot auto-generates `dashboard_data.json` after each rebalance.
