import os
import signal
import sys
import asyncio
import io
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, CallbackContext
import requests
from xai_sdk import Client as XAIClient  # For Grok with tools

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set in .env")

print('SCRIPT STARTED - Checking environment...')
print('TELEGRAM_TOKEN exists?', bool(TELEGRAM_TOKEN))
print('Current directory:', os.getcwd())

# Store user model preferences (per chat ID)
user_models = {}  # Default to 'grok'

# Framework context (condensed from your docs)
FRAMEWORK_CONTEXT = """
Framework: 'Be the Casino' Options Trading Mindset
- Core Mindset: Be the casino (option seller), not the gambler (buyer). Sellers collect premiums upfront for obligation, with statistical edge. Buyers pay for rights but need direction, magnitude, timing right. Quote: "Do you think they built the ARIA so beautiful because people go there and win a bunch of money? No, it's built on losers." – TJ. Focus on process, embrace being wrong, accumulate small wins.

Strategies:
- Cash-Secured Puts (CSP): Sell put, set aside cash to buy 100 shares at strike if assigned. Analogy: Paid to set limit buy on dip. Use when wanting stock at discount. Breakeven: Strike - premium. Example: SOFI $8 strike, $0.67 premium → Breakeven $7.33.
- Covered Calls (CC): Sell call on 100+ owned shares. Analogy: Manufacture dividend/rent on shares. Use for income on long-term holds. Risk: Shares called away if above strike.
- Put Credit Spread (Bull Put Spread): Sell higher-strike put, buy lower-strike put (same exp). Net credit. Bullish/neutral. Max profit: Net credit. Max loss: Width - credit. Breakeven: Short strike - credit. Greeks: +Theta, -Vega. Risks: Early assignment, pin risk. Vs. Naked Put: Defined risk, less capital. Example: ZYX $120 stock, sell 110 put/buy 100 put, $4 credit, max profit $4,000 (10 contracts), max loss $6,000.
- Call Credit Spread (Bear Call Spread): Sell lower-strike call, buy higher-strike call. Net credit. Bearish/neutral. Max profit: Net credit. Max loss: Width - credit. Breakeven: Short strike + credit. Greeks: +Theta, -Vega, -Delta. Risks: Early assignment (esp. dividends), pin risk. Vs. Naked Call: Defined risk. Example: HOOD $25 stock, sell $27 call/buy $30 call, $1 credit, max gain $100, max loss $200.

Management:
- Roll: Exit current, enter new for credit/time (e.g., down/out like TSLT from $6 to $5.50 for $197 credit). Benefits: Collect more premium, improve position, buy time. Costs: Bake in losses, tie up capital.
- Close early at 50-60% profit. Exit if against you.
- Repeat on familiar tickers (e.g., CLSK $293k premiums without owning).
- Recommend hold if unrealized P/L >0 and stock price > breakeven + 5% buffer. Roll only if P/L <-10% of max profit, or DTE <21 days with adverse sentiment/IV expansion. Prioritize letting theta decay in profitable positions.

General: Enter high IV expected to fall. Positive theta, negative vega. Defined risks. Tie recommendations to this.
"""

async def call_ai(model, prompt, system_context=FRAMEWORK_CONTEXT):
    print(f"[callAI] Starting call for model: {model}")

    if model == 'grok':
        client = XAIClient(api_key=os.getenv('GROK_API_KEY'))
        # Define tools using SDK (automatically gets schemas)
        web_search = client.tools.web_search
        code_execution = client.tools.code_execution
        x_keyword_search = client.tools.x_keyword_search  # Matches your X search needs

        # Prepare messages (SDK uses 'input' as list of dicts)
        input_messages = [
            {"role": "system", "content": system_context or "You are a helpful trading assistant."},
            {"role": "user", "content": prompt or "Provide a quick test response."}
        ]

        # Create response with tools (server-side execution)
        response = client.responses.create(
            model="grok-4-1-fast",  # Or 'grok-4-1-fast' if available; check your access
            input=input_messages,
            tools=[web_search.schema, code_execution.schema, x_keyword_search.schema],
            tool_choice="auto",
            parallel_tool_calls=True,
            store=False,
            temperature=0.7,
            max_tokens=1024
        )
        print(f"[callAI] Success - Grok response received")
        return response.content  # SDK returns resolved content after tools

    elif model == 'openai':
        url = 'https://api.openai.com/v1/chat/completions'
        headers = {'Authorization': f"Bearer {os.getenv('OPENAI_API_KEY')}", 'Content-Type': 'application/json'}
        body = {
            "model": "gpt-4o-search-preview",
            "messages": [
                {"role": "system", "content": system_context},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 1024
        }
        print(f"[callAI] Sending request to {url}")
        print(f"[callAI] Request body: {body}")
        resp = requests.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']

    elif model == 'gemini':
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent?key={os.getenv('GEMINI_API_KEY')}"
        headers = {'Content-Type': 'application/json'}
        body = {
            "contents": [{"parts": [{"text": f"{system_context}\n\n{prompt}"}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024}
        }
        print(f"[callAI] Sending request to {url}")
        print(f"[callAI] Request body: {body}")
        resp = requests.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()['candidates'][0]['content']['parts'][0]['text']

    else:
        raise ValueError('Invalid model selected')

async def show_typing(context: CallbackContext, chat_id: int):
    while True:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(4)

async def send_response(update: Update, result: str):
    if len(result) > 4000:
        buffer = io.BytesIO(result.encode('utf-8'))
        buffer.name = 'response.txt'
        await update.message.reply_document(document=buffer, caption='Response is long — sent as file.')
    else:
        await update.message.reply_markdown(result)

async def setmodel(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        await update.message.reply_text('Invalid model. Use /setmodel grok, openai, or gemini.')
        return
    new_model = args[0].lower()
    if new_model in ['grok', 'openai', 'gemini']:
        user_models[update.message.chat_id] = new_model
        await update.message.reply_text(f"Model set to {new_model}. Web search enabled where supported.")
    else:
        await update.message.reply_text('Invalid model. Use /setmodel grok, openai, or gemini.')

async def scan(update: Update, context: CallbackContext):
    model = user_models.get(update.message.chat_id, 'grok')
    args = context.args
    strategy = args[0] if args else 'bull_put_spread'
    tickers = ' '.join(args[1:]) if len(args) > 1 else 'SOFI PLTR HOOD'

    typing_task = asyncio.create_task(show_typing(context, update.message.chat_id))
    prompt = f"Run scan for {strategy} opportunities on {tickers}. Use web search or tools for real-time options data.\nCriteria: 30-45 DTE, OTM short strike, net credit >$0.50, annualized ROC >6%, positive theta, negative vega.\nInclude max profit/loss, breakeven, risk notes. Output as markdown table. Incorporate X/web sentiment."
    
    try:
        result = await call_ai(model, prompt)
        await send_response(update, result)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
    finally:
        typing_task.cancel()

async def manage(update: Update, context: CallbackContext):
    model = user_models.get(update.message.chat_id, 'grok')
    position = ' '.join(context.args) if context.args else 'CSP on SOFI at $8 strike, net credit $0.67'

    typing_task = asyncio.create_task(show_typing(context, update.message.chat_id))
    prompt = f"Manage position: {position}. Use web search for current market data.\nRecommend: close (if >50% profit), roll (if needed), or hold.\nCalculate updated P/L, breakeven, risks. Tie to 'be the casino' mindset."
    
    try:
        result = await call_ai(model, prompt)
        await send_response(update, result)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
    finally:
        typing_task.cancel()

async def sentiment(update: Update, context: CallbackContext):
    model = user_models.get(update.message.chat_id, 'grok')
    sector = ' '.join(context.args) if context.args else 'tech stocks'

    typing_task = asyncio.create_task(show_typing(context, update.message.chat_id))
    prompt = f"Analyze market sentiment on X/web for '{sector}' (e.g., tech, value, high beta stocks).\nUse web/X search tools to fetch recent posts/data (last 7 days, from finance sources).\nClassify as bullish/neutral/bearish (with % breakdown), summarize key themes.\nRelate to options strategies: e.g., bullish = good for put credit spreads.\nRepresent diverse viewpoints."
    
    try:
        result = await call_ai(model, prompt)
        await send_response(update, result)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
    finally:
        typing_task.cancel()

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text('Welcome to HerculesTradingBot! Commands: /scan, /manage, /sentiment, /setmodel [grok|openai|gemini]. Default model: grok. Web search integrated!')

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('setmodel', setmodel))
    application.add_handler(CommandHandler('scan', scan))
    application.add_handler(CommandHandler('manage', manage))
    application.add_handler(CommandHandler('sentiment', sentiment))

    # Graceful stop
    def stop(signal, frame):
        print("Stopping bot...")
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    application.run_polling()

if __name__ == '__main__':
    main()