"""Kalshi API client with connection pooling and optimized authentication."""

import os
import time
import base64

import requests
from dotenv import load_dotenv
from kalshi_python import Configuration, KalshiClient as SDKClient
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

from config.constants import (
    MARKET_BUY_MAX_PRICE,
    MARKET_SELL_MIN_PRICE,
    API_REQUEST_TIMEOUT,
    MAX_POSITIONS_PER_PAGE,
    ERROR_NO_API_KEY,
)

load_dotenv()


class KalshiClient:
    """Synchronous Kalshi client with connection pooling for low-latency trading."""

    def __init__(self, private_key_path="kalshi_private_key.pem"):
        api_key_id = os.getenv("KALSHI_API_KEY_ID")

        if not api_key_id:
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

        config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
        config.api_key_id = api_key_id
        config.private_key_pem = private_key_str

        try:
            self.client = SDKClient(config)
        except Exception as e:
            raise ValueError(f"Failed to initialize Kalshi SDK: {e}") from e

        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=0,
            pool_block=False,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Cache private key to avoid re-parsing on every request
        self._private_key = serialization.load_pem_private_key(
            private_key_str.encode("utf-8"),
            password=None,
            backend=default_backend(),
        )

    def close(self):
        """Close session and cleanup resources."""
        if hasattr(self, "_session") and self._session:
            self._session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def get_balance(self):
        """Get current account balance."""
        try:
            return self.client.get_balance()
        except Exception as e:
            raise RuntimeError(f"Failed to get balance: {e}") from e

    def get_market(self, ticker):
        """Get market information including current prices."""
        try:
            return self.client.get_market(ticker=ticker)
        except Exception as e:
            raise RuntimeError(f"Failed to get market info: {e}") from e

    def get_orderbook(self, ticker):
        """Get order book with bids and asks."""
        try:
            return self.client.get_market_orderbook(ticker=ticker)
        except Exception as e:
            raise RuntimeError(f"Failed to get orderbook: {e}") from e

    def place_order(
        self,
        ticker,
        action,
        side,
        count,
        order_type="limit",
        yes_price=None,
        no_price=None,
    ):
        """Place order using aggressive limit pricing for market orders."""
        if order_type.lower() == "market":
            if action.lower() == "buy":
                if side.lower() == "yes":
                    yes_price = MARKET_BUY_MAX_PRICE
                else:
                    no_price = MARKET_BUY_MAX_PRICE
            else:
                if side.lower() == "yes":
                    yes_price = MARKET_SELL_MIN_PRICE
                else:
                    no_price = MARKET_SELL_MIN_PRICE

        if order_type.lower() == "limit":
            if side.lower() == "yes" and yes_price is None:
                raise ValueError(
                    "yes_price must be specified for limit orders on yes side"
                )
            if side.lower() == "no" and no_price is None:
                raise ValueError(
                    "no_price must be specified for limit orders on no side"
                )

        try:
            order_params = {
                "ticker": ticker,
                "action": action.lower(),
                "side": side.lower(),
                "count": int(count),
                "type": "limit",
            }

            if side.lower() == "yes" and yes_price is not None:
                order_params["yes_price"] = int(yes_price)
            elif side.lower() == "no" and no_price is not None:
                order_params["no_price"] = int(no_price)

            return self.client.create_order(**order_params)
        except Exception as e:
            raise RuntimeError(f"Failed to place order: {e}") from e

    def get_positions(self):
        """Get current portfolio positions with automatic pagination."""
        try:
            path = "/trade-api/v2/portfolio/positions"
            timestamp = str(int(time.time() * 1000))
            method = "GET"

            message = f"{timestamp}{method}{path}".encode("utf-8")
            signature = self._private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
            signature_b64 = base64.b64encode(signature).decode("utf-8")

            headers = {
                "KALSHI-ACCESS-KEY": self.client.configuration.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": signature_b64,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "Content-Type": "application/json",
            }

            url = f"https://api.elections.kalshi.com{path}"
            url += f"?limit={MAX_POSITIONS_PER_PAGE}"

            response = self._session.get(
                url, headers=headers, timeout=API_REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                raise RuntimeError(
                    f"API returned status {response.status_code}: {response.text}"
                )

            data = response.json()
            all_positions = data.get("market_positions", [])
            cursor = data.get("cursor", "")

            while cursor:
                url_with_cursor = (
                    f"https://api.elections.kalshi.com{path}"
                    f"?limit={MAX_POSITIONS_PER_PAGE}&cursor={cursor}"
                )
                response = self._session.get(
                    url_with_cursor, headers=headers, timeout=API_REQUEST_TIMEOUT
                )

                if response.status_code != 200:
                    break

                page_data = response.json()
                all_positions.extend(page_data.get("market_positions", []))
                cursor = page_data.get("cursor", "")

            return {
                "market_positions": all_positions,
                "event_positions": data.get("event_positions", []),
                "cursor": "",
            }

        except Exception as e:
            raise RuntimeError(f"Failed to get positions: {e}") from e

    def get_fills(self, limit=20):
        """Get recent fills (executed trades)."""
        try:
            return self.client.get_fills(limit=limit)
        except Exception as e:
            raise RuntimeError(f"Failed to get fills: {e}") from e

    def get_orders(self, status="all"):
        """Get orders filtered by status."""
        try:
            if status == "all":
                return self.client.get_orders()
            else:
                return self.client.get_orders(status=status)
        except Exception as e:
            raise RuntimeError(f"Failed to get orders: {e}") from e

    def cancel_order(self, order_id):
        """Cancel order by ID."""
        try:
            result = self.client.cancel_order(order_id=order_id)
            return {
                "success": True,
                "message": "Order cancelled successfully",
                "result": result,
            }
        except Exception as e:
            raise RuntimeError(f"Failed to cancel order: {e}") from e

    def search_markets(self, query, max_results=20):
        """Search markets by title with relevance scoring."""
        try:
            query_lower = query.lower()
            query_words = query_lower.split()
            all_matches = []

            cursor = None
            pages_checked = 0
            max_pages = 20

            while pages_checked < max_pages and len(all_matches) < max_results * 3:
                try:
                    if cursor:
                        markets_result = self.client.get_markets(
                            limit=100, cursor=cursor
                        )
                    else:
                        markets_result = self.client.get_markets(limit=100)

                    if hasattr(markets_result, "markets") and markets_result.markets:
                        for market in markets_result.markets:
                            title_lower = market.title.lower()
                            ticker_lower = market.ticker.lower()

                            score = 0

                            if query_lower in title_lower:
                                score += 100

                            if all(word in title_lower for word in query_words):
                                score += 50

                            if query_lower in ticker_lower:
                                score += 30

                            for word in query_words:
                                if word in title_lower:
                                    score += 10

                            if score > 0:
                                all_matches.append((score, market))

                        if hasattr(markets_result, "cursor") and markets_result.cursor:
                            cursor = markets_result.cursor
                            pages_checked += 1
                        else:
                            break
                    else:
                        break
                except (ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
                    print(f"Error fetching markets page: {e}")
                    break

            all_matches.sort(key=lambda x: x[0], reverse=True)

            seen_tickers = set()
            unique_matches = []
            for score, market in all_matches:
                if market.ticker not in seen_tickers:
                    seen_tickers.add(market.ticker)
                    unique_matches.append(market)
                    if len(unique_matches) >= max_results:
                        break

            return unique_matches

        except Exception as e:
            raise RuntimeError(f"Failed to search markets: {e}") from e
