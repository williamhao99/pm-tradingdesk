"""Local orderbook state management for Kalshi markets."""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Orderbook:
    """Local orderbook state for a Kalshi market.

    Uses Dict[price -> size] for O(1) delta updates instead of sorted lists.
    """

    ticker: str
    yes_bids: Dict[int, int] = field(default_factory=dict)  # price -> size
    no_bids: Dict[int, int] = field(default_factory=dict)  # price -> size
    last_seq: Optional[int] = None
    last_update: float = 0.0

    # Cached best prices for O(1) access
    _best_yes_bid: Optional[int] = field(default=None, repr=False)
    _best_no_bid: Optional[int] = field(default=None, repr=False)

    def _update_best_yes_bid(self) -> None:
        """Update cached best YES bid."""
        self._best_yes_bid = max(self.yes_bids.keys()) if self.yes_bids else None

    def _update_best_no_bid(self) -> None:
        """Update cached best NO bid."""
        self._best_no_bid = max(self.no_bids.keys()) if self.no_bids else None

    def best_yes_bid(self) -> Optional[int]:
        return self._best_yes_bid

    def best_yes_ask(self) -> Optional[int]:
        return 100 - self._best_no_bid if self._best_no_bid is not None else None

    def best_no_bid(self) -> Optional[int]:
        return self._best_no_bid

    def best_no_ask(self) -> Optional[int]:
        return 100 - self._best_yes_bid if self._best_yes_bid is not None else None

    def yes_bid_size(self) -> int:
        return self.yes_bids.get(self._best_yes_bid, 0) if self._best_yes_bid else 0

    def yes_ask_size(self) -> int:
        return self.no_bids.get(self._best_no_bid, 0) if self._best_no_bid else 0

    def no_bid_size(self) -> int:
        return self.no_bids.get(self._best_no_bid, 0) if self._best_no_bid else 0

    def no_ask_size(self) -> int:
        return self.yes_bids.get(self._best_yes_bid, 0) if self._best_yes_bid else 0

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
        self.yes_bids = {p: s for p, s in yes_levels if s > 0}
        self.no_bids = {p: s for p, s in no_levels if s > 0}
        self._update_best_yes_bid()
        self._update_best_no_bid()
        self.last_seq = None  # Reset - delta stream has its own sequence
        self.last_update = time.time()

    def apply_delta(self, side: str, price: int, delta: int, seq: int) -> bool:
        """Apply incremental orderbook update. Returns False if sequence gap detected."""
        if self.last_seq is not None and seq != self.last_seq + 1:
            self.last_update = 0  # Mark stale
            return False

        levels = self.yes_bids if side == "yes" else self.no_bids
        is_yes = side == "yes"

        if price in levels:
            levels[price] += delta
            if levels[price] <= 0:
                del levels[price]
                # Recalc best if we removed the best price
                if is_yes and price == self._best_yes_bid:
                    self._update_best_yes_bid()
                elif not is_yes and price == self._best_no_bid:
                    self._update_best_no_bid()
        elif delta > 0:
            levels[price] = delta
            # Update best if new price is better
            if is_yes:
                if self._best_yes_bid is None or price > self._best_yes_bid:
                    self._best_yes_bid = price
            else:
                if self._best_no_bid is None or price > self._best_no_bid:
                    self._best_no_bid = price

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
