#!/usr/bin/env python3
"""Kalshi Trading Dashboard WebSocket server - fast, clean, simple."""

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config.constants import (
    DEFAULT_FILLS_LIMIT,
    KALSHI_WS_URL,
    PROJECT_ROOT,
)
from src.kalshi.clients.kalshi_client_async import AsyncKalshiClient
from src.kalshi.clients.kalshi_websocket_client import KalshiWebSocketClient
from src.kalshi.storage.trade_history import TradeHistory
from src.kalshi.tools.generate_hotkeys import (
    fetch_markets_by_pattern,
    generate_hotkeys_config,
    save_hotkeys_config,
)
from src.kalshi.tools.performance_monitor import get_monitor

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Validate environment
required_env_vars = ["KALSHI_API_KEY_ID"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    logger.error("Create a .env file with: KALSHI_API_KEY_ID=your_key_here")
    sys.exit(1)

if not Path("kalshi_private_key.pem").exists():
    logger.error("Private key file not found: kalshi_private_key.pem")
    logger.error("Place your Kalshi private key file in the project root")
    sys.exit(1)


async def enrich_items_with_market_data(
    items: list[Dict],
    kalshi_client: AsyncKalshiClient,
    ticker_key: str = "ticker",
    enrich_prices: bool = False,
) -> None:
    """
    Enrich items with market metadata (title, subtitles, optionally prices, status).

    Mutates items in-place by adding market data fields.
    """
    if not items:
        return

    tickers = list(set(item[ticker_key] for item in items if item.get(ticker_key)))
    if not tickers:
        return

    market_data = await kalshi_client.get_markets_batch(tickers)

    for item in items:
        ticker = item.get(ticker_key)
        if not ticker or ticker not in market_data or not market_data[ticker]:
            item["title"] = ticker
            item["yes_sub_title"] = None
            item["no_sub_title"] = None
            item["market_status"] = None
            continue

        market = market_data[ticker].get("market", {})
        item["title"] = market.get("title", ticker)
        item["yes_sub_title"] = market.get("yes_sub_title")
        item["no_sub_title"] = market.get("no_sub_title")
        item["market_status"] = market.get("status")

        if enrich_prices:
            yes_price = market.get("yes_price") or market.get("last_price")
            item["yes_price"] = yes_price
            item["no_price"] = market.get("no_price") or (
                100 - yes_price if yes_price is not None else None
            )


async def get_enriched_positions() -> tuple[list[Dict], int]:
    """
    Get positions enriched with prices and current_value calculated.
    Returns (positions, total_positions_value_cents).
    Single source of truth for position value calculation.
    """
    global kalshi_client

    positions = await kalshi_client.get_positions()
    market_positions = positions.get("market_positions", [])

    active_positions = [p for p in market_positions if p.get("position", 0) != 0]

    await enrich_items_with_market_data(
        active_positions, kalshi_client, enrich_prices=True
    )

    total_value = 0
    for pos in active_positions:
        position = pos.get("position", 0)
        if position == 0:
            pos["current_value"] = 0
            continue

        side = "yes" if position > 0 else "no"
        effective_price = pos.get(f"{side}_price")
        market_status = pos.get("market_status")

        # Calculate current value
        if effective_price is None:
            pos["current_value"] = 0
        else:
            contracts = abs(position)
            current_value = contracts * effective_price
            pos["current_value"] = current_value

            # Exclude closed/settled markets from total (awaiting settlement)
            # This matches Kalshi's official portfolio calculation
            if market_status not in ("closed", "settled"):
                total_value += current_value

        pos["side"] = side.upper()
        pos["contracts"] = abs(position)
        pos["effective_price"] = effective_price

    return active_positions, total_value


# Module-level state for single-user dashboard
active_websocket: Optional[WebSocket] = None
kalshi_client: Optional[AsyncKalshiClient] = None
kalshi_ws_client: Optional[KalshiWebSocketClient] = None
hotkey_bot_running = False
market_series_ticker: Optional[str] = None
trade_history: Optional[TradeHistory] = None


async def send_to_client(data: Dict) -> None:
    """Send JSON data to the connected client."""
    global active_websocket
    if active_websocket:
        try:
            await active_websocket.send_json(data)
        except Exception as e:
            logger.error(f"Failed to send to client: {e}", exc_info=True)
            active_websocket = None


async def execute_hotkey(keyword: str) -> Dict[str, Any]:
    """Execute hotkey - load config and place order."""
    try:
        hotkeys_file = PROJECT_ROOT / "src" / "kalshi" / "tools" / "hotkeys.json"

        try:
            with open(hotkeys_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except FileNotFoundError:
            return {"success": False, "error": "hotkeys.json not found"}

        hotkeys = config.get("hotkeys", {})
        defaults = config.get("defaults", {})

        keyword_normalized = keyword.lower().strip()

        if keyword_normalized not in hotkeys:
            return {"success": False, "error": f"Unknown hotkey: {keyword}"}

        hotkey_config = hotkeys[keyword_normalized]

        ticker = hotkey_config["ticker"]
        side = hotkey_config.get("side", defaults.get("side", "yes"))
        action = hotkey_config.get("action", defaults.get("action", "buy"))
        count = hotkey_config.get("count", defaults.get("count", 100))
        order_type = hotkey_config.get("type", defaults.get("type", "limit"))

        yes_price = hotkey_config.get("yes_price")
        no_price = hotkey_config.get("no_price")

        if yes_price is None and no_price is None:
            aggressive_price = 99 if action == "buy" else 1
            if side == "yes":
                yes_price = aggressive_price
            else:
                no_price = aggressive_price

        result = await kalshi_client.place_order(
            ticker=ticker,
            action=action,
            side=side,
            count=count,
            order_type=order_type,
            yes_price=yes_price,
            no_price=no_price,
        )

        logger.info(f"Hotkey executed: {keyword} -> {ticker}")
        return {"success": True, "keyword": keyword, "order": result}

    except Exception as e:
        logger.error("Error executing hotkey: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def start_hotkey_bot() -> Dict[str, Any]:
    """Start hotkey bot (just enable it)."""
    global hotkey_bot_running, market_series_ticker

    if hotkey_bot_running:
        return {"success": False, "error": "Bot is already running"}

    try:
        hotkeys_file = PROJECT_ROOT / "src" / "kalshi" / "tools" / "hotkeys.json"

        try:
            with open(hotkeys_file, "r", encoding="utf-8") as f:
                hotkeys_config = json.load(f)
                hotkeys = hotkeys_config.get("hotkeys", {})

                if hotkeys:
                    first_hotkey = next(iter(hotkeys.values()))
                    ticker = first_hotkey.get("ticker")
                    if ticker:
                        market_series_ticker = (
                            ticker.split("-")[0] if "-" in ticker else ticker
                        )
        except Exception as e:
            logger.error("Error loading hotkeys config: %s", e, exc_info=True)
            market_series_ticker = None

        hotkey_bot_running = True
        return {"success": True, "message": "Hotkey bot started"}

    except Exception as e:
        logger.error("Error starting hotkey bot: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def stop_hotkey_bot() -> Dict[str, Any]:
    """Stop hotkey bot (just disable it)."""
    global hotkey_bot_running, market_series_ticker

    if not hotkey_bot_running:
        return {"success": False, "error": "Bot is not running"}

    hotkey_bot_running = False
    market_series_ticker = None
    return {"success": True, "message": "Hotkey bot stopped"}


def get_bot_status() -> Dict[str, Any]:
    """Get bot status."""
    return {
        "is_running": hotkey_bot_running,
        "market_series_ticker": market_series_ticker,
    }


async def handle_kalshi_ws_message(data: Dict[str, Any]) -> None:
    """Forward Kalshi WebSocket messages to frontend client."""
    msg_type = data.get("type") or data.get("msg")

    if msg_type == "fill":
        await send_to_client({"type": "new_fill", "data": data})
    elif msg_type in ["ticker", "trade"]:
        ticker = data.get("ticker") or data.get("market_ticker")
        if ticker:
            market = data.get("market", {})
            yes_price = market.get("yes_price") or market.get("last_price")
            if yes_price is not None and market.get("no_price") is None:
                market["no_price"] = 100 - yes_price

            await send_to_client(
                {"type": "market_update", "ticker": ticker, "data": data}
            )
    elif msg_type == "orderbook_delta" or msg_type == "orderbook_snapshot":
        ticker = data.get("ticker") or data.get("market_ticker")
        if ticker:
            await send_to_client(
                {"type": "orderbook_update", "ticker": ticker, "data": data}
            )


def handle_kalshi_ws_status(connected: bool) -> None:
    """Notify frontend about Kalshi WebSocket connection status changes."""
    asyncio.create_task(
        send_to_client({"type": "kalshi_ws_status", "connected": connected})
    )


async def daily_snapshot_task() -> None:
    """Background task to save daily portfolio snapshots (once per day)."""
    global trade_history, kalshi_client

    while True:
        try:
            await asyncio.sleep(86400)  # Sleep 24 hours between checks

            if not trade_history or not kalshi_client:
                continue

            if trade_history.has_snapshot_today():  # Skip if already saved today
                continue

            try:
                balance_result = await kalshi_client.get_balance()
                cash_cents = balance_result.get("balance", 0)

                _, positions_value_cents = await get_enriched_positions()

                success = trade_history.save_snapshot(cash_cents, positions_value_cents)
                if success:
                    total_value = (cash_cents + positions_value_cents) / 100
                    logger.info(f"Daily snapshot saved: ${total_value:.2f}")

            except Exception as e:
                logger.error(f"Failed to capture daily snapshot: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in daily snapshot task: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifecycle manager"""
    global kalshi_client, kalshi_ws_client, trade_history

    logger.info("Starting Kalshi Trading Dashboard...")

    trade_history = TradeHistory()
    logger.info(
        f"Portfolio history initialized: {trade_history.get_snapshot_count()} snapshots stored"
    )

    kalshi_client = AsyncKalshiClient()

    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key = kalshi_client.private_key

    kalshi_ws_client = KalshiWebSocketClient(
        api_key_id=api_key_id,
        private_key=private_key,
        ws_url=KALSHI_WS_URL,
        on_message=handle_kalshi_ws_message,
        on_status_change=handle_kalshi_ws_status,
    )

    await kalshi_ws_client.start()

    snapshot_task = asyncio.create_task(daily_snapshot_task())

    logger.info("Dashboard ready at http://localhost:8000")
    logger.info("Kalshi WebSocket connected - real-time updates enabled")

    yield

    logger.info("Shutting down...")
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass
    if hotkey_bot_running:
        await stop_hotkey_bot()
    if kalshi_ws_client:
        await kalshi_ws_client.stop()
    if kalshi_client:
        await kalshi_client.close()
    if active_websocket:
        try:
            await active_websocket.close()
        except Exception as e:
            logger.debug("WebSocket close error during shutdown: %s", e)

    logger.info("Performance metrics:")
    get_monitor().print_summary()

    logger.info("Shutdown complete")


app = FastAPI(lifespan=lifespan, title="Kalshi Trading Dashboard")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def get_dashboard():
    """Serve dashboard HTML."""
    html_path = Path(__file__).parent / "dashboard.html"
    return FileResponse(html_path)


@app.get("/api/health")
async def health_check():
    """Basic health check."""
    issues = []

    if not kalshi_client:
        issues.append("Kalshi client not initialized")

    status = "healthy" if not issues else "degraded"

    return {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issues": issues,
        "services": {
            "kalshi_client": kalshi_client is not None,
            "active_connection": active_websocket is not None,
            "hotkey_bot": hotkey_bot_running,
        },
    }


@app.get("/api/metrics")
async def get_metrics():
    """Return performance metrics."""
    return get_monitor().get_summary()


async def handle_get_balance(data: Dict) -> Dict:
    """Get balance with portfolio calculations."""
    balance_result = await kalshi_client.get_balance()
    cash_cents = balance_result.get("balance", 0)

    _, positions_value_cents = await get_enriched_positions()

    portfolio_value_cents = cash_cents + positions_value_cents

    return {
        "type": "balance",
        "cash_cents": cash_cents,
        "positions_value_cents": positions_value_cents,
        "portfolio_value_cents": portfolio_value_cents,
    }


async def handle_get_positions(data: Dict) -> Dict:
    """Get positions."""
    active_positions, total_positions_value_cents = await get_enriched_positions()

    return {
        "type": "positions",
        "positions": active_positions,
        "total_positions_value_cents": total_positions_value_cents,
    }


async def handle_get_fills(data: Dict) -> Dict:
    """Get fills."""
    limit = data.get("limit", DEFAULT_FILLS_LIMIT)
    fills_result = await kalshi_client.get_fills(limit=limit)
    fills = fills_result.get("fills", [])
    fills_data = []

    await enrich_items_with_market_data(fills, kalshi_client)

    for fill in fills:
        fill_side = fill.get("side")
        fill_action = fill.get("action")

        # API flips side for sell orders to show the counterparty perspective
        # E.g. selling NO @ 99¢: API returns side="yes", action="sell", yes_price=1¢
        # We need to flip it back to show what actually happened: sold NO @ 99¢
        if fill_action == "sell":
            display_side = "no" if fill_side == "yes" else "yes"
            price_cents = fill.get(f"{display_side}_price", 0)
        else:
            display_side = fill_side
            price_cents = fill.get(f"{fill_side}_price", 0)

        fills_data.append(
            {
                "ticker": fill["ticker"],
                "title": fill.get("title", fill["ticker"]),
                "yes_sub_title": fill.get("yes_sub_title"),
                "no_sub_title": fill.get("no_sub_title"),
                "action": fill_action,
                "side": display_side,
                "count": fill.get("count"),
                "price": price_cents,
                "created_time": fill.get("created_time"),
                "is_taker": fill.get("is_taker", True),
            }
        )

    return {"type": "fills", "fills": fills_data}


async def handle_get_orders(data: Dict) -> Dict:
    """Get orders."""
    status = data.get("status", "resting")
    orders_result = await kalshi_client.get_orders(status=status)
    orders = orders_result.get("orders", [])

    await enrich_items_with_market_data(orders, kalshi_client)

    orders_data = []
    for order in orders:
        order_status = order.get("status", "")
        if order_status == "executed":
            count = order.get("fill_count", 0)
        else:
            count = order.get("remaining_count")
            if count is None:
                count = order.get("initial_count", 0)

        yes_price = order.get("yes_price")
        no_price = order.get("no_price")

        orders_data.append(
            {
                "order_id": order.get("order_id"),
                "ticker": order["ticker"],
                "title": order.get("title", order["ticker"]),
                "yes_sub_title": order.get("yes_sub_title"),
                "no_sub_title": order.get("no_sub_title"),
                "action": order.get("action"),
                "side": order.get("side"),
                "count": count,
                "type": order.get("type"),
                "yes_price": yes_price,
                "no_price": no_price,
                "status": order_status,
            }
        )

    return {"type": "orders", "orders": orders_data}


async def handle_lookup_ticker(data: Dict) -> Dict:
    """Look up market by ticker."""
    ticker = data.get("ticker", "")
    market = await kalshi_client.get_market_by_ticker(ticker)

    return {
        "type": "ticker_lookup",
        "ticker": ticker.upper(),
        "market": market if market else None,
    }


async def handle_get_orderbook(data: Dict) -> Dict:
    """Get full orderbook and subscribe to real-time updates."""
    ticker = data.get("ticker")

    # Fetch market info and orderbook in parallel
    market_task = kalshi_client.get_market(ticker)
    orderbook_task = kalshi_client.get_orderbook(ticker, depth=10)

    market, orderbook_response = await asyncio.gather(market_task, orderbook_task)
    market_info = market.get("market", {})

    orderbook = orderbook_response.get(
        "orderbook", {}
    )  # Extract orderbook from nested response

    # Get last traded prices as fallback
    yes_price = market_info.get("yes_price") or market_info.get("last_price")
    no_price = market_info.get("no_price")
    if no_price is None and yes_price is not None:
        no_price = 100 - yes_price

    # Subscribe to live updates
    if kalshi_ws_client:
        await kalshi_ws_client.subscribe_to_ticker(ticker)

    # Handle None values - Kalshi returns None when no bids exist
    yes_bids = orderbook.get("yes") or []
    no_bids = orderbook.get("no") or []

    return {
        "type": "orderbook",
        "ticker": ticker,
        "title": market_info.get("title", ticker),
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_bids": yes_bids,  # [[price, size], ...]
        "no_bids": no_bids,  # [[price, size], ...]
    }


async def handle_unsubscribe_market(data: Dict) -> None:
    """Unsubscribe from market updates."""
    ticker = data.get("ticker")
    if ticker and kalshi_ws_client:
        await kalshi_ws_client.unsubscribe_from_ticker(ticker)


async def handle_quick_order(data: Dict) -> Dict:
    """Place quick order with max aggressive pricing."""
    try:
        with get_monitor().track_operation("order"):
            ticker = data.get("ticker")
            order_action = data.get("order_action")
            side = data.get("side")
            count = data.get("count")

            aggressive_price = (
                99 if order_action == "buy" else 1
            )  # Max aggressive pricing

            yes_price = aggressive_price if side == "yes" else None
            no_price = aggressive_price if side == "no" else None

            result = await kalshi_client.place_order(
                ticker=ticker,
                action=order_action,
                side=side,
                count=count,
                order_type="limit",
                yes_price=yes_price,
                no_price=no_price,
            )

            order_id = result["order"]["order_id"]
            return {"type": "order_placed", "success": True, "order_id": order_id}

    except Exception as api_error:
        return {"type": "order_placed", "success": False, "error": str(api_error)}


async def handle_place_order(data: Dict) -> Dict:
    """Place limit order."""
    try:
        with get_monitor().track_operation("order"):
            ticker = data.get("ticker")
            order_action = data.get("order_action")
            side = data.get("side")
            count = data.get("count")
            price = data.get("price")

            yes_price = price if side == "yes" else None
            no_price = price if side == "no" else None

            result = await kalshi_client.place_order(
                ticker=ticker,
                action=order_action,
                side=side,
                count=count,
                order_type="limit",
                yes_price=yes_price,
                no_price=no_price,
            )

            order_id = result["order"]["order_id"]
            return {"type": "order_placed", "success": True, "order_id": order_id}

    except Exception as api_error:
        return {"type": "order_placed", "success": False, "error": str(api_error)}


async def handle_cancel_order(data: Dict) -> Dict:
    """Cancel order by ID."""
    try:
        order_id = data.get("order_id")
        await kalshi_client.cancel_order(order_id)

        return {"type": "order_cancelled", "success": True, "order_id": order_id}

    except Exception as api_error:
        return {"type": "order_cancelled", "success": False, "error": str(api_error)}


async def handle_get_hotkeys(data: Dict) -> Dict:
    """Get configured hotkeys."""
    hotkeys_file = PROJECT_ROOT / "src" / "kalshi" / "tools" / "hotkeys.json"

    try:
        with open(hotkeys_file, "r", encoding="utf-8") as f:
            hotkeys_config = json.load(f)

        return {"type": "hotkeys", "hotkeys": hotkeys_config.get("hotkeys", {})}
    except FileNotFoundError:
        return {"type": "error", "message": "hotkeys.json not found"}


async def handle_start_hotkey_bot(data: Dict) -> Dict:
    """Start hotkey bot."""
    result = await start_hotkey_bot()
    return {"type": "bot_status", **result, **get_bot_status()}


async def handle_stop_hotkey_bot(data: Dict) -> Dict:
    """Stop hotkey bot."""
    result = await stop_hotkey_bot()
    return {"type": "bot_status", **result, **get_bot_status()}


async def handle_get_bot_status(data: Dict) -> Dict:
    """Get bot status."""
    status = get_bot_status()
    return {"type": "bot_status", **status}


async def handle_bot_execute_hotkey(data: Dict) -> Dict:
    """Execute bot hotkey."""
    if not hotkey_bot_running:
        return {
            "type": "bot_hotkey_executed",
            "success": False,
            "error": "Bot is not running",
        }

    keyword = data.get("keyword")
    result = await execute_hotkey(keyword)
    return {"type": "bot_hotkey_executed", **result}


async def handle_take_snapshot(data: Dict) -> Dict:
    """Take manual portfolio snapshot."""
    if not trade_history:
        return {"type": "error", "message": "Portfolio history not initialized"}

    balance_result = await kalshi_client.get_balance()
    cash_cents = balance_result.get("balance", 0)

    _, positions_value_cents = await get_enriched_positions()

    trade_history.save_snapshot(cash_cents, positions_value_cents)
    logger.info(
        f"Manual snapshot saved: ${(cash_cents + positions_value_cents) / 100:.2f}"
    )

    analytics = trade_history.get_analytics()
    return {"type": "analytics", "data": analytics}


async def handle_get_analytics(data: Dict) -> Dict:
    """Get portfolio analytics."""
    if not trade_history:
        return {"type": "analytics", "error": "Portfolio history not initialized"}

    analytics = trade_history.get_analytics()
    return {"type": "analytics", "data": analytics}


async def handle_generate_hotkeys(data: Dict) -> Dict:
    """Generate hotkeys from series ticker."""
    series_ticker = data.get("series_ticker", "").strip().upper()
    share_count = data.get("share_count", 200)

    if not series_ticker:
        return {
            "type": "hotkey_generation_result",
            "success": False,
            "error": "Series ticker is required",
        }

    try:
        await send_to_client(
            {
                "type": "hotkey_generation_status",
                "message": f"Fetching markets for {series_ticker}...",
            }
        )

        markets = await fetch_markets_by_pattern(series_ticker)

        if not markets:
            return {
                "type": "hotkey_generation_result",
                "success": False,
                "error": f"No markets found for {series_ticker}",
            }

        await send_to_client(
            {
                "type": "hotkey_generation_status",
                "message": f"Generating {len(markets)} hotkeys...",
            }
        )

        config = generate_hotkeys_config(markets, default_count=share_count)

        if not config["hotkeys"]:
            return {
                "type": "hotkey_generation_result",
                "success": False,
                "error": "No hotkeys could be generated",
            }

        save_hotkeys_config(config)

        bot_was_running = hotkey_bot_running
        if bot_was_running:
            await stop_hotkey_bot()
            await asyncio.sleep(0.5)

        status = get_bot_status()
        await send_to_client({"type": "bot_status", **status})

        return {
            "type": "hotkey_generation_result",
            "success": True,
            "message": f"Generated {len(config['hotkeys'])} hotkeys",
            "hotkey_count": len(config["hotkeys"]),
            "bot_was_stopped": bot_was_running,
        }

    except Exception as e:
        logger.error("Error generating hotkeys: %s", e, exc_info=True)
        return {
            "type": "hotkey_generation_result",
            "success": False,
            "error": str(e),
        }


# Map actions to handler functions
ACTION_HANDLERS = {
    "get_balance": handle_get_balance,
    "get_positions": handle_get_positions,
    "get_fills": handle_get_fills,
    "get_orders": handle_get_orders,
    "lookup_ticker": handle_lookup_ticker,
    "get_orderbook": handle_get_orderbook,
    "unsubscribe_market": handle_unsubscribe_market,
    "quick_order": handle_quick_order,
    "place_order": handle_place_order,
    "cancel_order": handle_cancel_order,
    "get_hotkeys": handle_get_hotkeys,
    "start_hotkey_bot": handle_start_hotkey_bot,
    "stop_hotkey_bot": handle_stop_hotkey_bot,
    "get_bot_status": handle_get_bot_status,
    "bot_execute_hotkey": handle_bot_execute_hotkey,
    "take_snapshot": handle_take_snapshot,
    "get_analytics": handle_get_analytics,
    "generate_hotkeys": handle_generate_hotkeys,
}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time communication.
    Routes incoming messages to appropriate handlers.

    Single-connection design: Opening dashboard in multiple tabs will disconnect previous tabs.
    """
    global active_websocket

    # Close previous connection if exists
    if active_websocket is not None:
        try:
            await active_websocket.close()
        except Exception as e:
            logger.debug("Error closing previous WebSocket: %s", e)

    await websocket.accept()
    active_websocket = websocket

    try:
        await send_to_client(
            {
                "type": "connection",
                "status": "connected",
                "message": "Connected to Kalshi Trading Dashboard",
            },
        )

        while True:
            try:
                raw_message = await websocket.receive()
                if raw_message.get("type") == "websocket.disconnect":
                    break

                if "text" in raw_message:
                    message_text = raw_message["text"]
                    if len(message_text) > 10_000:
                        logger.warning("Message too large: %d bytes", len(message_text))
                        await send_to_client(
                            {"type": "error", "message": "Message too large"}
                        )
                        continue

                    data = json.loads(message_text)
                else:
                    continue

                action = data.get("action")

                handler = ACTION_HANDLERS.get(action)

                if handler:
                    response = await handler(data)
                    if response:
                        await send_to_client(response)

                else:
                    await send_to_client(
                        {"type": "error", "message": f"Unknown action: {action}"}
                    )

            except Exception as e:
                logger.error("WebSocket error: %s\n%s", e, traceback.format_exc())
                await send_to_client({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        active_websocket = None
    except Exception as e:
        logger.error("WebSocket connection error: %s", e, exc_info=True)
        active_websocket = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",  # Localhost only - prevents unauthorized network access
        port=8000,
        log_level="info",
        access_log=False,
    )
