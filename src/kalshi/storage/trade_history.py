"""Simple SQLite storage for portfolio history."""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TradeHistory:
    """Simple portfolio value tracking over time."""

    def __init__(self, db_path: str = "data/kalshi/trading_data.db"):
        self.db_path = Path(db_path)
        # Ensure data directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with portfolio snapshots table."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cash_cents INTEGER NOT NULL,
                    positions_value_cents INTEGER NOT NULL,
                    total_value_cents INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Index for time-based queries
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
                ON portfolio_snapshots(timestamp)
            """
            )

            conn.commit()
            logger.info(f"Portfolio history database initialized: {self.db_path}")

    def save_snapshot(self, cash_cents: int, positions_value_cents: int) -> bool:
        """
        Save a portfolio snapshot.

        Args:
            cash_cents: Current cash balance in cents
            positions_value_cents: Current position value in cents

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            total_value_cents = cash_cents + positions_value_cents
            timestamp = datetime.now(timezone.utc).isoformat()

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO portfolio_snapshots (
                        timestamp, cash_cents, positions_value_cents, total_value_cents
                    ) VALUES (?, ?, ?, ?)
                """,
                    (timestamp, cash_cents, positions_value_cents, total_value_cents),
                )
                conn.commit()
                return True

        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")
            return False

    def get_analytics(self, days: Optional[int] = 30) -> Dict:
        """
        Get portfolio analytics.

        Args:
            days: Number of days to include (default 30)

        Returns:
            Dict with portfolio history and stats
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            history = conn.execute(
                """
                SELECT
                    timestamp,
                    total_value_cents,
                    cash_cents,
                    positions_value_cents
                FROM portfolio_snapshots
                WHERE timestamp > datetime('now', '-' || ? || ' days')
                ORDER BY timestamp ASC
            """,
                (days,),
            ).fetchall()

            if history:
                current = history[-1]
                first = history[0]

                all_values = [row["total_value_cents"] for row in history]
                max_value = max(all_values)
                min_value = min(all_values)

                current_value = current["total_value_cents"]
                start_value = first["total_value_cents"]
                change_cents = current_value - start_value
                change_pct = (
                    ((change_cents / start_value) * 100) if start_value > 0 else 0
                )

                return {
                    "current": {
                        "total_value_cents": current_value,
                        "cash_cents": current["cash_cents"],
                        "positions_value_cents": current["positions_value_cents"],
                    },
                    "stats": {
                        "change_cents": change_cents,
                        "change_percent": round(change_pct, 2),
                        "high_cents": max_value,
                        "low_cents": min_value,
                        "snapshots_count": len(history),
                    },
                    "history": [
                        {
                            "timestamp": row["timestamp"],
                            "value_cents": row["total_value_cents"],
                        }
                        for row in history
                    ],
                }
            else:
                return {
                    "current": {
                        "total_value_cents": 0,
                        "cash_cents": 0,
                        "positions_value_cents": 0,
                    },
                    "stats": {
                        "change_cents": 0,
                        "change_percent": 0,
                        "high_cents": 0,
                        "low_cents": 0,
                        "snapshots_count": 0,
                    },
                    "history": [],
                }

    def get_snapshot_count(self) -> int:
        """Get total number of snapshots stored."""
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()
            return result[0] if result else 0

    def has_snapshot_today(self) -> bool:
        """Check if a snapshot already exists for today."""
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                """
                SELECT COUNT(*) FROM portfolio_snapshots
                WHERE DATE(timestamp) = DATE('now')
            """
            ).fetchone()
            return result[0] > 0 if result else False
