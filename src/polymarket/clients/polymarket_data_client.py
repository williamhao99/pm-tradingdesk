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

    def fetch_market(self, condition_id: str) -> Optional[Dict]:
        """Fetch market by condition ID."""
        url = f"{self.base_url}/markets/{condition_id}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            market = response.json()

            if self.verbose:
                self.logger(f"[API] Fetched market {condition_id}")

            return market

        except requests.HTTPError as e:
            self.logger(
                f"[API ERROR] HTTP {e.response.status_code if e.response else 'Unknown'} for market {condition_id}"
            )
            return None
        except requests.RequestException as e:
            self.logger(f"[API ERROR] Request failed for market {condition_id}: {e}")
            return None
        except Exception as e:
            self.logger(f"[API ERROR] Unexpected error for market {condition_id}: {e}")
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

    def get_stats(self) -> Dict:
        """Get client statistics."""
        return {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "session_active": self.session is not None,
        }
