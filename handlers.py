import io
import logging
from datetime import datetime
from typing import Dict, List
import requests
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import CallbackContext

from ai_engine import (
    build_manage_prompt,
    build_ticker_sentiment_prompt,
    call_ai,
    resolve_model,
    set_user_model,
)
from database import get_open_positions, get_trade_by_id, open_trade as open_trade_record, update_trade_field
from market_data import derive_sectors_for_tickers, get_market_data, is_ticker_like, normalize_tickers
from gemini_vision import analyze_trade_screenshot

logger = logging.getLogger(__name__)

HELP_TEXT = """
🎰 *Hercules "Be the Casino" Tutorial* 🎰

/start - Re-introduces the bot and displays the main command menu.
/setmodel [model] - Toggle between Grok (best for X-search), OpenAI, and Gemini.
/scan [ticker] - Analyzes for CSP, CC, BPS, and CCS based on IV and technicals.
/sentiment [sector or TICKERS] - Sector sentiment or ticker sentiment with auto sector context.
/manage [ticker] - Checks your trades for 50-60% profit targets or Roll advice.
/manageid [id] - Manage a specific open trade by its ID.
/positions [ticker] - List open positions (optionally filtered by ticker).
/open [ticker] [type] [strike] [premium] [expiry] - Logs your trade (expiry: mm/dd/yyyy).
/edit [id] [field] [new_value] - Modify an open position.

*To upload a screenshot, simply send the photo to the bot.*

*Remember: The gold is in managing the position.*
"""


async def start(update: Update, context: CallbackContext):
    await update.effective_message.reply_markdown(f"Welcome to **HerculesTradingBot**! 🚀\n\n{HELP_TEXT}")


async def help_command(update: Update, context: CallbackContext):
    await update.effective_message.reply_markdown(HELP_TEXT)


async def setmodel(update: Update, context: CallbackContext):
    if not context.args:
        return await update.effective_message.reply_text('Usage: /setmodel [grok|openai|gemini]')
    model = context.args[0].lower()
    if model in ['grok', 'openai', 'gemini']:
        set_user_model(update.effective_chat.id, model)
        await update.effective_message.reply_text(
            f"✅ Model set to {model}. Note: /sentiment always uses Grok; /scan and /manage use Gemini with Google Search."
        )


async def scan(update: Update, context: CallbackContext):
    model = resolve_model(update.effective_chat.id, 'scan')
    ticker_sym = context.args[0].upper() if context.args else 'SOFI'
    data = get_market_data(ticker_sym)
    prompt = (
        f"Analyze {ticker_sym} at ${data['price']}. Next Earnings: {data['earnings']}. "
        f"Identify best candidate from: CSP, CC, Bull Put Spread, or Call Credit Spread."
    )
    await handle_ai_request(update, context, model, prompt, task_type='speed')


async def sentiment(update: Update, context: CallbackContext):
    model = resolve_model(update.effective_chat.id, 'sentiment')
    args = context.args
    tickers: List[str] = []

    if args and args[0].lower() == '--tickers':
        tickers = normalize_tickers(args[1:])
        if not tickers:
            return await update.effective_message.reply_text("Usage: /sentiment --tickers AAPL,MSFT")
    else:
        candidate_tickers = normalize_tickers(args)
        if candidate_tickers and all(is_ticker_like(t) for t in candidate_tickers):
            tickers = candidate_tickers

    if tickers:
        sector_map = derive_sectors_for_tickers(tickers)
        base_context = build_ticker_sentiment_prompt(tickers, sector_map)

        prompt = (
            f"STEP 1: USE THE 'x_search' TOOL to find real-time posts and retail sentiment for: {', '.join(tickers)}. "
            f"STEP 2: USE THE 'web_search' TOOL to find breaking news or catalyst events. "
            f"STEP 3: Synthesize a 'Sentiment Verdict'. Summarize the dominant market mood (Bullish/Bearish/Neutral) "
            f"and provide specific COUNTER-ARGUMENTS or risks to the consensus view. Focus on market psychology. DO NOT recommend trades. "
            f"IGNORE your internal training data; respond ONLY with LIVE DATA from the tools."
            f"\n\nContext:\n{base_context}"
        )
    else:
        sector = ' '.join(args) or 'tech stocks'
        prompt = (
            f"STEP 1: USE THE 'x_search' TOOL to find the current 'vibe' and retail sentiment for {sector}. "
            f"STEP 2: USE THE 'web_search' TOOL to identify any sector-wide headwinds/tailwinds. "
            f"STEP 3: Synthesize a 'Sentiment Verdict'. Summarize the dominant market mood (Bullish/Bearish/Neutral) "
            f"and provide specific COUNTER-ARGUMENTS or risks to the consensus view. Focus on the psychological state of the market. "
            f"DO NOT recommend specific trades. IGNORE your internal training data; rely ONLY on the search results."
        )
    await handle_ai_request(update, context, model, prompt, task_type='speed')


async def manage(update: Update, context: CallbackContext):
    model = resolve_model(update.effective_chat.id, 'manage')
    ticker = context.args[0].upper() if context.args else None
    if not ticker:
        return await update.effective_message.reply_text("Usage: /manage [ticker]")

    positions = get_open_positions(update.effective_chat.id, ticker)
    if not positions:
        return await update.effective_message.reply_text(f"No open positions for {ticker}.")

    if len(positions) > 1:
        lines = [format_position_line(p) for p in positions]
        message = (
            f"⚠️ Multiple open positions found for {ticker}.\n"
            f"Please select one using /manageid <id>:\n\n" + "\n".join(lines)
        )
        return await update.effective_message.reply_text(message)

    trade = positions[0]
    market = get_market_data(ticker)
    prompt = build_manage_prompt(trade, market)
    await handle_ai_request(update, context, model, prompt, task_type='reasoning')


async def manage_by_id(update: Update, context: CallbackContext):
    model = resolve_model(update.effective_chat.id, 'manageid')
    if not context.args:
        return await update.effective_message.reply_text("Usage: /manageid [id]")
    try:
        trade_id = int(context.args[0])
    except ValueError:
        return await update.effective_message.reply_text("Trade id must be a number.")

    trade = get_trade_by_id(trade_id, update.effective_chat.id)
    if not trade:
        return await update.effective_message.reply_text("No open trade found with that ID for this chat.")

    market = get_market_data(trade["ticker"])
    prompt = build_manage_prompt(trade, market)
    await handle_ai_request(update, context, model, prompt, task_type='reasoning')


async def positions(update: Update, context: CallbackContext):
    ticker_filter = context.args[0].upper() if context.args else None
    trades = get_open_positions(update.effective_chat.id, ticker_filter)
    if not trades:
        msg = f"No open positions{f' for {ticker_filter}' if ticker_filter else ''}."
        return await update.effective_message.reply_text(msg)

    header = f"Open positions{f' for {ticker_filter}' if ticker_filter else ''}:"
    lines = [format_position_line(t) for t in trades]
    await update.effective_message.reply_text(f"{header}\n" + "\n".join(lines))


async def open_trade(update: Update, context: CallbackContext):
    """
    Command: /open [ticker] [type] [strike] [premium] [expiry]
    """
    args = context.args
    arg_count = len(args)

    if arg_count != 5:
        error_msg = (
            f"❌ **Argument Mismatch**\n"
            f"Expected: 5 arguments\n"
            f"Received: {arg_count}\n\n"
            f"Your input: `{args}`\n\n"
            f"Usage: `/open [TICKER] [TYPE] [STRIKE] [PREMIUM] [MM/DD/YYYY]`"
        )
        return await update.effective_message.reply_markdown(error_msg)

    try:
        ticker, t_type, strike, premium, expiry = args

        try:
            datetime.strptime(expiry, '%m/%d/%Y')
        except ValueError:
            return await update.effective_message.reply_text("❌ Date must be in MM/DD/YYYY format.")

        open_trade_record(
            update.effective_chat.id,
            ticker,
            t_type,
            strike,
            premium,
            expiry,
        )

        await update.effective_message.reply_text(
            f"✅ Business is open! Logged {ticker.upper()} {t_type.upper()} expiring {expiry}."
        )

    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Database/System Error: {str(e)}")


async def handle_photo(update: Update, context: CallbackContext):
    """Handles photo uploads for trade analysis."""
    if not update.message.photo:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        photo_file = await update.message.photo[-1].get_file()
        
        # Use requests to download the file content
        response = requests.get(photo_file.file_path)
        response.raise_for_status()
        image_bytes = response.content

        trade_details = analyze_trade_screenshot(image_bytes)

        if not trade_details:
            return await update.effective_message.reply_text(
                "Could not extract trade details from the image. Please try again."
            )

        # Confirmation message
        confirmation_message = (
            f"Found {trade_details.get('type')} on {trade_details.get('ticker')}.\n"
            f"Short: ${trade_details.get('short_strike')}, Long: ${trade_details.get('long_strike')}\n"
            f"Premium: ${trade_details.get('price')}, Expiry: {trade_details.get('expiry')}\n"
            f"Opened: {trade_details.get('open_date')}\n\n"
            "Is this correct?"
        )
        
        # Store trade_details in context for later use
        context.user_data['trade_details'] = trade_details
        
        await update.effective_message.reply_text(confirmation_message) # We can add Yes/No buttons here later

    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await update.effective_message.reply_text("An error occurred while processing the image.")


def format_position_line(trade: Dict) -> str:
    line = f"• ID {trade['id']} — {trade['ticker']} {trade['type']} {trade['strike']}"
    if trade.get('long_strike'):
        line += f"/{trade['long_strike']}"
    line += f" exp {trade['expiry']} entry {trade['entry_price']}"
    return line

async def confirm_trade(update: Update, context: CallbackContext):
    """Captures text replies to confirm/save the trade."""
    # 1. Check if we actually have a trade waiting for confirmation
    trade_details = context.user_data.get('trade_details')
    if not trade_details:
        return # Ignore random text if no trade is pending

    text = update.message.text.lower().strip()

    # 2. Handle "Yes" - Save to DB
    if text in ['yes', 'y', 'confirm', 'ok']:
        try:
            # Clean price string just in case Gemini added a '$'
            price_raw = str(trade_details['price']).replace('$', '')
            
            trade_id = open_trade_record(
                chat_id=update.effective_chat.id,
                ticker=trade_details['ticker'],
                t_type=trade_details['type'],
                strike=trade_details['short_strike'],
                premium=price_raw,
                expiry=trade_details['expiry'],
                long_strike=trade_details.get('long_strike'),
                open_date=trade_details.get('open_date')
            )
            
            await update.effective_message.reply_text(f"✅ **Trade Saved!** (ID: {trade_id})")
        
        except Exception as e:
            logger.error(f"Save Error: {e}")
            await update.effective_message.reply_text(f"⚠️ Error saving trade: {e}")
        
        # 3. Clear memory so we don't save it twice
        del context.user_data['trade_details']

    # 4. Handle "No" - Cancel
    elif text in ['no', 'n', 'cancel']:
        await update.effective_message.reply_text("❌ Trade discarded.")
        del context.user_data['trade_details']
    
    # 5. Handle ambiguous text
    else:
        await update.effective_message.reply_text("⚠️ Please reply **'Yes'** to save or **'No'** to cancel.")

async def handle_ai_request(update: Update, context: CallbackContext, model: str, prompt: str, task_type: str = 'speed'):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception as e:
        logger.warning("Could not send typing action (harmless network issue): %s", e)

    try:
        result, citations = await call_ai(model, prompt, task_type=task_type)

        if not result:
            result = "⚠️ AI returned no text (Check logs for tool output)."

        if citations:
            deduped = []
            for url in citations:
                if url and url not in deduped:
                    deduped.append(url)
            if deduped:
                sources_block = "\n".join(f"- {url}" for url in deduped)
                result = f"{result}\n\nSources:\n{sources_block}"

        if len(result) > 4000:
            buffer = io.BytesIO(result.encode('utf-8'))
            buffer.name = 'response.txt'
            await update.effective_message.reply_document(document=buffer, caption='Response is long — sent as file.')
        else:
            await update.effective_message.reply_text(result)
    except Exception as e:
        logger.error("Bot Reply Error: %s", e)
        await update.effective_message.reply_text(f"⚠️ System Error: {str(e)}")


async def edit_trade(update: Update, context: CallbackContext):
    """
    Command: /edit [id] [field] [new_value]
    """
    args = context.args
    if len(args) != 3:
        return await update.effective_message.reply_text("Usage: /edit [ID] [FIELD] [NEW_VALUE]")

    trade_id, field, new_value = args
    chat_id = update.effective_chat.id

    FIELD_MAP = {
        'ticker': 'ticker',
        'type': 'type',
        'strike': 'strike',
        'short': 'strike',
        'long': 'long_strike',
        'price': 'entry_price',
        'premium': 'entry_price',
        'expiry': 'expiry',
        'date': 'date',
        'opened': 'date'
    }

    db_field = FIELD_MAP.get(field.lower())
    if not db_field:
        return await update.effective_message.reply_text(f"Invalid field: {field}")

    # Type Conversion
    if db_field in ['strike', 'long_strike', 'entry_price']:
        try:
            new_value = float(new_value)
        except ValueError:
            return await update.effective_message.reply_text("New value must be a number for this field.")
    elif db_field in ['date', 'expiry']:
        try:
            datetime.strptime(new_value, '%Y-%m-%d')
        except ValueError:
            return await update.effective_message.reply_text("Date must be in YYYY-MM-DD format.")

    trade_before = get_trade_by_id(trade_id, chat_id)
    if not trade_before:
        return await update.effective_message.reply_text(f"Trade ID {trade_id} not found.")

    old_value = trade_before[db_field]

    success = update_trade_field(trade_id, chat_id, db_field, new_value)

    if success:
        await update.effective_message.reply_text(
            f"✅ Updated Trade {trade_id}: '{db_field}' changed from {old_value} to {new_value}."
        )
    else:
        await update.effective_message.reply_text(f"⚠️ Failed to update trade {trade_id}.")
