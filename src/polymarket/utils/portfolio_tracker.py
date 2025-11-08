"""Portfolio value tracking and conviction calculation for Polymarket traders."""

import threading
import time
from typing import Dict, Optional, Tuple, Callable
import requests


class PortfolioTracker:
    """Manages portfolio value caching and conviction calculation."""

    def __init__(
        self,
        data_api_base: str,
        session: requests.Session,
        cache_ttl_seconds: int = 3600,
        invalidation_threshold: float = 0.10,
        verbose: bool = False,
        logger: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize portfolio tracker.

        Args:
            data_api_base: Base URL for Polymarket Data API
            session: Requests session for API calls
            cache_ttl_seconds: Cache time-to-live (default: 1 hour)
            invalidation_threshold: Bet size % that invalidates cache (default: 10%)
            verbose: Enable verbose logging
            logger: Optional logging function
        """
        self.data_api_base = data_api_base
        self.session = session
        self.cache_ttl_seconds = cache_ttl_seconds
        self.invalidation_threshold = invalidation_threshold
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)

        # Cache: wallet_address -> {value: float, fetched_at: float}
        self.cache: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

        # Conviction thresholds (configurable)
        self.conviction_extreme_pct = 10.0
        self.conviction_high_pct = 5.0
        self.conviction_medium_pct = 2.0
        self.conviction_low_pct = 0.5
        self.conviction_hysteresis_pct = 0.2

    def get_portfolio_value(self, wallet_address: str) -> Optional[float]:
        """
        Get portfolio value with caching.

        Returns cached value if fresh, otherwise fetches from API.
        """
        current_time = time.time()

        # Check cache first (thread-safe read)
        with self._lock:
            if wallet_address in self.cache:
                cache_entry = self.cache[wallet_address]
                fetched_at = cache_entry.get("fetched_at", 0)
                age = current_time - fetched_at

                if age < self.cache_ttl_seconds:
                    if self.verbose:
                        self.logger(
                            f"[PORTFOLIO] Cache hit for {wallet_address[:10]}... (age: {int(age)}s)"
                        )
                    return cache_entry.get("value")

        # Cache miss or expired - fetch from API
        value = self._fetch_from_api(wallet_address)

        # Update cache (thread-safe write)
        if value is not None:
            with self._lock:
                self.cache[wallet_address] = {
                    "value": value,
                    "fetched_at": current_time,
                }

        return value

    def _fetch_from_api(self, wallet_address: str) -> Optional[float]:
        """Fetch portfolio value from Data API."""
        try:
            url = f"{self.data_api_base}/positions"
            response = self.session.get(
                url, params={"user": wallet_address}, timeout=10
            )
            response.raise_for_status()

            positions = response.json()

            # Calculate total portfolio value from positions
            # Each position has a currentValue field in USDC
            portfolio_value = 0.0
            if positions and isinstance(positions, list):
                for position in positions:
                    value_str = position.get("currentValue", "0")
                    try:
                        portfolio_value += float(value_str)
                    except (ValueError, TypeError):
                        continue

            if self.verbose:
                self.logger(
                    f"[PORTFOLIO] Fetched {wallet_address[:10]}...: ${portfolio_value:.2f} ({len(positions) if positions else 0} positions)"
                )

            return portfolio_value if portfolio_value > 0 else None

        except requests.RequestException as e:
            if self.verbose:
                self.logger(f"[PORTFOLIO ERROR] {wallet_address[:10]}...: {e}")
            return None
        except (ValueError, KeyError, TypeError) as e:
            if self.verbose:
                self.logger(f"[PORTFOLIO PARSE ERROR] {wallet_address[:10]}...: {e}")
            return None

    def invalidate_cache(self, wallet_address: str):
        """Manually invalidate cache for wallet."""
        with self._lock:
            if wallet_address in self.cache:
                del self.cache[wallet_address]
                if self.verbose:
                    self.logger(
                        f"[PORTFOLIO] Cache invalidated for {wallet_address[:10]}..."
                    )

    def should_invalidate_for_bet(
        self, wallet_address: str, bet_size_usdc: float
    ) -> bool:
        """
        Check if bet is large enough to invalidate cache.

        Returns True if bet is >10% of cached portfolio (indicates deposit/withdrawal).
        """
        with self._lock:
            if wallet_address not in self.cache:
                return False

            cached_value = self.cache[wallet_address].get("value", 0)
            if cached_value <= 0:
                return False

            return (bet_size_usdc / cached_value) > self.invalidation_threshold

    def calculate_conviction(
        self, bet_size_usdc: float, portfolio_value: float, last_conviction: str = ""
    ) -> Tuple[str, float]:
        """
        Calculate conviction level from bet size relative to portfolio.

        Includes hysteresis to prevent label flapping near thresholds.

        Returns:
            Tuple of (conviction_label, percentage)
        """
        if portfolio_value <= 0:
            return ("UNKNOWN", 0.0)

        pct = (bet_size_usdc / portfolio_value) * 100

        # Base thresholds with hysteresis
        if pct >= self.conviction_extreme_pct + self.conviction_hysteresis_pct:
            new_label = "EXTREME"
        elif pct >= self.conviction_high_pct + self.conviction_hysteresis_pct:
            new_label = "HIGH"
        elif pct >= self.conviction_medium_pct + self.conviction_hysteresis_pct:
            new_label = "MEDIUM"
        elif pct >= self.conviction_low_pct + self.conviction_hysteresis_pct:
            new_label = "LOW"
        else:
            new_label = "MINIMAL"

        # If we have a last conviction and we're near a boundary, keep the old label
        # unless we clearly crossed (prevents LOW <-> MEDIUM bouncing at 2.0%)
        if last_conviction and last_conviction != "UNKNOWN":
            thresholds = {
                "EXTREME": self.conviction_extreme_pct,
                "HIGH": self.conviction_high_pct,
                "MEDIUM": self.conviction_medium_pct,
                "LOW": self.conviction_low_pct,
                "MINIMAL": 0.0,
            }

            old_threshold = thresholds.get(last_conviction, 0.0)

            # If we're within the deadband of the old threshold, keep old label
            if abs(pct - old_threshold) <= self.conviction_hysteresis_pct:
                new_label = last_conviction

        return (new_label, pct)

    def get_cache_stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        with self._lock:
            return {
                "cached_wallets": len(self.cache),
                "ttl_seconds": self.cache_ttl_seconds,
            }

    def export_cache_for_persistence(self) -> Dict[str, Dict[str, float]]:
        """Export cache state for external persistence (only fresh entries)."""
        current_time = time.time()

        with self._lock:
            # Only export entries still within TTL
            fresh_cache = {}
            for wallet, cache_entry in self.cache.items():
                fetched_at = cache_entry.get("fetched_at", 0)
                age = current_time - fetched_at

                if age < self.cache_ttl_seconds:
                    fresh_cache[wallet] = cache_entry

            return fresh_cache

    def load_cache_from_persistence(self, cache_data: Dict[str, Dict[str, float]]):
        """Load cache state from external persistence with TTL validation."""
        current_time = time.time()

        with self._lock:
            self.cache.clear()

            for wallet, cache_entry in cache_data.items():
                fetched_at = cache_entry.get("fetched_at", 0)
                age = current_time - fetched_at

                # Only load if still within TTL
                if age < self.cache_ttl_seconds:
                    self.cache[wallet] = cache_entry
