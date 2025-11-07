#!/usr/bin/env python3
"""Kalshi Trading Dashboard WebSocket server with compression and real-time updates"""

import asyncio
import json
import logging
import subprocess
import sys
import time
import traceback
import zlib
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

import uvloop
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from config.constants import (
    COMPRESSION_RATIO_WINDOW,
    DEFAULT_FILLS_LIMIT,
    MARKET_UPDATE_INTERVAL,
    PERFORMANCE_WINDOW_SIZE,
    WS_COMPRESSION_LEVEL,
    WS_COMPRESSION_THRESHOLD,
)
from src.kalshi.clients.kalshi_client_async import AsyncKalshiClient, MarketData
from src.kalshi.tools.generate_hotkeys import (
    fetch_markets_by_pattern,
    generate_hotkeys_config,
    save_hotkeys_config,
)

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket connection manager with compression support"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.connection_metadata: Dict[WebSocket, Dict] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Add a new connection"""
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
            self.connection_metadata[websocket] = {
                "connected_at": time.time(),
                "last_ping": time.time(),
                "message_count": 0,
                "compression_enabled": True,
            }

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove connection and clean up subscriptions"""
        async with self._lock:
            self.active_connections.discard(websocket)
            self.connection_metadata.pop(websocket, None)

        if market_service is not None:
            try:
                await market_service.unsubscribe_all(websocket)
            except Exception as e:
                logger.error("Error unsubscribing websocket during disconnect: %s", e)

    async def send_json(
        self, websocket: WebSocket, data: Dict, compress: bool = True
    ) -> bool:
        """Send JSON with optional compression. Returns True if successful."""
        if websocket not in self.active_connections:
            return False

        try:
            if compress and self.connection_metadata.get(websocket, {}).get(
                "compression_enabled", False
            ):
                json_bytes = json.dumps(data).encode("utf-8")
                compressed = zlib.compress(json_bytes, level=WS_COMPRESSION_LEVEL)

                if len(compressed) < len(json_bytes) * WS_COMPRESSION_THRESHOLD:
                    await websocket.send_bytes(b"\x01" + compressed)
                else:
                    await websocket.send_text(json.dumps(data))
            else:
                await websocket.send_json(data)

            if websocket in self.connection_metadata:
                self.connection_metadata[websocket]["message_count"] += 1

            return True

        except Exception:
            await self.disconnect(websocket)
            return False

    async def broadcast_json(self, data: Dict, compress: bool = True) -> None:
        """Broadcast JSON to all clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await self.send_json(connection, data, compress=compress)
            except Exception:
                disconnected.append(connection)

        for connection in disconnected:
            await self.disconnect(connection)


class MarketDataService:
    """Market data service with real-time updates"""

    def __init__(self, client: AsyncKalshiClient):
        self.client = client
        self.update_subscribers: Dict[str, Set[WebSocket]] = {}
        self._update_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start_updates(self):
        """Start background market updates"""
        self._update_task = asyncio.create_task(self._update_loop())

    async def stop_updates(self):
        """Stop background updates"""
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

    async def _update_loop(self):
        """Background loop to update subscribed market data"""
        while True:
            try:
                await asyncio.sleep(MARKET_UPDATE_INTERVAL)

                async with self._lock:
                    tickers = list(self.update_subscribers.keys())

                if tickers:
                    market_updates = await self.client.get_markets_batch(tickers)

                    for ticker, data in market_updates.items():
                        if data:
                            async with self._lock:
                                if ticker not in self.update_subscribers:
                                    continue
                                subscribers = self.update_subscribers[ticker].copy()

                            if not subscribers:
                                continue

                            failed_connections = []
                            for ws in subscribers:
                                success = await connection_manager.send_json(
                                    ws,
                                    {
                                        "type": "market_update",
                                        "ticker": ticker,
                                        "data": data,
                                    },
                                    compress=True,
                                )

                                if not success:
                                    failed_connections.append(ws)

                            if failed_connections:
                                async with self._lock:
                                    if ticker in self.update_subscribers:
                                        for ws in failed_connections:
                                            self.update_subscribers[ticker].discard(ws)

            except Exception as e:
                logger.error("Update loop error: %s", e)
                await asyncio.sleep(5)

    async def subscribe_to_market(self, websocket: WebSocket, ticker: str):
        """Subscribe WebSocket to market updates"""
        async with self._lock:
            if ticker not in self.update_subscribers:
                self.update_subscribers[ticker] = set()
            self.update_subscribers[ticker].add(websocket)

    async def unsubscribe_from_market(self, websocket: WebSocket, ticker: str):
        """Unsubscribe WebSocket from market updates"""
        async with self._lock:
            if ticker in self.update_subscribers:
                self.update_subscribers[ticker].discard(websocket)
                if not self.update_subscribers[ticker]:
                    del self.update_subscribers[ticker]

    async def unsubscribe_all(self, websocket: WebSocket):
        """Unsubscribe WebSocket from all markets"""
        async with self._lock:
            tickers_to_clean = []
            for ticker, subscribers in self.update_subscribers.items():
                if websocket in subscribers:
                    subscribers.discard(websocket)
                    if not subscribers:
                        tickers_to_clean.append(ticker)

            for ticker in tickers_to_clean:
                del self.update_subscribers[ticker]


class PerformanceMonitor:
    """Performance metrics tracker"""

    def __init__(
        self,
        window_size: int = PERFORMANCE_WINDOW_SIZE,
        compression_window: int = COMPRESSION_RATIO_WINDOW,
    ):
        self.request_times = deque(maxlen=window_size)
        self.error_count = 0
        self.success_count = 0
        self.start_time = time.time()
        self.compression_ratio = deque(maxlen=compression_window)

    def record_request(self, duration: float, success: bool = True):
        """Record a request"""
        self.request_times.append(duration)
        if success:
            self.success_count += 1
        else:
            self.error_count += 1

    def record_compression(self, original_size: int, compressed_size: int):
        """Record compression statistics"""
        if original_size > 0:
            ratio = compressed_size / original_size
            self.compression_ratio.append(ratio)

    def get_metrics(self) -> Dict:
        """Get current metrics"""
        uptime = time.time() - self.start_time
        total_requests = self.success_count + self.error_count

        if not self.request_times:
            return {
                "uptime_seconds": uptime,
                "total_requests": total_requests,
                "success_rate": 0,
                "avg_latency_ms": 0,
                "p95_latency_ms": 0,
                "p99_latency_ms": 0,
                "avg_compression_ratio": 0,
            }

        sorted_times = sorted(self.request_times)
        p95_index = int(len(sorted_times) * 0.95)
        p99_index = int(len(sorted_times) * 0.99)

        avg_compression = (
            sum(self.compression_ratio) / len(self.compression_ratio)
            if self.compression_ratio
            else 0
        )

        return {
            "uptime_seconds": uptime,
            "total_requests": total_requests,
            "success_rate": (
                self.success_count / total_requests if total_requests > 0 else 0
            ),
            "avg_latency_ms": sum(sorted_times) / len(sorted_times) * 1000,
            "p95_latency_ms": sorted_times[p95_index] * 1000,
            "p99_latency_ms": sorted_times[p99_index] * 1000,
            "error_count": self.error_count,
            "avg_compression_ratio": avg_compression,
            "compression_savings": f"{(1 - avg_compression) * 100:.1f}%",
        }


class HotkeyBotManager:
    """Hotkey trader subprocess manager"""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.is_running = False
        self.stats = {"trades": 0, "start_time": None}
        self._output_task: Optional[asyncio.Task] = None
        self.market_series_ticker: Optional[str] = None

    async def start_bot(self) -> Dict[str, Any]:
        """Start the hotkey trader subprocess"""
        if self.is_running:
            return {"success": False, "error": "Bot is already running"}

        try:
            project_root = Path(__file__).parent.parent.parent.parent
            hotkeys_file = project_root / "src" / "kalshi" / "tools" / "hotkeys.json"

            try:
                with open(hotkeys_file, "r", encoding="utf-8") as f:
                    hotkeys_config = json.load(f)
                    hotkeys = hotkeys_config.get("hotkeys", {})

                    if hotkeys:
                        first_hotkey = next(iter(hotkeys.values()))
                        ticker = first_hotkey.get("ticker")

                        if ticker:
                            series_ticker = (
                                ticker.split("-")[0] if "-" in ticker else ticker
                            )
                            self.market_series_ticker = series_ticker
            except Exception as e:
                logger.error("Error loading hotkeys config: %s", e, exc_info=True)
                self.market_series_ticker = None

            bot_script = project_root / "src" / "kalshi" / "bots" / "hotkey_trader.py"

            self.process = subprocess.Popen(
                [sys.executable, str(bot_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            self.is_running = True
            self.stats["start_time"] = time.time()
            self.stats["trades"] = 0

            self._output_task = asyncio.create_task(self._read_output())

            return {"success": True, "message": "Hotkey bot started"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def stop_bot(self) -> Dict[str, Any]:
        """Stop the hotkey trader subprocess"""
        if not self.is_running:
            return {"success": False, "error": "Bot is not running"}

        try:
            if self.process:
                self.process.stdin.write("quit\n")
                self.process.stdin.flush()

                await asyncio.sleep(0.5)

                if self.process.poll() is None:
                    self.process.terminate()
                    await asyncio.sleep(0.2)
                    if self.process.poll() is None:
                        self.process.kill()

                self.process = None

            if self._output_task:
                self._output_task.cancel()
                self._output_task = None

            self.is_running = False
            self.market_series_ticker = None
            return {"success": True, "message": "Hotkey bot stopped"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def execute_hotkey(self, keyword: str) -> Dict[str, Any]:
        """Send keyword to running bot"""
        if not self.is_running or not self.process:
            return {"success": False, "error": "Bot is not running"}

        try:
            self.process.stdin.write(f"{keyword}\n")
            self.process.stdin.flush()

            self.stats["trades"] += 1

            return {"success": True, "keyword": keyword}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _read_output(self):
        """Read subprocess output"""
        try:
            while self.is_running and self.process:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self.process.stdout.readline
                )
                if line:
                    print(line.strip())
                else:
                    break
        except Exception as e:
            logger.error("Error reading bot output: %s", e)

    def get_status(self) -> Dict[str, Any]:
        """Get bot status"""
        uptime = None
        if self.is_running and self.stats["start_time"]:
            uptime = time.time() - self.stats["start_time"]

        return {
            "is_running": self.is_running,
            "trades": self.stats["trades"],
            "uptime_seconds": uptime,
            "market_series_ticker": self.market_series_ticker,
        }


connection_manager = ConnectionManager()
market_service: Optional[MarketDataService] = None
performance_monitor = PerformanceMonitor()
kalshi_client: Optional[AsyncKalshiClient] = None
hotkey_bot_manager = HotkeyBotManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifecycle manager"""
    global kalshi_client, market_service

    kalshi_client = AsyncKalshiClient()
    market_service = MarketDataService(kalshi_client)
    await market_service.start_updates()

    yield

    if hotkey_bot_manager.is_running:
        await hotkey_bot_manager.stop_bot()
    if market_service:
        await market_service.stop_updates()
    if kalshi_client:
        await kalshi_client.close()


app = FastAPI(lifespan=lifespan, title="Kalshi Trading Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def get_dashboard():
    """Serve dashboard HTML"""
    html_path = Path(__file__).parent / "dashboard.html"
    return FileResponse(html_path)


@app.get("/api/health")
async def health_check():
    """Health check"""
    metrics = performance_monitor.get_metrics()
    client_metrics = kalshi_client.get_metrics() if kalshi_client else {}

    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server_metrics": metrics,
        "client_metrics": client_metrics,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time communication"""
    await connection_manager.connect(websocket)

    try:
        await connection_manager.send_json(
            websocket,
            {
                "type": "connection",
                "status": "connected",
                "message": "Connected to Kalshi Trading Dashboard (Compression Enabled)",
            },
            compress=False,
        )

        while True:
            start_time = time.time()

            try:
                raw_data = await websocket.receive()

                if raw_data.get("type") == "websocket.disconnect":
                    break

                if "text" in raw_data:
                    data = json.loads(raw_data["text"])
                elif "bytes" in raw_data:
                    bytes_data = raw_data["bytes"]
                    if bytes_data[0:1] == b"\x01":
                        decompressed = zlib.decompress(bytes_data[1:])
                        data = json.loads(decompressed.decode("utf-8"))
                    else:
                        data = json.loads(bytes_data.decode("utf-8"))
                else:
                    continue

                action = data.get("action")

                if action == "get_balance":
                    result = await kalshi_client.get_balance()
                    await connection_manager.send_json(
                        websocket,
                        {"type": "balance", "balance": result.get("balance", 0)},
                        compress=False,
                    )

                elif action == "get_positions":
                    positions = await kalshi_client.get_positions()
                    market_positions = positions.get("market_positions", [])

                    active_positions = [
                        p
                        for p in market_positions
                        if p.get("position", 0) != 0 or p.get("market_exposure", 0) != 0
                    ]

                    if active_positions:
                        tickers = [p["ticker"] for p in active_positions]
                        market_data = await kalshi_client.get_markets_batch(tickers)

                        for pos in active_positions:
                            ticker = pos["ticker"]
                            if ticker in market_data and market_data[ticker]:
                                market = market_data[ticker].get("market", {})
                                pos["title"] = market.get("title", ticker)
                                pos["yes_sub_title"] = market.get("yes_sub_title")
                                pos["no_sub_title"] = market.get("no_sub_title")
                                pos["yes_price"] = market.get("yes_price")
                                pos["no_price"] = market.get("no_price")
                                if pos["yes_price"] is None:
                                    pos["yes_price"] = market.get("last_price")
                                if (
                                    pos["no_price"] is None
                                    and pos["yes_price"] is not None
                                ):
                                    pos["no_price"] = 100 - pos["yes_price"]

                    await connection_manager.send_json(
                        websocket,
                        {"type": "positions", "positions": active_positions},
                        compress=True,
                    )

                elif action == "get_fills":
                    limit = data.get("limit", DEFAULT_FILLS_LIMIT)
                    fills_result = await kalshi_client.get_fills(limit=limit)
                    fills = fills_result.get("fills", [])

                    if fills:
                        tickers = list(set(fill["ticker"] for fill in fills))
                        market_data = await kalshi_client.get_markets_batch(tickers)

                        fills_data = []
                        for fill in fills:
                            ticker = fill["ticker"]
                            title = ticker
                            yes_sub_title = None
                            no_sub_title = None

                            if ticker in market_data and market_data[ticker]:
                                market = market_data[ticker].get("market", {})
                                title = market.get("title", ticker)
                                yes_sub_title = market.get("yes_sub_title")
                                no_sub_title = market.get("no_sub_title")

                            fill_side = fill.get("side")
                            if fill_side == "yes":
                                price_cents = fill.get("yes_price", 0)
                            else:
                                price_cents = fill.get("no_price", 0)

                            fills_data.append(
                                {
                                    "ticker": ticker,
                                    "title": title,
                                    "yes_sub_title": yes_sub_title,
                                    "no_sub_title": no_sub_title,
                                    "action": fill.get("action"),
                                    "side": fill.get("side"),
                                    "count": fill.get("count"),
                                    "price": price_cents,
                                    "created_time": fill.get("created_time"),
                                    "is_taker": fill.get("is_taker", True),
                                }
                            )

                    await connection_manager.send_json(
                        websocket, {"type": "fills", "fills": fills_data}, compress=True
                    )

                elif action == "get_orders":
                    status = data.get("status", "resting")
                    orders_result = await kalshi_client.get_orders(status=status)
                    orders = orders_result.get("orders", [])

                    orders_data = []
                    if orders:
                        tickers = list(set(order["ticker"] for order in orders))
                        market_data = await kalshi_client.get_markets_batch(tickers)
                        for order in orders:
                            ticker = order["ticker"]
                            title = ticker
                            yes_sub_title = None
                            no_sub_title = None

                            if ticker in market_data and market_data[ticker]:
                                market = market_data[ticker].get("market", {})
                                title = market.get("title", ticker)
                                yes_sub_title = market.get("yes_sub_title")
                                no_sub_title = market.get("no_sub_title")

                            order_status = order.get("status", "")
                            if order_status == "executed":
                                count = order.get("fill_count", 0)
                            else:
                                count = order.get("remaining_count", 0) or order.get(
                                    "initial_count", 0
                                )

                            yes_price = order.get("yes_price")
                            no_price = order.get("no_price")

                            orders_data.append(
                                {
                                    "order_id": order.get("order_id"),
                                    "ticker": ticker,
                                    "title": title,
                                    "yes_sub_title": yes_sub_title,
                                    "no_sub_title": no_sub_title,
                                    "action": order.get("action"),
                                    "side": order.get("side"),
                                    "count": count,
                                    "type": order.get("type"),
                                    "yes_price": yes_price,
                                    "no_price": no_price,
                                    "status": order_status,
                                }
                            )

                    await connection_manager.send_json(
                        websocket,
                        {"type": "orders", "orders": orders_data},
                        compress=True,
                    )

                elif action == "lookup_ticker":
                    ticker = data.get("ticker", "")
                    market = await kalshi_client.get_market_by_ticker(ticker)

                    await connection_manager.send_json(
                        websocket,
                        {
                            "type": "ticker_lookup",
                            "ticker": ticker.upper(),
                            "market": market if market else None,
                        },
                        compress=False,
                    )

                elif action == "get_orderbook":
                    ticker = data.get("ticker")
                    market = await kalshi_client.get_market(ticker)

                    market_info = market.get("market", {})

                    yes_price = market_info.get("yes_price") or market_info.get(
                        "last_price"
                    )
                    no_price = market_info.get("no_price")

                    if no_price is None and yes_price is not None:
                        no_price = 100 - yes_price

                    await connection_manager.send_json(
                        websocket,
                        {
                            "type": "orderbook",
                            "ticker": ticker,
                            "title": market_info.get("title", ticker),
                            "yes_price": yes_price,
                            "no_price": no_price,
                        },
                        compress=False,
                    )

                    await market_service.subscribe_to_market(websocket, ticker)

                elif action == "place_order":
                    try:
                        result = await kalshi_client.place_order(
                            ticker=data.get("ticker"),
                            action=data.get("order_action"),
                            side=data.get("side"),
                            count=data.get("count"),
                            order_type=data.get("order_type", "limit"),
                            yes_price=data.get("yes_price"),
                            no_price=data.get("no_price"),
                        )

                        order_id = result.get("order", {}).get("order_id", "Unknown")

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "order_placed",
                                "success": True,
                                "order_id": order_id,
                            },
                            compress=False,
                        )

                    except Exception as api_error:
                        error_message = str(api_error)
                        if hasattr(api_error, "message"):
                            error_message = api_error.message

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "order_placed",
                                "success": False,
                                "error": error_message,
                            },
                            compress=False,
                        )

                elif action == "cancel_order":
                    try:
                        order_id = data.get("order_id")
                        await kalshi_client.cancel_order(order_id)

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "order_cancelled",
                                "success": True,
                                "order_id": order_id,
                            },
                            compress=False,
                        )

                    except Exception as api_error:
                        error_message = str(api_error)
                        if hasattr(api_error, "message"):
                            error_message = api_error.message

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "order_cancelled",
                                "success": False,
                                "error": error_message,
                            },
                            compress=False,
                        )

                elif action == "get_metrics":
                    client_metrics = kalshi_client.get_metrics()
                    server_metrics = performance_monitor.get_metrics()

                    await connection_manager.send_json(
                        websocket,
                        {
                            "type": "metrics",
                            "client": client_metrics,
                            "server": server_metrics,
                        },
                        compress=True,
                    )

                elif action == "get_hotkeys":
                    project_root = Path(__file__).parent.parent.parent.parent
                    hotkeys_file = (
                        project_root / "src" / "kalshi" / "tools" / "hotkeys.json"
                    )

                    try:
                        with open(hotkeys_file, "r", encoding="utf-8") as f:
                            hotkeys_config = json.load(f)

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "hotkeys",
                                "hotkeys": hotkeys_config.get("hotkeys", {}),
                            },
                            compress=False,
                        )
                    except FileNotFoundError:
                        await connection_manager.send_json(
                            websocket,
                            {"type": "error", "message": "hotkeys.json not found"},
                            compress=False,
                        )

                elif action == "start_hotkey_bot":
                    result = await hotkey_bot_manager.start_bot()
                    await connection_manager.send_json(
                        websocket,
                        {
                            "type": "bot_status",
                            **result,
                            **hotkey_bot_manager.get_status(),
                        },
                        compress=False,
                    )

                elif action == "stop_hotkey_bot":
                    result = await hotkey_bot_manager.stop_bot()
                    await connection_manager.send_json(
                        websocket,
                        {
                            "type": "bot_status",
                            **result,
                            **hotkey_bot_manager.get_status(),
                        },
                        compress=False,
                    )

                elif action == "get_bot_status":
                    status = hotkey_bot_manager.get_status()
                    await connection_manager.send_json(
                        websocket,
                        {"type": "bot_status", **status},
                        compress=False,
                    )

                elif action == "bot_execute_hotkey":
                    keyword = data.get("keyword")
                    result = await hotkey_bot_manager.execute_hotkey(keyword)
                    await connection_manager.send_json(
                        websocket,
                        {"type": "bot_hotkey_executed", **result},
                        compress=False,
                    )

                elif action == "generate_hotkeys":
                    series_ticker = data.get("series_ticker", "").strip().upper()
                    share_count = data.get("share_count", 200)

                    if not series_ticker:
                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "hotkey_generation_result",
                                "success": False,
                                "error": "Series ticker is required",
                            },
                            compress=False,
                        )
                        continue

                    try:
                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "hotkey_generation_status",
                                "message": f"Fetching markets for {series_ticker}...",
                            },
                            compress=False,
                        )

                        markets = await asyncio.get_event_loop().run_in_executor(
                            None, fetch_markets_by_pattern, series_ticker
                        )

                        if not markets:
                            await connection_manager.send_json(
                                websocket,
                                {
                                    "type": "hotkey_generation_result",
                                    "success": False,
                                    "error": f"No markets found for {series_ticker}",
                                },
                                compress=False,
                            )
                            continue

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "hotkey_generation_status",
                                "message": f"Generating {len(markets)} hotkeys...",
                            },
                            compress=False,
                        )

                        config = generate_hotkeys_config(
                            markets, default_count=share_count
                        )

                        if not config["hotkeys"]:
                            await connection_manager.send_json(
                                websocket,
                                {
                                    "type": "hotkey_generation_result",
                                    "success": False,
                                    "error": "No hotkeys could be generated",
                                },
                                compress=False,
                            )
                            continue

                        save_hotkeys_config(config)

                        bot_was_running = hotkey_bot_manager.is_running
                        if bot_was_running:
                            await hotkey_bot_manager.stop_bot()
                            await asyncio.sleep(0.5)

                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "hotkey_generation_result",
                                "success": True,
                                "message": f"Generated {len(config['hotkeys'])} hotkeys",
                                "hotkey_count": len(config["hotkeys"]),
                                "bot_was_stopped": bot_was_running,
                            },
                            compress=False,
                        )

                        status = hotkey_bot_manager.get_status()
                        await connection_manager.send_json(
                            websocket,
                            {"type": "bot_status", **status},
                            compress=False,
                        )

                    except Exception as e:
                        logger.error("Error generating hotkeys: %s", e, exc_info=True)
                        await connection_manager.send_json(
                            websocket,
                            {
                                "type": "hotkey_generation_result",
                                "success": False,
                                "error": str(e),
                            },
                            compress=False,
                        )

                else:
                    await connection_manager.send_json(
                        websocket,
                        {"type": "error", "message": f"Unknown action: {action}"},
                        compress=False,
                    )

                duration = time.time() - start_time
                performance_monitor.record_request(duration, success=True)

            except Exception as e:
                logger.error("WebSocket error: %s\n%s", e, traceback.format_exc())
                performance_monitor.record_request(0, success=False)

                await connection_manager.send_json(
                    websocket, {"type": "error", "message": str(e)}, compress=False
                )

    except WebSocketDisconnect:
        await connection_manager.disconnect(websocket)
    except Exception as e:
        logger.error("WebSocket connection error: %s", e)
        await connection_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
        loop="uvloop",
        access_log=False,
    )
