import asyncio
import logging
from typing import Iterable

import pytz
from telegram.ext import CallbackContext

from ai_engine import build_manage_prompt, call_ai, resolve_model
from database import get_all_open_trades
from market_data import get_market_data

logger = logging.getLogger(__name__)

# Eastern time is required for market-aligned scheduling
EST = pytz.timezone("US/Eastern")


async def scheduled_market_scan(context: CallbackContext) -> None:
    """
    Run management analysis for every open trade and push the results
    to the originating chat. Designed to be invoked by JobQueue.
    """
    logger.info("Starting scheduled market scan...")
    trades = get_all_open_trades()

    if not trades:
        logger.info("No open trades to scan.")
        return

    logger.info(f"Found {len(trades)} open trades to scan.")

    # enumerate(trades, 1) starts the counter at 1 for cleaner logs (e.g., "1/8")
    for i, trade in enumerate(trades, 1):
        try:
            ticker = trade["ticker"]
            chat_id = trade["chat_id"]
            
            # Log exactly which trade is being processed
            logger.info(f"Processing {i}/{len(trades)}: Trade ID {trade['id']} ({ticker})")

            market = get_market_data(ticker)
            prompt = build_manage_prompt(trade, market)

            model = resolve_model(chat_id, "manage")
            
            # Await the response to ensure we finish one trade before starting the next
            response, _ = await call_ai(model, prompt, task_type="reasoning")

            message = f"🔔 Scheduled Check: {ticker} {trade['type']}\n\n{response}"
            
            # FIX APPLIED: Removed parse_mode="Markdown"
            # This ensures the message is delivered reliably as plain text,
            # avoiding crashes if the AI generates special characters (like underscores).
            await context.bot.send_message(chat_id=chat_id, text=message)

            # Sleep 2 seconds to prevent hitting API rate limits
            await asyncio.sleep(2)

        except Exception as exc:  # noqa: BLE001
            # Catch individual trade errors so the entire batch doesn't fail
            logger.error("Failed to auto-manage trade %s: %s", trade.get("id"), exc, exc_info=True)
            
    logger.info("Scheduled market scan completed.")


def schedule_weekday_jobs(job_queue, jobs: Iterable[dict]) -> None:
    """
    Helper to register daily jobs for Monday–Friday in US/Eastern.
    Expects items shaped like {'time': datetime.time, 'callback': func, 'name': str}.
    """
    days = (0, 1, 2, 3, 4)  # Monday=0
    for job in jobs:
        job_queue.run_daily(
            job["callback"],
            job["time"],
            days=days,
            name=job.get("name"),
        )