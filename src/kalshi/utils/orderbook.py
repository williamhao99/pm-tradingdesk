"""Local orderbook state management for Kalshi markets."""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OrderbookLevel:
    """Single price level in the orderbook."""

    price: int  # cents (1-99)
    size: int  # contracts


@dataclass
class Orderbook:
    """Local orderbook state for a Kalshi market."""

    ticker: str
    yes_bids: List[OrderbookLevel] = field(default_factory=list)  # sorted desc by price
    no_bids: List[OrderbookLevel] = field(default_factory=list)  # sorted desc by price
    last_seq: Optional[int] = None
    last_update: float = 0.0

    def best_yes_bid(self) -> Optional[int]:
        return self.yes_bids[0].price if self.yes_bids else None

    def best_yes_ask(self) -> Optional[int]:
        return 100 - self.no_bids[0].price if self.no_bids else None

    def best_no_bid(self) -> Optional[int]:
        return self.no_bids[0].price if self.no_bids else None

    def best_no_ask(self) -> Optional[int]:
        return 100 - self.yes_bids[0].price if self.yes_bids else None

    def yes_bid_size(self) -> int:
        return self.yes_bids[0].size if self.yes_bids else 0

    def yes_ask_size(self) -> int:
        return self.no_bids[0].size if self.no_bids else 0

    def no_bid_size(self) -> int:
        return self.no_bids[0].size if self.no_bids else 0

    def no_ask_size(self) -> int:
        return self.yes_bids[0].size if self.yes_bids else 0

    def spread(self) -> Optional[int]:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        return ask - bid if bid is not None and ask is not None else None

    def midpoint(self) -> Optional[float]:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        return (bid + ask) / 2.0 if bid is not None and ask is not None else None

    def apply_snapshot(
        self, yes_levels: List[List[int]], no_levels: List[List[int]], seq: int
    ) -> None:
        """Apply full orderbook snapshot."""
        self.yes_bids = [OrderbookLevel(p, s) for p, s in yes_levels if s > 0]
        self.no_bids = [OrderbookLevel(p, s) for p, s in no_levels if s > 0]
        self.yes_bids.sort(key=lambda x: -x.price)
        self.no_bids.sort(key=lambda x: -x.price)
        self.last_seq = None  # Reset - delta stream has its own sequence
        self.last_update = time.time()

    def apply_delta(self, side: str, price: int, delta: int, seq: int) -> bool:
        """Apply incremental orderbook update. Returns False if sequence gap detected."""
        if self.last_seq is not None and seq != self.last_seq + 1:
            logger.warning(
                f"Sequence gap for {self.ticker}: expected {self.last_seq + 1}, got {seq}"
            )
            self.last_update = 0  # Mark stale
            return False

        levels = self.yes_bids if side == "yes" else self.no_bids

        # Find or create the price level
        found = False
        for level in levels:
            if level.price == price:
                level.size += delta
                if level.size <= 0:
                    levels.remove(level)
                found = True
                break

        if not found and delta > 0:
            levels.append(OrderbookLevel(price, delta))
            levels.sort(key=lambda x: -x.price)

        self.last_seq = seq
        self.last_update = time.time()
        return True

    def is_stale(self, max_age_seconds: float = 1.0) -> bool:
        return time.time() - self.last_update > max_age_seconds

    def has_liquidity(self) -> bool:
        return bool(self.yes_bids) and bool(self.no_bids)

    def __str__(self) -> str:
        yes_bid = self.best_yes_bid()
        yes_ask = self.best_yes_ask()
        spread = self.spread()
        return (
            f"{self.ticker}: YES {yes_bid or '?'}/{yes_ask or '?'} "
            f"(spread={spread or '?'}c)"
        )
