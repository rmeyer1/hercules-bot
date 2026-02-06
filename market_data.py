import logging
from datetime import datetime
from typing import Dict, List

import yfinance as yf

logger = logging.getLogger(__name__)


def get_market_data(ticker_symbol: str) -> Dict[str, str]:
    try:
        ticker = yf.Ticker(ticker_symbol)

        price = "N/A"
        try:
            price = ticker.fast_info.last_price
        except Exception:
            try:
                hist = ticker.history(period="1d")
                if not hist.empty:
                    price = hist["Close"].iloc[-1]
            except Exception as e:
                logger.warning("Price fetch failed for %s: %s", ticker_symbol, e)

        if isinstance(price, (int, float)):
            price = f"{price:.2f}"

        next_earnings = "Unknown"
        try:
            earnings_df = ticker.earnings_dates
            if earnings_df is not None and not earnings_df.empty:
                future_dates = earnings_df.index[earnings_df.index.tz_localize(None) > datetime.now()]

                if not future_dates.empty:
                    next_earnings = future_dates.min().strftime('%Y-%m-%d')
                else:
                    cal = ticker.calendar
                    if cal and not cal.empty:
                        next_earnings = cal.iloc[0, 0].strftime('%Y-%m-%d')
        except Exception as e:
            logger.warning("Earnings fetch failed for %s: %s", ticker_symbol, e)

        info = {}
        try:
            info = ticker.info
        except Exception:
            pass

        return {
            "price": price,
            "earnings": next_earnings,
            "iv_hint": info.get("beta", "N/A"),
            "sector": info.get("sector", "Unknown"),
            "dma_50": info.get("fifty_day_average", "N/A"),
            "dma_200": info.get("two_hundred_day_average", "N/A"),
        }

    except Exception as e:
        logger.error("⚠️ Market Data Crash for %s: %s", ticker_symbol, e, exc_info=True)
        return {"price": "N/A", "earnings": "Check Broker", "iv_hint": "N/A", "sector": "Unknown"}


def normalize_tickers(tokens: List[str]) -> List[str]:
    normalized = []
    for token in tokens:
        for part in token.split(','):
            cleaned = part.strip().upper()
            if cleaned:
                normalized.append(cleaned)
    return normalized


def is_ticker_like(token: str) -> bool:
    token = token.strip().upper().strip(',')
    if not token:
        return False
    cleaned = token.replace('.', '').replace('-', '')
    return cleaned.isalnum() and token == token.upper() and len(cleaned) <= 6


def derive_sectors_for_tickers(tickers: List[str]) -> Dict[str, str]:
    sector_map: Dict[str, str] = {}
    for ticker in tickers:
        data = get_market_data(ticker)
        sector = data.get("sector") or "Unknown"
        if sector == "Unknown":
            logger.info("Sector not found for ticker %s", ticker)
        sector_map[ticker] = sector
    return sector_map
