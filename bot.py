import os
import signal
import sys
import asyncio
import sqlite3
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, CallbackContext

import requests

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set in .env")

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  chat_id INTEGER, 
                  ticker TEXT, 
                  type TEXT, 
                  strike REAL, 
                  entry_price REAL, 
                  date TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- DYNAMIC MODEL TRACKING ---
user_models = {} 

# --- UPDATED FRAMEWORK CONTEXT (The Four Core Trades) ---
FRAMEWORK_CONTEXT = """
You are the 'Grandmaster' Trading Assistant. Core Philosophy: 'Be the Casino, Not the Gambler.'
- Mindset: Sellers collect premiums upfront for an obligation with a statistical edge. 
- Analogy: A trade is a 'fence' for a 'dog' (the stock). We only care that the boundary isn't crossed.

The Four Core Trades:
1. Cash-Secured Puts (CSP): "Getting paid to agree to buy the dip." Selling a put with cash collateral. 
2. Covered Calls (CC): "Manufacturing a dividend" or "collecting rent" on 100+ owned shares.
3. Bull Put Spreads: Selling a higher-strike put and buying a lower-strike put. Bullish/Neutral.
4. Call Credit Spreads: Selling a lower-strike call and buying a higher-strike call. Bearish/Neutral.

Criteria:
- IV Rank: Favor IV > 50th percentile for richer premiums.
- Timeframe: Target 30-45 DTE for optimal Theta decay.
- Risk: NEVER hold through earnings. Monitor dividends for Call-based trades.
- Management: 'The gold is in managing the position.' Close at 50-60% profit. Roll only for a Net Credit.
"""

# --- MARKET DATA HELPERS ---
def get_market_data(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        calendar = ticker.calendar
        next_earnings = "Unknown"
        if calendar is not None and not calendar.empty:
            next_earnings = calendar.iloc[0, 0].strftime('%Y-%m-%d') if hasattr(calendar, 'iloc') else "N/A"

        return {
            "price": info.get("regularMarketPrice") or info.get("currentPrice"),
            "earnings": next_earnings,
            "iv_rank": info.get("beta") # Approximation
        }
    except Exception:
        return {"price": "N/A", "earnings": "Check Broker", "iv_rank": "N/A"}

# --- AI ROUTING LOGIC ---
async def call_ai(model: str, prompt: str, system_context: str = FRAMEWORK_CONTEXT) -> str:
    if model == 'grok':
        from xai_sdk import Client
        from xai_sdk.chat import user, system
        from xai_sdk.tools import web_search, code_execution, x_search
        client = Client(api_key=os.getenv('GROK_API_KEY'))
        chat = client.chat.create(model="grok-2-latest", tools=[web_search(), code_execution(), x_search()])
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
    
    prompt = (
        f"Analyze {ticker_sym} at ${data['price']}. Next Earnings: {data['earnings']}. "
        f"Identify if this ticker is a candidate for: 1. CSP (buy the dip), 2. CC (rent generation), "
        f"3. Bull Put Spread (high IV bull), or 4. Call Credit Spread (high IV bear). "
        f"Recommend the BEST strategy based on current IV and technicals."
    )
    
    await handle_ai_request(update, context, model, prompt)

async def sentiment(update: Update, context: CallbackContext):
    model = user_models.get(update.effective_chat.id, 'grok')
    sector = ' '.join(context.args) or 'tech stocks'
    prompt = (
        f"Fetch sentiment for {sector}. How does this outlook impact our 4 core trades: "
        f"Cash-Secured Puts, Covered Calls, Bull Put Spreads, and Call Credit Spreads? "
        f"Which strategy is the most 'Casino' move right now?"
    )
    await handle_ai_request(update, context, model, prompt)

async def manage(update: Update, context: CallbackContext):
    model = user_models.get(update.effective_chat.id, 'grok')
    ticker = context.args[0].upper() if context.args else None
    if not ticker: return await update.message.reply_text("Usage: /manage [ticker]")
    
    conn = sqlite3.connect('trades.db'); c = conn.cursor()
    c.execute("SELECT entry_price, type FROM trades WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,))
    row = c.fetchone(); conn.close()
    
    market = get_market_data(ticker)
    entry_info = f"Entry: ${row[0]} ({row[1]})" if row else "No entry data."
    prompt = f"Manage {ticker}. {entry_info}. Market Price: ${market['price']}. Apply the 50% profit and Net Credit Roll rules."
    await handle_ai_request(update, context, model, prompt)

async def open_trade(update: Update, context: CallbackContext):
    try:
        ticker, t_type, strike, premium = context.args
        conn = sqlite3.connect('trades.db'); c = conn.cursor()
        c.execute("INSERT INTO trades (chat_id, ticker, type, strike, entry_price, date) VALUES (?, ?, ?, ?, ?, ?)",
                  (update.effective_chat.id, ticker.upper(), t_type.upper(), strike, premium, datetime.now().strftime('%Y-%m-%d')))
        conn.commit(); conn.close()
        await update.message.reply_text(f"Logged {ticker} {t_type} at ${premium}.")
    except: await update.message.reply_text("Usage: /open [ticker] [type:CSP/CC/BPS/CCS] [strike] [premium]")

async def handle_ai_request(update, context, model, prompt):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        result = await call_ai(model, prompt)
        await update.message.reply_markdown(result)
    except Exception as e: await update.message.reply_text(f"Error: {str(e)}")

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd in [("start", lambda u, c: u.message.reply_text("Hercules Ready.")), 
                ("setmodel", setmodel), ("scan", scan), ("sentiment", sentiment), 
                ("manage", manage), ("open", open_trade)]:
        application.add_handler(CommandHandler(cmd[0], cmd[1]))
    application.run_polling()

if __name__ == '__main__': main()