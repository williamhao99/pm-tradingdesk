"""Kalshi WebSocket client for real-time market data and fills."""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable, Dict, Optional, Set

import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from src.kalshi.tools.performance_monitor import get_monitor

logger = logging.getLogger(__name__)


class KalshiWebSocketClient:
    """Real-time WebSocket client for Kalshi API with auto-reconnection."""

    def __init__(
        self,
        api_key_id: str,
        private_key,
        ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2",
        on_message: Optional[Callable[[Dict], None]] = None,
        on_status_change: Optional[Callable[[bool], None]] = None,
    ):
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.ws_url = ws_url
        self.on_message = on_message
        self.on_status_change = on_status_change

        self.ws: Optional[websockets.ClientConnection] = None
        self.is_running = False
        self.reconnect_attempts = 0
        self.max_reconnect_delay = 30.0

        self.subscribed_tickers: Set[str] = set()
        self.orderbook_sequences: Dict[str, int] = {}
        self.ticker_sids: Dict[str, list[int]] = {}
        self._awaiting_snapshot: Set[str] = set()  # Tickers waiting for fresh snapshot
        self._last_resubscribe: Dict[str, float] = {}  # Cooldown to prevent thrashing

        self._next_request_id = 1
        self._pending_subscriptions: Dict[int, str] = {}  # request_id -> ticker

        self._connection_task: Optional[asyncio.Task] = None
        self._message_task: Optional[asyncio.Task] = None
        self._disconnect_time: Optional[float] = None

    def _generate_auth_headers(self) -> Dict[str, str]:
        """Generate WebSocket authentication headers."""
        timestamp = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        message = f"{timestamp}GET{path}".encode()

        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256(),
        )

        signature_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _get_next_request_id(self) -> int:
        """Get next unique request ID."""
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    async def start(self):
        """Start WebSocket connection and message processing."""
        if self.is_running:
            logger.warning("WebSocket client already running")
            return

        self.is_running = True
        self._connection_task = asyncio.create_task(self._connection_loop())
        logger.info("Kalshi WebSocket client started")

    async def stop(self):
        """Stop WebSocket connection gracefully."""
        if not self.is_running:
            return

        logger.info("Stopping Kalshi WebSocket client...")
        self.is_running = False

        if self._message_task:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass

        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug("WebSocket close error (expected during cleanup): %s", e)

        if self._connection_task:
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass

        logger.info("Kalshi WebSocket client stopped")

    async def _connection_loop(self):
        """Maintain WebSocket connection with automatic reconnection."""
        while self.is_running:
            try:
                headers = self._generate_auth_headers()

                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                ) as websocket:
                    self.ws = websocket
                    logger.info("Connected to Kalshi WebSocket")

                    if self._disconnect_time is not None:
                        reconnect_ms = (time.time() - self._disconnect_time) * 1000
                        get_monitor().track_ws_reconnection(reconnect_ms)
                        self._disconnect_time = None

                    self.reconnect_attempts = 0

                    if self.on_status_change:
                        self.on_status_change(True)

                    await self._subscribe_fills()
                    await self._resubscribe_tickers()

                    self._message_task = asyncio.create_task(
                        self._message_loop(websocket)
                    )
                    await self._message_task

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")

                if self._disconnect_time is None:
                    self._disconnect_time = time.time()

                if self.on_status_change:
                    self.on_status_change(False)

                if not self.is_running:
                    break

                delay = min(
                    self.max_reconnect_delay, (2**self.reconnect_attempts) + 0.5
                )
                self.reconnect_attempts += 1

                logger.info(
                    f"Reconnecting in {delay:.1f}s "
                    f"(attempt {self.reconnect_attempts})..."
                )
                await asyncio.sleep(delay)

    async def _message_loop(self, websocket: websockets.ClientConnection):
        """Process incoming WebSocket messages."""
        async for message in websocket:
            try:
                with get_monitor().track_operation("ws_message"):
                    data = json.loads(message)
                    await self._handle_message(data)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse WebSocket message: {e}")
            except Exception as e:
                logger.error(f"Error handling message: {e}", exc_info=True)

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Route incoming messages and detect issues."""
        msg_type = data.get("type") or data.get("msg")

        if msg_type == "subscribed":
            msg = data.get("msg", {})  # API uses "msg", not "result"
            request_id = data.get("id")  # Get request ID from response

            if "channels" in msg:  # Array format
                channels = msg.get("channels", [])
                tickers = msg.get("market_tickers", [])
                if tickers:
                    logger.info(f"Subscribed to {channels} for {tickers}")
                else:
                    logger.info(f"Subscribed to {channels}")
            elif "channel" in msg:  # Single channel format
                channel = msg.get("channel")
                sid = msg.get("sid")

                ticker = self._pending_subscriptions.pop(request_id, None)

                if ticker and channel in ["ticker", "orderbook_delta"]:
                    if ticker not in self.ticker_sids:
                        self.ticker_sids[ticker] = []
                    self.ticker_sids[ticker].append(sid)
                    logger.info(f"Subscribed to '{channel}' for {ticker} (sid={sid})")
                else:
                    logger.info(f"Subscribed to '{channel}' (sid={sid})")
            return

        if msg_type == "error":
            error_msg = data.get(
                "msg", data.get("message", {})
            )  # API response format varies
            if isinstance(error_msg, dict):
                code = error_msg.get("code")
                msg = error_msg.get("msg", error_msg.get("message"))
                logger.error(f"Kalshi WebSocket error: code={code}, msg={msg}")
            else:
                logger.error(f"Kalshi WebSocket error: {data}")
            return

        if msg_type == "orderbook_snapshot":
            # Snapshot received - clear awaiting flag and reset sequence tracking
            msg = data.get("msg", data)
            ticker = msg.get("market_ticker") or msg.get("ticker")
            if ticker:
                self._awaiting_snapshot.discard(ticker)
                self.orderbook_sequences.pop(ticker, None)  # Will be set by first delta

        if msg_type == "orderbook_delta" or msg_type == "orderbook_update":
            # Data may be nested inside 'msg' field
            msg = data.get("msg", data)
            ticker = msg.get("market_ticker") or msg.get("ticker")
            seq = data.get("seq")  # seq is at top level

            # Ignore deltas while waiting for fresh snapshot
            if ticker and ticker in self._awaiting_snapshot:
                return

            if ticker and seq is not None:
                expected_seq = self.orderbook_sequences.get(ticker)

                if expected_seq is not None and seq != expected_seq + 1:
                    # Rate limit resubscribes to prevent thrashing (5s cooldown)
                    now = time.time()
                    last_resub = self._last_resubscribe.get(ticker, 0)
                    if now - last_resub < 5.0:
                        # Still in cooldown - just accept the gap and continue
                        logger.debug(
                            f"Gap for {ticker} (seq {expected_seq + 1} -> {seq}), cooldown active"
                        )
                    else:
                        logger.warning(
                            f"Orderbook gap for {ticker}: seq {expected_seq + 1} -> {seq}. Resubscribing..."
                        )
                        self._last_resubscribe[ticker] = now
                        self._awaiting_snapshot.add(ticker)
                        # Unsubscribe first to force fresh snapshot on resubscribe
                        await self.unsubscribe_from_ticker(ticker)
                        await self.subscribe_to_ticker(ticker)
                        return

                self.orderbook_sequences[ticker] = seq

        if self.on_message:
            try:
                await self.on_message(data)
            except Exception as e:
                logger.error(f"Error in message callback: {e}", exc_info=True)

    async def _subscribe_fills(self) -> None:
        """Subscribe to fills channel (user-specific)."""
        if not self.ws:
            return

        try:
            request_id = self._get_next_request_id()
            # Note: API uses "fill" (singular), not "fills"
            await self.ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "cmd": "subscribe",
                        "params": {"channels": ["fill"]},
                    }
                )
            )
            logger.info("Sent fill subscription request")
        except Exception as e:
            logger.error(f"Failed to subscribe to fill: {e}")

    async def subscribe_to_ticker(self, ticker: str) -> None:
        """Subscribe to ticker and orderbook updates for a market."""
        if not self.ws:
            logger.warning("Cannot subscribe: WebSocket not connected")
            return

        try:
            request_id = self._get_next_request_id()
            self._pending_subscriptions[request_id] = ticker  # Track by request ID

            await self.ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker", "orderbook_delta"],
                            "market_tickers": [ticker],
                        },
                    }
                )
            )

            self.subscribed_tickers.add(ticker)
            self.orderbook_sequences.pop(
                ticker, None
            )  # Reset sequence for fresh subscription
            self.ticker_sids.pop(ticker, None)  # Clear old subscription IDs

            logger.info(f"Sent subscription request for {ticker} (id={request_id})")
        except Exception as e:
            logger.error(f"Failed to subscribe to {ticker}: {e}")

    async def unsubscribe_from_ticker(self, ticker: str) -> None:
        """Unsubscribe from a market's updates."""
        if not self.ws:
            return

        try:
            sids = self.ticker_sids.get(ticker, [])

            if not sids:
                logger.warning(
                    f"No subscription IDs found for {ticker}, cannot unsubscribe"
                )
                return

            request_id = self._get_next_request_id()
            await self.ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "cmd": "unsubscribe",
                        "params": {"sids": sids},
                    }
                )
            )

            self.subscribed_tickers.discard(ticker)
            self.orderbook_sequences.pop(ticker, None)
            self.ticker_sids.pop(ticker, None)

            logger.info(
                f"Sent unsubscribe request for {ticker} (sids={sids}, id={request_id})"
            )
        except Exception as e:
            logger.error(f"Failed to unsubscribe from {ticker}: {e}")

    async def _resubscribe_tickers(self) -> None:
        """Resubscribe to all tickers after reconnection."""
        if not self.subscribed_tickers:
            return

        logger.info(f"Resubscribing to {len(self.subscribed_tickers)} tickers...")

        for ticker in list(self.subscribed_tickers):
            await self.subscribe_to_ticker(ticker)
