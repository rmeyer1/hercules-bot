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

    for trade in trades:
        try:
            ticker = trade["ticker"]
            chat_id = trade["chat_id"]

            market = get_market_data(ticker)
            prompt = build_manage_prompt(trade, market)

            model = resolve_model(chat_id, "manage")
            response, _ = await call_ai(model, prompt, task_type="reasoning")

            message = f"🔔 Scheduled Check: {ticker} {trade['type']}\n\n{response}"
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")

            # Prevent hitting provider rate limits when many trades are open
            await asyncio.sleep(2)

        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to auto-manage trade %s: %s", trade.get("id"), exc, exc_info=True)


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
