import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def init_db() -> None:
    """Initialize the trades database and backfill schema additions."""
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    c.execute(
        '''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  ticker TEXT,
                  type TEXT,
                  strike REAL,
                  entry_price REAL,
                  date TEXT,
                  expiry TEXT,
                  status TEXT DEFAULT 'OPEN',
                  closed_date TEXT)'''
    )

    columns = {row[1] for row in c.execute("PRAGMA table_info(trades)")}
    if 'status' not in columns:
        c.execute("ALTER TABLE trades ADD COLUMN status TEXT DEFAULT 'OPEN'")
        c.execute("UPDATE trades SET status = COALESCE(status, 'OPEN')")
    if 'closed_date' not in columns:
        c.execute("ALTER TABLE trades ADD COLUMN closed_date TEXT")

    c.execute(
        """CREATE INDEX IF NOT EXISTS idx_trades_chat_ticker_status
                 ON trades (chat_id, ticker, status)"""
    )
    c.execute(
        """CREATE INDEX IF NOT EXISTS idx_trades_chat_status
                 ON trades (chat_id, status)"""
    )

    conn.commit()
    conn.close()


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
        c.execute(
            """SELECT id, ticker, type, strike, entry_price, date, expiry, status, closed_date
                     FROM trades
                     WHERE ticker=? AND chat_id=? AND status='OPEN'
                     ORDER BY id DESC""",
            (ticker, chat_id),
        )
    else:
        c.execute(
            """SELECT id, ticker, type, strike, entry_price, date, expiry, status, closed_date
                     FROM trades
                     WHERE chat_id=? AND status='OPEN'
                     ORDER BY id DESC""",
            (chat_id,),
        )
    rows = [row_to_dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_trade_by_id(trade_id: int, chat_id: int) -> Optional[Dict]:
    conn = sqlite3.connect('trades.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT id, ticker, type, strike, entry_price, date, expiry, status, closed_date
                 FROM trades
                 WHERE id=? AND chat_id=? AND status='OPEN'
                 LIMIT 1""",
        (trade_id, chat_id),
    )
    row = c.fetchone()
    conn.close()
    return row_to_dict(row) if row else None


def open_trade(chat_id: int, ticker: str, t_type: str, strike: float, premium: float, expiry: str) -> int:
    """Insert a new open trade and return its id."""
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    c.execute(
        """INSERT INTO trades (chat_id, ticker, type, strike, entry_price, date, expiry, status, closed_date)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL)""",
        (
            chat_id,
            ticker.upper(),
            t_type.upper(),
            float(strike),
            float(premium),
            datetime.now().strftime('%Y-%m-%d'),
            expiry,
        ),
    )
    conn.commit()
    trade_id = c.lastrowid
    conn.close()
    return trade_id
