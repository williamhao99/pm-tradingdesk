"""Async Kalshi client with connection pooling, caching, and rate limiting."""

import os
import sys
import asyncio
import time
import base64
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from collections import deque

import aiohttp
from dotenv import load_dotenv
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from config.constants import (
    PROD_API_URL,
    DEMO_API_URL,
    MARKET_BUY_MAX_PRICE,
    MARKET_SELL_MIN_PRICE,
    MARKET_CACHE_TTL,
    ORDERBOOK_CACHE_TTL,
    MAX_TOTAL_CONNECTIONS,
    MAX_CONNECTIONS_PER_HOST,
    KEEPALIVE_TIMEOUT,
    DNS_CACHE_TTL,
    API_REQUEST_TIMEOUT,
    API_CONNECT_TIMEOUT,
    API_READ_TIMEOUT,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_PERIOD,
    MAX_POSITIONS_PER_PAGE,
    PERFORMANCE_WINDOW_SIZE,
    ERROR_NO_API_KEY,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


@dataclass
class MarketData:
    """Cached market data with TTL."""

    ticker: str
    title: str
    yes_price: Optional[int]
    no_price: Optional[int]
    last_price: Optional[int]
    yes_sub_title: Optional[str] = None
    no_sub_title: Optional[str] = None
    timestamp: float = 0.0

    def is_stale(self, max_age: float = MARKET_CACHE_TTL) -> bool:
        """Check if data exceeds TTL."""
        return time.time() - self.timestamp > max_age


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, rate: int = RATE_LIMIT_REQUESTS, per: float = RATE_LIMIT_PERIOD):
        self.rate = rate
        self.per = per
        self.allowance = rate
        self.last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire rate limit token, sleeping if necessary."""
        async with self._lock:
            current = time.monotonic()
            time_passed = current - self.last_check
            self.last_check = current
            self.allowance += time_passed * (self.rate / self.per)

            if self.allowance > self.rate:
                self.allowance = self.rate

            if self.allowance < 1.0:
                sleep_time = (1.0 - self.allowance) * (self.per / self.rate)
                await asyncio.sleep(sleep_time)
                self.allowance = 0.0
            else:
                self.allowance -= 1.0


class AsyncKalshiClient:
    """Async Kalshi client optimized for dashboard WebSocket feeds."""

    def __init__(self, private_key_path: str = "kalshi_private_key.pem"):
        self.api_key_id = os.getenv("KALSHI_API_KEY_ID")
        if not self.api_key_id:
            raise ValueError(ERROR_NO_API_KEY)

        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        key_file = os.path.join(project_root, private_key_path)
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                private_key_str = f.read()
        except FileNotFoundError as exc:
            raise ValueError(f"Private key file not found: {key_file}") from exc

        self.private_key = serialization.load_pem_private_key(
            private_key_str.encode("utf-8"), password=None, backend=default_backend()
        )

        self.base_url = PROD_API_URL
        self.demo_url = DEMO_API_URL
        self.use_demo = os.getenv("USE_DEMO", "false").lower() == "true"

        connector = aiohttp.TCPConnector(
            limit=MAX_TOTAL_CONNECTIONS,
            limit_per_host=MAX_CONNECTIONS_PER_HOST,
            ttl_dns_cache=DNS_CACHE_TTL,
            enable_cleanup_closed=True,
            force_close=False,
            keepalive_timeout=KEEPALIVE_TIMEOUT,
        )

        timeout = aiohttp.ClientTimeout(
            total=API_REQUEST_TIMEOUT,
            connect=API_CONNECT_TIMEOUT,
            sock_read=API_READ_TIMEOUT,
        )

        self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        self.market_cache: Dict[str, MarketData] = {}
        self.orderbook_cache: Dict[str, Tuple[Any, float]] = {}
        self.cache_lock = asyncio.Lock()
        self.last_cache_cleanup = time.time()

        self.rate_limiter = RateLimiter()

        self.request_count = 0
        self.request_times: deque = deque(maxlen=PERFORMANCE_WINDOW_SIZE)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        """Close HTTP session and cleanup resources."""
        await self.session.close()

    async def cleanup_stale_caches(self):
        """Remove stale entries from caches to prevent unbounded growth."""
        current_time = time.time()

        async with self.cache_lock:
            # Clean market cache (remove entries older than TTL)
            stale_markets = [
                ticker
                for ticker, data in self.market_cache.items()
                if data.is_stale()
            ]
            for ticker in stale_markets:
                del self.market_cache[ticker]

            # Clean orderbook cache (remove entries older than TTL)
            stale_orderbooks = [
                key
                for key, (_, timestamp) in self.orderbook_cache.items()
                if current_time - timestamp > ORDERBOOK_CACHE_TTL
            ]
            for key in stale_orderbooks:
                del self.orderbook_cache[key]

            if stale_markets or stale_orderbooks:
                logger.info(
                    f"Cache cleanup: removed {len(stale_markets)} stale markets, "
                    f"{len(stale_orderbooks)} stale orderbooks"
                )

            self.last_cache_cleanup = current_time

    def _sign_request(self, timestamp: str, method: str, path: str) -> str:
        """Generate RSA-PSS signature for authentication."""
        full_path = f"/trade-api/v2{path}"
        message = f"{timestamp}{method}{full_path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        use_cache: bool = False,
    ) -> Dict:
        """Make authenticated request with rate limiting and optional caching."""
        # Periodic cache cleanup (every hour) to prevent unbounded growth
        if time.time() - self.last_cache_cleanup > 3600:  # 1 hour
            await self.cleanup_stale_caches()

        cache_key = None
        if method == "GET" and use_cache:
            cache_key = hash((path, str(params)))
            if cache_key in self.orderbook_cache:
                cached_data, cached_time = self.orderbook_cache[cache_key]
                if time.time() - cached_time < ORDERBOOK_CACHE_TTL:
                    return cached_data

        await self.rate_limiter.acquire()

        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request(timestamp, method, path)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

        url = f"{self.demo_url if self.use_demo else self.base_url}{path}"

        start_time = time.time()
        self.request_count += 1

        try:
            async with self.session.request(
                method, url, headers=headers, json=data, params=params
            ) as response:
                request_time = time.time() - start_time
                self.request_times.append(request_time)

                result = await response.json()

                if response.status >= 400:
                    error_msg = result.get("message", "Unknown error")

                    if response.status == 401:
                        logger.error("Authentication failed: %s", error_msg)
                    elif response.status == 403:
                        logger.error("Rate limited or forbidden: %s", error_msg)
                    elif response.status == 422:
                        logger.error("Validation error: %s", error_msg)
                    else:
                        logger.error("API error %s: %s", response.status, error_msg)

                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=error_msg,
                        headers=response.headers,
                    )

                if method == "GET" and use_cache and cache_key is not None:
                    self.orderbook_cache[cache_key] = (result, time.time())

                return result

        except aiohttp.ClientError as e:
            logger.error("API request failed: %s", e)
            raise

    async def get_balance(self) -> Dict:
        """Get account balance."""
        return await self._request("GET", "/portfolio/balance")

    async def get_market(self, ticker: str, use_cache: bool = True) -> Dict:
        """Get market data with caching."""
        async with self.cache_lock:
            if use_cache and ticker in self.market_cache:
                cached = self.market_cache[ticker]
                if not cached.is_stale():
                    return {"market": asdict(cached)}

        result = await self._request("GET", f"/markets/{ticker}")

        if "market" in result:
            market = result["market"]
            cached_data = MarketData(
                ticker=ticker,
                title=market.get("title", ticker),
                yes_price=market.get("yes_price"),
                no_price=market.get("no_price"),
                last_price=market.get("last_price"),
                yes_sub_title=market.get("yes_sub_title"),
                no_sub_title=market.get("no_sub_title"),
                timestamp=time.time(),
            )
            async with self.cache_lock:
                self.market_cache[ticker] = cached_data

        return result

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

    async def get_orderbook(self, ticker: str) -> Dict:
        """Get orderbook with caching."""
        return await self._request(
            "GET", f"/markets/{ticker}/orderbook", use_cache=True
        )

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
        if order_type == "market":
            if action == "buy":
                if side == "yes":
                    yes_price = MARKET_BUY_MAX_PRICE
                else:
                    no_price = MARKET_BUY_MAX_PRICE
            else:
                if side == "yes":
                    yes_price = MARKET_SELL_MIN_PRICE
                else:
                    no_price = MARKET_SELL_MIN_PRICE

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

    async def get_market_by_ticker(self, ticker: str) -> Dict:
        """Get market by exact ticker match."""
        ticker = ticker.upper().strip()
        if not ticker:
            return {}

        try:
            result = await self._request("GET", f"/markets/{ticker}")
            return result.get("market", {})
        except (aiohttp.ClientError, KeyError):
            return {}

    def get_metrics(self) -> Dict:
        """Get performance metrics (latency percentiles, cache stats)."""
        if not self.request_times:
            return {
                "total_requests": self.request_count,
                "avg_latency_ms": 0,
                "p95_latency_ms": 0,
                "p99_latency_ms": 0,
            }

        sorted_times = sorted(self.request_times)
        p95_index = int(len(sorted_times) * 0.95)
        p99_index = int(len(sorted_times) * 0.99)

        return {
            "total_requests": self.request_count,
            "avg_latency_ms": sum(sorted_times) / len(sorted_times) * 1000,
            "p95_latency_ms": (
                sorted_times[p95_index] * 1000 if p95_index < len(sorted_times) else 0
            ),
            "p99_latency_ms": (
                sorted_times[p99_index] * 1000 if p99_index < len(sorted_times) else 0
            ),
            "cache_size": len(self.market_cache),
        }
