"""
Bot Scheduler
Runs the trading bot on a schedule. Checks every hour if rebalance is due.
"""

import schedule
import time
from datetime import datetime
from bot import LiveRotationBot
from config import LOOKBACK_DAYS, TOP_N, REBALANCE_FREQ_DAYS, ASSETS
import logging
import os

os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/scheduler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

bot = LiveRotationBot()

def check_and_trade():
    now = datetime.now()
    logger.info(f"Heartbeat: {now.strftime('%Y-%m-%d %H:%M')}")
    bot.run_once()

# Check every hour
schedule.every().hour.at(":00").do(check_and_trade)
check_and_trade()  # Run immediately

logger.info("=" * 50)
logger.info("SCHEDULER STARTED - Press Ctrl+C to stop")
logger.info("=" * 50)

try:
    while True:
        schedule.run_pending()
        time.sleep(60)
except KeyboardInterrupt:
    logger.info("Scheduler stopped by user.")
