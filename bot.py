import logging
import os

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from database import init_db
from handlers import (
    help_command,
    manage,
    manage_by_id,
    open_trade,
    positions,
    scan,
    sentiment,
    setmodel,
    start,
    handle_photo
)

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
    
    # Add the photo handler
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == '__main__':
    main()
