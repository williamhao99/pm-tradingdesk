"""Simplified async Kalshi client."""

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from cachetools import TTLCache
from dotenv import load_dotenv

from config.constants import (
    API_REQUEST_TIMEOUT,
    ERROR_NO_API_KEY,
    KALSHI_API_URL,
    MARKET_CACHE_TTL,
    MAX_POSITIONS_PER_PAGE,
    PROJECT_ROOT,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_MAX_ATTEMPTS,
)
from src.kalshi.auth import load_private_key, sign_request
from src.kalshi.tools.performance_monitor import get_monitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


class AsyncKalshiClient:
    """
    Simple async Kalshi client with caching and error handling.

    Features:
    - TTL-based caching for market metadata (titles, descriptions)
    - Exponential backoff retry logic
    - Automatic request signing
    """

    def __init__(self, private_key_path: str = "kalshi_private_key.pem"):
        self.api_key_id = os.getenv("KALSHI_API_KEY_ID")
        if not self.api_key_id:
            raise ValueError(ERROR_NO_API_KEY)

        key_file = PROJECT_ROOT / private_key_path
        try:
            self.private_key = load_private_key(key_file)
        except FileNotFoundError as exc:
            raise ValueError(f"Private key file not found: {key_file}") from exc

        self.base_url = KALSHI_API_URL

        self.session = None  # Lazy initialization for proper cleanup
        self._session_lock = asyncio.Lock()
        self.market_cache = TTLCache(maxsize=50, ttl=MARKET_CACHE_TTL)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session with lazy initialization."""
        async with self._session_lock:
            if self.session is None:
                timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
                self.session = aiohttp.ClientSession(timeout=timeout)
            return self.session

    async def close(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        max_retries: int = RETRY_MAX_ATTEMPTS,
    ) -> Dict:
        """Make authenticated request with exponential backoff retry."""
        start_time = time.time()
        last_error = None
        session = await self._get_session()

        for attempt in range(max_retries):
            timestamp = str(int(time.time() * 1000))
            # Prepend Kalshi API path prefix for signature
            full_path = f"/trade-api/v2{path}"
            signature = sign_request(self.private_key, timestamp, method, full_path)

            headers = {
                "KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": signature,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "Content-Type": "application/json",
            }

            url = f"{self.base_url}{path}"

            try:
                async with session.request(
                    method, url, headers=headers, json=data, params=params
                ) as response:
                    result = await response.json()
                    latency_ms = (time.time() - start_time) * 1000
                    get_monitor().track_api_call(path, latency_ms, response.status)

                    if response.status >= 400:
                        error_msg = result.get("message", "Unknown error")

                        if response.status < 500:  # Don't retry 4xx errors
                            logger.error("API error %s: %s", response.status, error_msg)
                            raise aiohttp.ClientResponseError(
                                request_info=response.request_info,
                                history=response.history,
                                status=response.status,
                                message=error_msg,
                                headers=response.headers,
                            )

                        last_error = aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message=error_msg,
                            headers=response.headers,
                        )
                        logger.warning(
                            "API server error %s (attempt %d/%d): %s",
                            response.status,
                            attempt + 1,
                            max_retries,
                            error_msg,
                        )
                    else:
                        return result

            except aiohttp.ClientError as e:
                last_error = e
                logger.warning(
                    "API request failed (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )

            if attempt < max_retries - 1:
                delay = RETRY_BASE_DELAY_SECONDS * (RETRY_BACKOFF_MULTIPLIER**attempt)
                await asyncio.sleep(delay)

        logger.error("API request failed after %d attempts", max_retries)
        raise last_error

    async def get_balance(self) -> Dict:
        """Get account balance."""
        return await self._request("GET", "/portfolio/balance")

    async def get_market(self, ticker: str, use_cache: bool = True) -> Dict:
        """Get market data with simple caching."""
        if use_cache and ticker in self.market_cache:
            get_monitor().track_cache_access(hit=True)
            return self.market_cache[ticker]

        get_monitor().track_cache_access(hit=False)
        result = await self._request("GET", f"/markets/{ticker}")

        if use_cache:
            self.market_cache[ticker] = result

        return result

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        """Get orderbook with [price, size] pairs for yes/no sides."""
        return await self._request("GET", f"/markets/{ticker}/orderbook?depth={depth}")

    async def get_markets_batch(self, tickers: List[str]) -> Dict[str, Dict]:
        """Fetch multiple markets in parallel."""
        tasks = [self.get_market(ticker) for ticker in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        market_data = {}
        for ticker, result in zip(tickers, results):
            if not isinstance(result, Exception):
                market_data[ticker] = result
            else:
                logger.error("Failed to fetch market %s: %s", ticker, result)
                market_data[ticker] = None

        return market_data

    async def get_positions(self, limit: int = MAX_POSITIONS_PER_PAGE) -> Dict:
        """Get all positions with automatic pagination."""
        positions = []
        cursor = None

        while True:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor

            result = await self._request("GET", "/portfolio/positions", params=params)
            positions.extend(result.get("market_positions", []))

            cursor = result.get("cursor")
            if not cursor:
                break

        return {
            "market_positions": positions,
            "event_positions": result.get("event_positions", []),
        }

    async def get_fills(self, limit: int = 20) -> Dict:
        """Get recent fills."""
        return await self._request("GET", "/portfolio/fills", params={"limit": limit})

    async def get_orders(self, status: Optional[str] = None) -> Dict:
        """Get orders filtered by status."""
        params = {"status": status} if status else {}
        return await self._request("GET", "/portfolio/orders", params=params)

    async def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """Place order with automatic client_order_id for idempotency."""
        if count <= 0:
            raise ValueError(f"Count must be positive, got: {count}")
        if count > 25000:
            raise ValueError(f"Count exceeds reasonable limit, got: {count}")
        if action not in ("buy", "sell"):
            raise ValueError(f"Invalid action: {action}")
        if side not in ("yes", "no"):
            raise ValueError(f"Invalid side: {side}")

        if yes_price is not None and (yes_price < 1 or yes_price > 99):
            raise ValueError(f"yes_price must be 1-99, got: {yes_price}")
        if no_price is not None and (no_price < 1 or no_price > 99):
            raise ValueError(f"no_price must be 1-99, got: {no_price}")

        if not client_order_id:
            client_order_id = str(uuid.uuid4())

        data = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": "limit",
            "client_order_id": client_order_id,
        }

        if side == "yes" and yes_price is not None:
            data["yes_price"] = yes_price
        elif side == "no" and no_price is not None:
            data["no_price"] = no_price

        return await self._request("POST", "/portfolio/orders", data=data)

    async def cancel_order(self, order_id: str) -> Dict:
        """Cancel order by ID."""
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_market_by_ticker(self, ticker: str) -> Optional[Dict]:
        """Get market by exact ticker match. Returns None if not found or on error."""
        ticker = ticker.upper().strip()
        if not ticker:
            return None

        try:
            result = await self._request("GET", f"/markets/{ticker}")
            return result.get("market")
        except (aiohttp.ClientError, KeyError) as e:
            logger.error(f"Failed to fetch market {ticker}: {e}")
            return None
