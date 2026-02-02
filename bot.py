import logging
import os
from datetime import datetime, time, timedelta

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from database import init_db
from handlers import (
    confirm_trade,
    edit_trade,
    handle_photo,
    help_command,
    manage,
    manage_by_id,
    open_trade,
    positions,
    scan,
    sentiment,
    setmodel,
    start,
)
from jobs import EST, schedule_weekday_jobs, scheduled_market_scan

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set in .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    init_db()

    request = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setmodel", setmodel))
    application.add_handler(CommandHandler("scan", scan))
    application.add_handler(CommandHandler("sentiment", sentiment))
    application.add_handler(CommandHandler("manage", manage))
    application.add_handler(CommandHandler("manageid", manage_by_id))
    application.add_handler(CommandHandler("positions", positions))
    application.add_handler(CommandHandler("open", open_trade))
    application.add_handler(CommandHandler("edit", edit_trade))
    
    # Add the photo handler
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # NEW: Add the text listener for confirmations
    # filters.TEXT & ~filters.COMMAND ensures we don't block commands like /start
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_trade))

    # Register weekday market scans (Eastern Time)
    market_jobs = [
        {"time": time(9, 30, tzinfo=EST), "callback": scheduled_market_scan, "name": "scan_market_open"},
        {"time": time(12, 0, tzinfo=EST), "callback": scheduled_market_scan, "name": "scan_midday"},
        {"time": time(15, 45, tzinfo=EST), "callback": scheduled_market_scan, "name": "scan_power_hour"},
    ]
    schedule_weekday_jobs(application.job_queue, market_jobs)

    # Optional: quick local verification window (minutes from now), still weekdays only
    test_offset = os.getenv("SCHEDULE_TEST_MINUTES")
    if test_offset:
        try:
            minutes = int(test_offset)
            now_est = datetime.now(EST) + timedelta(minutes=minutes)
            test_time = now_est.time().replace(microsecond=0)
            today = datetime.now(EST).weekday()
            application.job_queue.run_daily(
                scheduled_market_scan,
                test_time,
                days=(today,),
                name="scan_test_run",
            )
            logger.info("Scheduled test market scan at %s EST for weekday index %s", test_time.strftime("%H:%M"), today)
        except ValueError:
            logger.warning("Invalid SCHEDULE_TEST_MINUTES value: %s", test_offset)

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == '__main__':
    main()
