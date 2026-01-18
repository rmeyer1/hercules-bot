// bot.js - Telegram Bot for Options Trading with Multi-Model Support (Grok, Gemini, OpenAI) + Web Search
// Requires: Node.js, npm install telegraf dotenv axios
// Setup: Create .env with TELEGRAM_TOKEN, GROK_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY
// Run: node bot.js

require('dotenv').config();
const { Telegraf } = require('telegraf');
const axios = require('axios');
const { Buffer } = require('buffer'); // For sending text as file

const bot = new Telegraf(process.env.TELEGRAM_TOKEN);

// Store user model preferences (per chat ID)
const userModels = new Map(); // Default to 'grok'

// Framework context (condensed from your docs)
const FRAMEWORK_CONTEXT = `
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
`;

// Function to call the selected API with web search support
async function callAI(model, prompt, systemContext = FRAMEWORK_CONTEXT) {
  let url, headers, body;

  if (model === 'grok') {
    url = 'https://api.x.ai/v1/chat/completions';
    headers = { Authorization: `Bearer ${process.env.GROK_API_KEY}`, 'Content-Type': 'application/json' };
    body = {
      model: 'grok-4-1-fast-reasoning',
      messages: [
        { role: 'system', content: systemContext },
        { role: 'user', content: prompt },
      ],
      tools: [{ type: 'x_search' }, { type: 'code_execution' }, { type: 'web_search' }], // Enable web search
    };
  } else if (model === 'openai') {
    url = 'https://api.openai.com/v1/chat/completions';
    headers = { Authorization: `Bearer ${process.env.OPENAI_API_KEY}`, 'Content-Type': 'application/json' };
    body = {
      model: 'gpt-4o-search-preview', // Use search-enabled model for web access
      messages: [
        { role: 'system', content: systemContext },
        { role: 'user', content: prompt },
      ],
    };
  } else if (model === 'gemini') {
    url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent?key=${process.env.GEMINI_API_KEY}`;
    headers = { 'Content-Type': 'application/json' };
    body = {
      contents: [
        { parts: [{ text: `${systemContext}\n\n${prompt}` }] },
      ],
      grounding: { source: 'GOOGLE_SEARCH' }, // Enable built-in web search
    };
    // Gemini response format differs; adjust parsing below
  } else {
    throw new Error('Invalid model selected');
  }

  const response = await axios.post(url, body, { headers });
  
  if (model === 'gemini') {
    return response.data.candidates[0].content.parts[0].text;
  } else {
    return response.data.choices[0].message.content;
  }
}

// Helper to show typing indicator every ~4 seconds (Telegram resets after ~5 s)
async function showTyping(ctx, stopSignal = { stop: false }) {
  const chatId = ctx.chat.id;

  const interval = setInterval(async () => {
    if (stopSignal.stop) {
      clearInterval(interval);
      return;
    }
    try {
      await ctx.telegram.sendChatAction(chatId, 'typing');
    } catch (err) {
      // ignore errors (user might have blocked bot, left chat, etc.)
    }
  }, 4000); // Telegram timeout is ~5 seconds

  return () => {
    stopSignal.stop = true;
    clearInterval(interval);
  };
}

// Helper to send response (as markdown or file if too long)
async function sendResponse(ctx, result, stopTyping) {
  stopTyping();
  if (result.length > 4000) { // Telegram max message length ~4096
    // Send as .txt file
    const buffer = Buffer.from(result, 'utf-8');
    await ctx.replyWithDocument({ source: buffer, filename: 'response.txt' }, { caption: 'Response is long—sent as file for better reading.' });
  } else {
    await ctx.replyWithMarkdown(result);
  }
}

// Command: /setmodel [grok|openai|gemini]
bot.command('setmodel', async (ctx) => {
  const args = ctx.message.text.split(' ').slice(1);
  const newModel = args[0]?.toLowerCase();
  if (['grok', 'openai', 'gemini'].includes(newModel)) {
    userModels.set(ctx.chat.id, newModel);
    await ctx.reply(`Model set to ${newModel}. Web search enabled where supported.`);
  } else {
    await ctx.reply('Invalid model. Use /setmodel grok, openai, or gemini.');
  }
});

// Command: /scan [strategy] [tickers...]
bot.command('scan', async (ctx) => {
  const model = userModels.get(ctx.chat.id) || 'grok';
  const args = ctx.message.text.split(' ').slice(1);
  const strategy = args[0] || 'bull_put_spread';
  const tickers = args.slice(1).join(' ') || 'SOFI PLTR HOOD';

  // Show typing indicator immediately
  const stopTyping = await showTyping(ctx);

  let prompt = `Run scan for ${strategy} opportunities on ${tickers}. Use web search or tools for real-time options data.
Criteria: 30-45 DTE, OTM short strike, net credit >$0.50, annualized ROC >6%, positive theta, negative vega.
Include max profit/loss, breakeven, risk notes. Output as markdown table. Incorporate X/web sentiment.`;
  
  try {
    const result = await callAI(model, prompt);
    await sendResponse(ctx, result, stopTyping);
  } catch (error) {
    stopTyping();
    await ctx.reply(`Error: ${error.message}`);
  }
});

// Command: /manage [position details...]
bot.command('manage', async (ctx) => {
  const model = userModels.get(ctx.chat.id) || 'grok';
  const position = ctx.message.text.split(' ').slice(1).join(' ') || 'CSP on SOFI at $8 strike, net credit $0.67';

  // Show typing indicator immediately
  const stopTyping = await showTyping(ctx);

  let prompt = `Manage position: ${position}. Use web search for current market data.
Recommend: close (if >50% profit), roll (if needed), or hold.
Calculate updated P/L, breakeven, risks. Tie to 'be the casino' mindset.`;
  
  try {
    const result = await callAI(model, prompt);
    await sendResponse(ctx, result, stopTyping);
  } catch (error) {
    stopTyping();
    await ctx.reply(`Error: ${error.message}`);
  }
});

// Command: /sentiment [sector...]
bot.command('sentiment', async (ctx) => {
  const model = userModels.get(ctx.chat.id) || 'grok';
  const sector = ctx.message.text.split(' ').slice(1).join(' ') || 'tech stocks';

  // Show typing indicator immediately
  const stopTyping = await showTyping(ctx);

  let prompt = `Analyze market sentiment on X/web for '${sector}' (e.g., tech, value, high beta stocks).
Use web/X search tools to fetch recent posts/data (last 7 days, from finance sources).
Classify as bullish/neutral/bearish (with % breakdown), summarize key themes.
Relate to options strategies: e.g., bullish = good for put credit spreads.
Represent diverse viewpoints.`;
  
  try {
    const result = await callAI(model, prompt);
    await sendResponse(ctx, result, stopTyping);
  } catch (error) {
    stopTyping();
    await ctx.reply(`Error: ${error.message}`);
  }
});

// Start command for welcome
bot.start((ctx) => {
  ctx.reply('Welcome to OptionsCasinoBot! Commands: /scan, /manage, /sentiment, /setmodel [grok|openai|gemini]. Default model: grok. Web search integrated!');
});

bot.launch();
console.log('Bot is running...');

// Handle graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));