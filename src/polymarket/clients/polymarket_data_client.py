"""Polymarket Data API client with retry logic."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import List, Dict, Optional, Callable


class PolymarketDataClient:
    """Client for Polymarket Data API with built-in retry logic."""

    def __init__(
        self,
        base_url: str = "https://data-api.polymarket.com",
        timeout: int = 10,
        max_retries: int = 5,
        backoff_factor: float = 0.5,
        verbose: bool = False,
        logger: Optional[Callable[[str], None]] = None,
    ):
        """Initialize Data API client with retry logic."""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)

        # Create session with retry logic
        self.session = self._create_session(max_retries, backoff_factor)

    def _create_session(
        self, max_retries: int, backoff_factor: float
    ) -> requests.Session:
        """Create session with retry logic."""
        session = requests.Session()

        retry = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )

        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        if self.verbose:
            self.logger(
                f"[API CLIENT] Initialized with {max_retries} retries, {backoff_factor}s backoff"
            )

        return session

    def fetch_recent_trades(
        self,
        wallet_address: str,
        limit: int = 50,
        sort_by: str = "TIMESTAMP",
        sort_direction: str = "DESC",
    ) -> List[Dict]:
        """Fetch recent trades for wallet."""
        url = f"{self.base_url}/activity"
        params = {
            "user": wallet_address,
            "limit": limit,
            "type": "TRADE",
            "sortBy": sort_by,
            "sortDirection": sort_direction,
        }

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()

            trades = response.json()

            if self.verbose:
                self.logger(
                    f"[API] Fetched {len(trades) if trades else 0} trades for {wallet_address[:10]}..."
                )

            return trades if trades else []

        except requests.HTTPError as e:
            self.logger(
                f"[API ERROR] HTTP {e.response.status_code if e.response else 'Unknown'} for {wallet_address[:10]}..."
            )
            return []
        except requests.RequestException as e:
            self.logger(f"[API ERROR] Request failed for {wallet_address[:10]}...: {e}")
            return []
        except Exception as e:
            self.logger(
                f"[API ERROR] Unexpected error for {wallet_address[:10]}...: {e}"
            )
            return []

    def fetch_positions(self, wallet_address: str) -> Optional[List[Dict]]:
        """Fetch positions for wallet."""
        url = f"{self.base_url}/positions"
        params = {"user": wallet_address}

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()

            positions = response.json()

            if self.verbose:
                self.logger(
                    f"[API] Fetched {len(positions) if positions else 0} positions for {wallet_address[:10]}..."
                )

            return positions if positions else []

        except requests.HTTPError as e:
            self.logger(
                f"[API ERROR] HTTP {e.response.status_code if e.response else 'Unknown'} for {wallet_address[:10]}..."
            )
            return None
        except requests.RequestException as e:
            self.logger(f"[API ERROR] Request failed for {wallet_address[:10]}...: {e}")
            return None
        except Exception as e:
            self.logger(
                f"[API ERROR] Unexpected error for {wallet_address[:10]}...: {e}"
            )
            return None

    def validate_wallet_address(self, wallet_address: str) -> bool:
        """Validate wallet address format."""
        if not wallet_address.startswith("0x"):
            raise ValueError(
                f"Invalid wallet address: {wallet_address} (must start with 0x)"
            )

        if len(wallet_address) != 42:
            raise ValueError(
                f"Wallet address must be 42 characters (got {len(wallet_address)})"
            )

        return True

    def close(self):
        """Close the session and cleanup resources."""
        try:
            if self.session:
                self.session.close()
                if self.verbose:
                    self.logger("[API CLIENT] Session closed")
        except Exception as e:
            self.logger(f"[API CLIENT ERROR] Error closing session: {e}")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def reconstruct_positions_from_api(self, wallet_address: str) -> Dict:
        """
        Reconstruct positions from the /positions API endpoint.

        This is more reliable than reconstructing from trades, as it captures
        ALL current positions regardless of how long ago they were opened.

        Returns dict with structure:
        {
            (market_slug, outcome): {
                "shares": float,
                "usdc": float,  # Estimated value based on current size
                "trade_count": 0  # Unknown from positions endpoint
            }
        }
        """
        positions_data = self.fetch_positions(wallet_address)

        if not positions_data:
            return {}

        positions = {}

        for position in positions_data:
            try:
                # Positions endpoint returns 'slug' for market identifier
                market_slug = position.get("slug")
                outcome = position.get("outcome")
                size = float(position.get("size", 0))
                initial_value = float(position.get("initialValue", 0))

                if not all([market_slug, outcome]) or size < 1.0:
                    continue

                # Normalize outcome to uppercase
                outcome = outcome.upper()

                # Create position key
                key = (market_slug, outcome)

                # Use actual investment amount from API
                positions[key] = {
                    "shares": size,
                    "usdc": initial_value,  # Actual USDC invested
                    "trade_count": 0  # Unknown from positions endpoint
                }

            except (ValueError, TypeError, KeyError) as e:
                self.logger(f"[API ERROR] Failed to parse position: {e}")
                continue

        if positions:
            self.logger(
                f"[API] Loaded {len(positions)} active positions from API"
            )

        return positions

    def reconstruct_positions_from_trades(
        self, wallet_address: str, limit: int = 1000
    ) -> Dict:
        """
        Reconstruct net positions from historical trades.

        Returns dict with structure:
        {
            (market_slug, outcome): {
                "shares": float,  # Net shares (BUY adds, SELL subtracts)
                "usdc": float,    # Net USDC invested
                "trade_count": int
            }
        }
        """
        trades = self.fetch_recent_trades(
            wallet_address, limit=limit, sort_by="TIMESTAMP", sort_direction="DESC"
        )

        if not trades:
            return {}

        positions = {}
        skipped_trades = 0

        for trade in trades:
            try:
                # API returns 'slug' for market identifier, not 'market'
                market_slug = trade.get("slug") or trade.get("market")
                outcome = trade.get("outcome")
                side = trade.get("side")
                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))

                if not all([market_slug, outcome, side]):
                    skipped_trades += 1
                    continue

                # Normalize outcome to uppercase
                outcome = outcome.upper()

                # Create position key
                key = (market_slug, outcome)

                if key not in positions:
                    positions[key] = {"shares": 0.0, "usdc": 0.0, "trade_count": 0}

                # Calculate USDC amount for this trade
                usdc_amount = size * price

                # Update net position (BUY adds, SELL subtracts)
                if side.upper() == "BUY":
                    positions[key]["shares"] += size
                    positions[key]["usdc"] += usdc_amount
                elif side.upper() == "SELL":
                    positions[key]["shares"] -= size
                    positions[key]["usdc"] -= usdc_amount

                positions[key]["trade_count"] += 1

            except (ValueError, TypeError, KeyError) as e:
                self.logger(f"[API ERROR] Failed to parse trade: {e}")
                skipped_trades += 1
                continue

        # Filter out closed positions (< 1 share)
        active_positions = {
            k: v for k, v in positions.items() if abs(v["shares"]) >= 1.0
        }

        if active_positions:
            self.logger(
                f"[API] Reconstructed {len(active_positions)} active positions from {len(trades)} trades"
            )

        return active_positions
