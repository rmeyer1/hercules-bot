import logging
import os
from datetime import datetime
from typing import Dict, List, Tuple

import requests
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

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

user_models: Dict[int, str] = {}


def set_user_model(chat_id: int, model: str) -> None:
    user_models[chat_id] = model


def resolve_model(chat_id: int, command: str) -> str:
    """
    Route to the appropriate model for each command.
    Gemini handles analysis/scanning with Google Search grounding.
    Grok is reserved for X/Twitter sentiment.
    """
    if command == 'sentiment':
        return 'grok'
    if command in ('scan', 'manage', 'manageid'):
        return 'gemini'
    return user_models.get(chat_id, 'gemini')


def _extract_response_text(response) -> str:
    """
    Some Gemini responses (especially tool calls) may not populate .text directly.
    This helper stitches together any text parts so we always return something user-visible.
    """
    if getattr(response, "text", None):
        return response.text

    text_parts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                text_parts.append(part_text)
    return "\n".join(text_parts).strip()


def build_ticker_sentiment_prompt(tickers: List[str], sector_map: Dict[str, str]) -> str:
    sectors_lines = "\n".join([f"- {t}: {sector_map.get(t, 'Unknown')}" for t in tickers])
    unique_sectors = [s for s in dict.fromkeys(sector_map.values())]
    aggregate = ", ".join(unique_sectors) if unique_sectors else "Unknown"
    return (
        f"Tickers analyzed: {', '.join(tickers)}\n\n"
        f"Derived sectors:\n{sectors_lines}\n\n"
        f"Aggregate sector exposure: {aggregate}\n\n"
        "Consider both ticker-specific sentiment and broader sector-level tailwinds/headwinds. "
        "Describe how the tone differs by ticker/sector and note any contrarian or risk signals shaping psychology."
    )


def build_manage_prompt(trade: Dict, market: Dict) -> str:
    # Detect if this is a spread (has long_strike)
    long_strike = trade.get('long_strike')
    
    if long_strike:
        # It's a spread - calculate max risk
        spread_width = abs(trade['strike'] - long_strike)
        max_risk = spread_width - trade['entry_price']  # width - credit received
        entry_info = (
            f"Position: {trade['type']} Credit Spread @ Short Strike: ${trade['strike']} "
            f"/ Long Strike: ${long_strike} (Width: ${spread_width:.2f}). "
            f"Net Premium Collected: ${trade['entry_price']}. "
            f"Max Risk: ${max_risk:.2f} per spread. "
            f"Expiry: {trade['expiry']} (Opened: {trade['date']})"
        )
    else:
        # Single leg (CSP or CC)
        entry_info = (
            f"Position: {trade['type']} @ Strike: ${trade['strike']}. "
            f"Premium Collected: ${trade['entry_price']}. "
            f"Expiry: {trade['expiry']} (Opened: {trade['date']})"
        )

    return (
        f"Manage {trade['ticker']}. {entry_info}. Current Market Price: ${market['price']}. "
        f"Today: {datetime.now().strftime('%Y-%m-%d')}. "
        f"Calculate current profit/loss based on decay. "
        f"Evaluate 50% profit target and provide Net Credit Roll advice."
    )


async def call_ai(model: str, prompt: str, system_context: str = FRAMEWORK_CONTEXT, task_type: str = "speed") -> Tuple[str, List[str]]:
    citations: List[str] = []

    if model == 'grok':
        from xai_sdk import Client
        from xai_sdk.chat import user, system
        from xai_sdk.tools import web_search, code_execution, x_search

        api_key = os.getenv('XAI_API_KEY') or os.getenv('GROK_API_KEY')
        if not api_key:
            raise ValueError("XAI_API_KEY or GROK_API_KEY not set.")

        client = Client(api_key=api_key)

        chat_kwargs = {
            "model": "grok-4-1-fast",
            "tools": [web_search(), code_execution(), x_search()],
            "include": ["inline_citations"]
        }

        chat = client.chat.create(**chat_kwargs)
        chat.append(system(system_context))
        chat.append(user(prompt))

        final_response = None
        content_parts: List[str] = []

        try:
            for response, chunk in chat.stream():
                final_response = response
                chunk_text = getattr(chunk, "content", None)
                if chunk_text:
                    content_parts.append(chunk_text)
        except AttributeError:
            logger.info("xai_sdk chat object does not support streaming; falling back to sample().")
            final_response = chat.sample()
            fallback_content = getattr(final_response, "content", "") or ""
            if fallback_content:
                content_parts.append(fallback_content)

        content = "".join(content_parts).strip()
        if not content and final_response:
            content = getattr(final_response, "content", "") or ""

        raw_citations = getattr(final_response, "citations", None) or []
        for entry in raw_citations:
            url = entry if isinstance(entry, str) else getattr(entry, "url", None)
            if url:
                citations.append(url)

        return content, citations

    if model == 'openai':
        url = 'https://api.openai.com/v1/chat/completions'
        headers = {'Authorization': f"Bearer {os.getenv('OPENAI_API_KEY')}", 'Content-Type': 'application/json'}
        body = {"model": "gpt-4o", "messages": [{"role": "system", "content": system_context}, {"role": "user", "content": prompt}]}
        content = requests.post(url, headers=headers, json=body).json()['choices'][0]['message']['content']
        return content, citations

    if model == 'gemini':
        try:
            api_key = os.getenv('GEMINI_API_KEY')
            if not api_key:
                raise ValueError("GEMINI_API_KEY not set in environment.")

            client = genai.Client(api_key=api_key)

            model_name = 'gemini-2.5-pro' if task_type == 'reasoning' else 'gemini-2.5-flash'
            temperature = 0.2 if task_type == 'reasoning' else 0.7

            config_kwargs = {
                "system_instruction": system_context,
                "tools": [types.Tool(google_search=types.GoogleSearch())],
                "temperature": temperature,
            }

            if task_type == 'reasoning':
                thinking_cls = getattr(types, "ThinkingConfig", None)
                if thinking_cls:
                    config_kwargs["thinking_config"] = thinking_cls()

            config = types.GenerateContentConfig(**config_kwargs)

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )

            result_text = _extract_response_text(response)
            content = result_text or "⚠️ AI Error: Empty response from Gemini."
            return content, citations
        except Exception as e:
            logger.error("Gemini API Error: %s", e)
            return f"⚠️ AI Error: {str(e)}", citations

    return "⚠️ Unsupported model selection.", citations
