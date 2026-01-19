import os
import signal
import sys
import asyncio
import io
import sqlite3
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, CallbackContext

import requests
from typing import Optional, List, Dict

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set in .env")

# --- DATABASE SETUP (The "Business Ledger" with Expiry) ---
def init_db():
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    # Added 'status' and 'closed_date' to support deterministic position management
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  ticker TEXT,
                  type TEXT,
                  strike REAL,
                  entry_price REAL,
                  date TEXT,
                  expiry TEXT,
                  status TEXT DEFAULT 'OPEN',
                  closed_date TEXT)''')

    # Backfill schema for existing databases
    columns = {row[1] for row in c.execute("PRAGMA table_info(trades)")}
    if 'status' not in columns:
        c.execute("ALTER TABLE trades ADD COLUMN status TEXT DEFAULT 'OPEN'")
        c.execute("UPDATE trades SET status = COALESCE(status, 'OPEN')")
    if 'closed_date' not in columns:
        c.execute("ALTER TABLE trades ADD COLUMN closed_date TEXT")

    c.execute("""CREATE INDEX IF NOT EXISTS idx_trades_chat_ticker_status
                 ON trades (chat_id, ticker, status)""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_trades_chat_status
                 ON trades (chat_id, status)""")

    conn.commit()
    conn.close()

init_db()

# --- DYNAMIC MODEL TRACKING ---
user_models = {} 

# --- UPDATED FRAMEWORK CONTEXT ---
FRAMEWORK_CONTEXT = """
You are the 'Grandmaster' Trading Assistant. Your core philosophy is 'Be the Casino, Not the Gambler.'
- Mindset: Sellers collect premiums upfront for an obligation with a statistical edge.
- Analogy: A credit spread is a 'fence' for a 'dog' (the stock). We only care that the boundary isn't crossed.

The Four Core Trades:
1. Cash-Secured Puts (CSP): Getting paid to agree to buy the dip.
2. Covered Calls (CC): Collecting 'rent' on 100+ owned shares.
3. Bull Put Spreads: Selling a higher-strike put, buying a lower-strike put.
4. Call Credit Spreads: Selling a lower-strike call, buying a higher-strike call.

Criteria:
- IV Rank: Favor IV > 50th percentile.
- Timeframe: Target 30-45 DTE for optimal Theta decay.
- Management: Close at 50-60% profit. Roll only for a Net Credit.
"""

HELP_TEXT = """
🎰 *Hercules "Be the Casino" Tutorial* 🎰

/start - Re-introduces the bot and displays the main command menu.
/setmodel [model] - Toggle between Grok (best for X-search), OpenAI, and Gemini.
/scan [ticker] - Analyzes for CSP, CC, BPS, and CCS based on IV and technicals.
/sentiment [sector] - Scans X/Web to suggest the best "Casino" move for a sector.
/manage [ticker] - Checks your trades for 50-60% profit targets or Roll advice.
/manageid [id] - Manage a specific open trade by its ID.
/positions [ticker] - List open positions (optionally filtered by ticker).
/open [ticker] [type] [strike] [premium] [expiry] - Logs your trade (expiry: mm/dd/yyyy).

*Remember: The gold is in managing the position.*
"""

# --- MARKET DATA HELPERS ---
def get_market_data(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        calendar = ticker.calendar
        next_earnings = calendar.iloc[0, 0].strftime('%Y-%m-%d') if calendar is not None and not calendar.empty else "Unknown"
        return {
            "price": info.get("regularMarketPrice") or info.get("currentPrice"),
            "earnings": next_earnings,
            "iv_hint": info.get("beta")
        }
    except Exception:
        return {"price": "N/A", "earnings": "Check Broker", "iv_hint": "N/A"}

# --- AI ROUTING LOGIC ---
async def call_ai(model: str, prompt: str, system_context: str = FRAMEWORK_CONTEXT) -> str:
    if model == 'grok':
        from xai_sdk import Client
        from xai_sdk.chat import user, system
        from xai_sdk.tools import web_search, code_execution, x_search
        client = Client(api_key=os.getenv('GROK_API_KEY'))
        chat = client.chat.create(model="grok-4-1-fast", tools=[web_search(), code_execution(), x_search()])
        chat.append(system(system_context))
        chat.append(user(prompt))
        return chat.sample().content
    elif model == 'openai':
        url = 'https://api.openai.com/v1/chat/completions'
        headers = {'Authorization': f"Bearer {os.getenv('OPENAI_API_KEY')}", 'Content-Type': 'application/json'}
        body = {"model": "gpt-4o", "messages": [{"role": "system", "content": system_context}, {"role": "user", "content": prompt}]}
        return requests.post(url, headers=headers, json=body).json()['choices'][0]['message']['content']
    elif model == 'gemini':
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={os.getenv('GEMINI_API_KEY')}"
        body = {"contents": [{"parts": [{"text": f"{system_context}\n\n{prompt}"}]}]}
        return requests.post(url, json=body).json()['candidates'][0]['content']['parts'][0]['text']

# --- COMMAND HANDLERS ---

async def start(update: Update, context: CallbackContext):
    await update.message.reply_markdown(f"Welcome to **HerculesTradingBot**! 🚀\n\n{HELP_TEXT}")

async def setmodel(update: Update, context: CallbackContext):
    if not context.args: return await update.message.reply_text('Usage: /setmodel [grok|openai|gemini]')
    model = context.args[0].lower()
    if model in ['grok', 'openai', 'gemini']:
        user_models[update.effective_chat.id] = model
        await update.message.reply_text(f"✅ Model set to {model}.")

async def scan(update: Update, context: CallbackContext):
    model = user_models.get(update.effective_chat.id, 'grok')
    ticker_sym = context.args[0].upper() if context.args else 'SOFI'
    data = get_market_data(ticker_sym)
    prompt = (f"Analyze {ticker_sym} at ${data['price']}. Next Earnings: {data['earnings']}. "
              f"Identify best candidate from: CSP, CC, Bull Put Spread, or Call Credit Spread.")
    await handle_ai_request(update, context, model, prompt)

async def sentiment(update: Update, context: CallbackContext):
    model = user_models.get(update.effective_chat.id, 'grok')
    sector = ' '.join(context.args) or 'tech stocks'
    prompt = f"Analyze sentiment for {sector}. Impact on CSP, CC, BPS, and CCS? Best 'Casino' move?."
    await handle_ai_request(update, context, model, prompt)

async def manage(update: Update, context: CallbackContext):
    model = user_models.get(update.effective_chat.id, 'grok')
    ticker = context.args[0].upper() if context.args else None
    if not ticker: return await update.message.reply_text("Usage: /manage [ticker]")

    positions = get_open_positions(update.effective_chat.id, ticker)
    if not positions:
        return await update.message.reply_text(f"No open positions for {ticker}.")

    if len(positions) > 1:
        lines = [format_position_line(p) for p in positions]
        message = (f"⚠️ Multiple open positions found for {ticker}.\n"
                   f"Please select one using /manageid <id>:\n\n" + "\n".join(lines))
        return await update.message.reply_text(message)

    trade = positions[0]
    market = get_market_data(ticker)
    prompt = build_manage_prompt(trade, market)
    await handle_ai_request(update, context, model, prompt)


async def manage_by_id(update: Update, context: CallbackContext):
    model = user_models.get(update.effective_chat.id, 'grok')
    if not context.args:
        return await update.message.reply_text("Usage: /manageid [id]")
    try:
        trade_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Trade id must be a number.")

    trade = get_trade_by_id(trade_id, update.effective_chat.id)
    if not trade:
        return await update.message.reply_text("No open trade found with that ID for this chat.")

    market = get_market_data(trade["ticker"])
    prompt = build_manage_prompt(trade, market)
    await handle_ai_request(update, context, model, prompt)


async def positions(update: Update, context: CallbackContext):
    ticker_filter = context.args[0].upper() if context.args else None
    trades = get_open_positions(update.effective_chat.id, ticker_filter)
    if not trades:
        msg = f"No open positions{f' for {ticker_filter}' if ticker_filter else ''}."
        return await update.message.reply_text(msg)

    header = f"Open positions{f' for {ticker_filter}' if ticker_filter else ''}:"
    lines = [format_position_line(t) for t in trades]
    await update.message.reply_text(f"{header}\n" + "\n".join(lines))

async def open_trade(update: Update, context: CallbackContext):
    """
    Command: /open [ticker] [type] [strike] [premium] [expiry]
    """
    args = context.args
    arg_count = len(args)
    
    # DIAGNOSTIC: If it fails, tell us exactly why
    if arg_count != 5:
        error_msg = (
            f"❌ **Argument Mismatch**\n"
            f"Expected: 5 arguments\n"
            f"Received: {arg_count}\n\n"
            f"Your input: `{args}`\n\n"
            f"Usage: `/open [TICKER] [TYPE] [STRIKE] [PREMIUM] [MM/DD/YYYY]`"
        )
        return await update.message.reply_markdown(error_msg)

    try:
        ticker, t_type, strike, premium, expiry = args
        
        # Validate Date format briefly
        try:
            datetime.strptime(expiry, '%m/%d/%Y')
        except ValueError:
            return await update.message.reply_text("❌ Date must be in MM/DD/YYYY format.")

        conn = sqlite3.connect('trades.db')
        c = conn.cursor()
        c.execute("""INSERT INTO trades (chat_id, ticker, type, strike, entry_price, date, expiry, status, closed_date)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL)""",
                  (update.effective_chat.id, ticker.upper(), t_type.upper(), 
                   float(strike), float(premium), 
                   datetime.now().strftime('%Y-%m-%d'), expiry))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ Business is open! Logged {ticker.upper()} {t_type.upper()} expiring {expiry}.")
        
    except Exception as e:
        await update.message.reply_text(f"⚠️ Database/System Error: {str(e)}")

async def handle_ai_request(update, context, model, prompt):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        result = await call_ai(model, prompt)
        if len(result) > 4000:
            buffer = io.BytesIO(result.encode('utf-8')); buffer.name = 'response.txt'
            await update.message.reply_document(document=buffer, caption='Response is long — sent as file.')
        else: await update.message.reply_markdown(result)
    except Exception as e: await update.message.reply_text(f"Error: {str(e)}")


# --- TRADE HELPERS ---
def row_to_dict(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "type": row["type"],
        "strike": row["strike"],
        "entry_price": row["entry_price"],
        "expiry": row["expiry"],
        "status": row["status"],
        "closed_date": row["closed_date"],
        "date": row["date"],
    }


def get_open_positions(chat_id: int, ticker: Optional[str] = None) -> List[Dict]:
    conn = sqlite3.connect('trades.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if ticker:
        c.execute("""SELECT id, ticker, type, strike, entry_price, date, expiry, status, closed_date
                     FROM trades
                     WHERE ticker=? AND chat_id=? AND status='OPEN'
                     ORDER BY id DESC""", (ticker, chat_id))
    else:
        c.execute("""SELECT id, ticker, type, strike, entry_price, date, expiry, status, closed_date
                     FROM trades
                     WHERE chat_id=? AND status='OPEN'
                     ORDER BY id DESC""", (chat_id,))
    rows = [row_to_dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_trade_by_id(trade_id: int, chat_id: int) -> Optional[Dict]:
    conn = sqlite3.connect('trades.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT id, ticker, type, strike, entry_price, date, expiry, status, closed_date
                 FROM trades
                 WHERE id=? AND chat_id=? AND status='OPEN'
                 LIMIT 1""", (trade_id, chat_id))
    row = c.fetchone()
    conn.close()
    return row_to_dict(row) if row else None


def format_position_line(trade: Dict) -> str:
    return f"• ID {trade['id']} — {trade['ticker']} {trade['type']} {trade['strike']} exp {trade['expiry']} entry {trade['entry_price']}"


def build_manage_prompt(trade: Dict, market: Dict) -> str:
    entry_info = (f"Entry: ${trade['entry_price']} ({trade['type']}) expiring {trade['expiry']} "
                  f"(opened {trade['date']})")
    return (f"Manage {trade['ticker']}. {entry_info}. Market Price: ${market['price']}. "
            f"Today: {datetime.now().strftime('%Y-%m-%d')}. "
            f"Evaluate 50% profit target and provide Net Credit Roll advice.")

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", lambda u, c: u.message.reply_markdown(HELP_TEXT)))
    application.add_handler(CommandHandler("setmodel", setmodel))
    application.add_handler(CommandHandler("scan", scan))
    application.add_handler(CommandHandler("sentiment", sentiment))
    application.add_handler(CommandHandler("manage", manage))
    application.add_handler(CommandHandler("manageid", manage_by_id))
    application.add_handler(CommandHandler("positions", positions))
    application.add_handler(CommandHandler("open", open_trade))
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__': main()
