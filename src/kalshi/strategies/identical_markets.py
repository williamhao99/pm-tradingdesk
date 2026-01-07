#!/usr/bin/env python3
"""Arbitrage monitor for paired Kalshi markets.

Detects and executes arbitrage when two related markets are mispriced.
Supports same_yes (YES_A = YES_B) and opposite (YES_A = NO_B) relationships.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.kalshi.auth import load_private_key
from src.kalshi.clients.kalshi_client_async import AsyncKalshiClient
from src.kalshi.clients.kalshi_websocket_client import KalshiWebSocketClient
from src.kalshi.utils.fees import calculate_fee, taker_fee_cents
from src.kalshi.utils.orderbook import Orderbook

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class RelationshipType(Enum):
    """How the two markets relate to each other."""

    SAME_YES = "same_yes"  # YES_A = YES_B
    OPPOSITE = "opposite"  # YES_A = NO_B


class TradingMode(Enum):
    """How to capture the arbitrage."""

    MONITOR = "monitor"  # Just log opportunities, don't trade
    TAKER = "taker"  # Take liquidity (cross the spread) - always fills


@dataclass
class ArbOpportunity:
    """Detected arbitrage opportunity."""

    buy_ticker: str
    buy_side: str  # "yes" or "no"
    buy_price: int  # cents
    hedge_ticker: str
    hedge_side: str  # "yes" or "no"
    hedge_price: int  # cents
    qty: int
    gross_edge_cents: int  # Before fees
    buy_fee_cents: float
    hedge_fee_cents: float
    net_edge_cents: float  # After fees
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"ARB: Buy {self.qty} {self.buy_side.upper()} @ {self.buy_price}c on {self.buy_ticker}, "
            f"Hedge {self.hedge_side.upper()} @ {self.hedge_price}c on {self.hedge_ticker} | "
            f"Gross={self.gross_edge_cents}c, Fees={self.buy_fee_cents + self.hedge_fee_cents:.1f}c, "
            f"Net={self.net_edge_cents:.1f}c"
        )


@dataclass
class Config:
    """Strategy configuration."""

    ticker_a: str
    ticker_b: str
    relationship: RelationshipType = RelationshipType.SAME_YES
    mode: TradingMode = TradingMode.MONITOR
    qty: int = 1
    min_edge_cents: float = 1.0  # Minimum net edge to trigger (after fees)
    max_exposure_cents: int = 10000  # Max total exposure in cents ($100 default)
    max_unwind_slippage_cents: int = (
        15  # Max loss per contract on unwind before warning
    )
    dry_run: bool = True  # Don't actually place orders


# -----------------------------------------------------------------------------
# Arbitrage Detection
# -----------------------------------------------------------------------------


def detect_same_yes_arb(
    book_a: Orderbook,
    book_b: Orderbook,
    cfg: Config,
) -> Optional[ArbOpportunity]:
    """Detect arbitrage for same_yes relationship (YES_A = YES_B)."""
    # Direction 1: Buy YES on A, Buy NO on B
    a_yes_ask = book_a.best_yes_ask()
    b_no_ask = book_b.best_no_ask()  # = 100 - YES_B_bid

    if a_yes_ask and b_no_ask and (a_yes_ask + b_no_ask) < 100:
        qty = min(cfg.qty, book_a.yes_ask_size(), book_b.no_ask_size())
        if qty > 0:
            gross = (100 - a_yes_ask - b_no_ask) * qty
            fee_buy = calculate_fee(a_yes_ask, qty, False)
            fee_hedge = calculate_fee(b_no_ask, qty, False)
            net = gross - fee_buy - fee_hedge

            if net >= cfg.min_edge_cents:
                return ArbOpportunity(
                    buy_ticker=book_a.ticker,
                    buy_side="yes",
                    buy_price=a_yes_ask,
                    hedge_ticker=book_b.ticker,
                    hedge_side="no",
                    hedge_price=b_no_ask,
                    qty=qty,
                    gross_edge_cents=gross,
                    buy_fee_cents=fee_buy,
                    hedge_fee_cents=fee_hedge,
                    net_edge_cents=net,
                )

    # Direction 2: Buy YES on B, Buy NO on A
    b_yes_ask = book_b.best_yes_ask()
    a_no_ask = book_a.best_no_ask()  # = 100 - YES_A_bid

    if b_yes_ask and a_no_ask and (b_yes_ask + a_no_ask) < 100:
        qty = min(cfg.qty, book_b.yes_ask_size(), book_a.no_ask_size())
        if qty > 0:
            gross = (100 - b_yes_ask - a_no_ask) * qty
            fee_buy = calculate_fee(b_yes_ask, qty, False)
            fee_hedge = calculate_fee(a_no_ask, qty, False)
            net = gross - fee_buy - fee_hedge

            if net >= cfg.min_edge_cents:
                return ArbOpportunity(
                    buy_ticker=book_b.ticker,
                    buy_side="yes",
                    buy_price=b_yes_ask,
                    hedge_ticker=book_a.ticker,
                    hedge_side="no",
                    hedge_price=a_no_ask,
                    qty=qty,
                    gross_edge_cents=gross,
                    buy_fee_cents=fee_buy,
                    hedge_fee_cents=fee_hedge,
                    net_edge_cents=net,
                )

    return None


def detect_opposite_arb(
    book_a: Orderbook,
    book_b: Orderbook,
    cfg: Config,
) -> Optional[ArbOpportunity]:
    """Detect arbitrage for opposite relationship (YES_A = NO_B)."""
    a_ask = book_a.best_yes_ask()
    b_ask = book_b.best_yes_ask()

    if a_ask and b_ask:
        total_cost = a_ask + b_ask

        if total_cost < 100:
            qty = min(cfg.qty, book_a.yes_ask_size(), book_b.yes_ask_size())
            if qty > 0:
                gross = (100 - total_cost) * qty
                fee_a = calculate_fee(a_ask, qty, False)
                fee_b = calculate_fee(b_ask, qty, False)
                net = gross - fee_a - fee_b

                if net >= cfg.min_edge_cents:
                    return ArbOpportunity(
                        buy_ticker=book_a.ticker,
                        buy_side="yes",
                        buy_price=a_ask,
                        hedge_ticker=book_b.ticker,
                        hedge_side="yes",
                        hedge_price=b_ask,
                        qty=qty,
                        gross_edge_cents=gross,
                        buy_fee_cents=fee_a,
                        hedge_fee_cents=fee_b,
                        net_edge_cents=net,
                    )

    return None


def detect_arb(
    book_a: Orderbook,
    book_b: Orderbook,
    cfg: Config,
) -> Optional[ArbOpportunity]:
    """Detect arbitrage based on configured relationship type."""
    if book_a.is_stale() or book_b.is_stale():
        return None

    if cfg.relationship == RelationshipType.SAME_YES:
        return detect_same_yes_arb(book_a, book_b, cfg)
    else:
        return detect_opposite_arb(book_a, book_b, cfg)


# -----------------------------------------------------------------------------
# Order Execution
# -----------------------------------------------------------------------------


class OrderExecutor:
    """Handles order placement via REST API using AsyncKalshiClient."""

    def __init__(
        self, rest_client: AsyncKalshiClient, cfg: Config, dry_run: bool = True
    ):
        self.client = rest_client
        self.cfg = cfg
        self.dry_run = dry_run
        self.orders_placed = 0
        self.orders_filled = 0

        # Position and exposure tracking
        self.positions: Dict[str, int] = (
            {}
        )  # ticker -> net contracts (positive = long YES)
        self.total_exposure_cents: int = 0  # Total money at risk
        self.total_cost_cents: int = 0  # Total cost basis

    def get_exposure(self) -> int:
        """Get current total exposure in cents."""
        return self.total_exposure_cents

    async def load_existing_positions(self, tracked_tickers: set) -> None:
        """Load existing positions from Kalshi for tracked tickers only."""
        try:
            result = await self.client.get_positions()
            positions = result.get("market_positions", [])

            for pos in positions:
                ticker = pos.get("ticker") or pos.get("market_ticker")
                if not ticker:
                    continue

                # Only track positions for the markets we're monitoring
                if ticker not in tracked_tickers:
                    continue

                # Position count (positive = long YES, negative = short YES)
                position = pos.get("position", 0)
                if position == 0:
                    continue

                # Market exposure is what Kalshi reports as money at risk
                exposure = pos.get("market_exposure", 0)  # in cents

                self.positions[ticker] = position
                self.total_exposure_cents += exposure

                logger.info(
                    f"Loaded position: {ticker} = {position} contracts, "
                    f"exposure=${exposure/100:.2f}"
                )

            if self.positions:
                logger.info(
                    f"Exposure on tracked markets: ${self.total_exposure_cents/100:.2f} "
                    f"(limit: ${self.cfg.max_exposure_cents/100:.2f})"
                )
            else:
                logger.info("No existing positions on tracked markets")

        except Exception as e:
            logger.warning(f"Could not load existing positions: {e}")

    def can_trade(self, cost_cents: int) -> bool:
        """Check if trade is within exposure limit."""
        new_exposure = self.total_exposure_cents + cost_cents
        if new_exposure > self.cfg.max_exposure_cents:
            logger.warning(
                f"Trade blocked: would exceed max exposure "
                f"(current=${self.total_exposure_cents/100:.2f}, "
                f"trade=${cost_cents/100:.2f}, "
                f"max=${self.cfg.max_exposure_cents/100:.2f})"
            )
            return False

        return True

    def record_fill(self, ticker: str, side: str, action: str, count: int, price: int):
        """Record a fill and update position/exposure tracking."""
        # Update position
        if ticker not in self.positions:
            self.positions[ticker] = 0

        # Determine position change
        if action == "buy":
            if side == "yes":
                self.positions[ticker] += count
            else:  # buy NO = short YES
                self.positions[ticker] -= count
            # Buying increases exposure
            cost = count * price
            self.total_exposure_cents += cost
            self.total_cost_cents += cost
        else:  # sell
            if side == "yes":
                self.positions[ticker] -= count
            else:  # sell NO = long YES
                self.positions[ticker] += count
            # Selling decreases exposure (realize some value)
            value = count * price
            self.total_exposure_cents = max(0, self.total_exposure_cents - value)

        logger.info(
            f"Position update: {ticker} = {self.positions[ticker]} contracts, "
            f"Total exposure: ${self.total_exposure_cents/100:.2f}"
        )

    def verify_fill(self, result: Optional[Dict], expected_qty: int) -> int:
        """Return number of contracts filled from an IOC order."""
        if not result:
            return 0

        # Dry run always simulates full fill
        if result.get("dry_run"):
            return expected_qty

        order = result.get("order", {})
        # Kalshi API uses "fill_count", not "filled_count"
        fill_count = order.get("fill_count", 0)

        if fill_count < expected_qty:
            logger.warning(
                f"IOC order partially filled: expected {expected_qty}, "
                f"got {fill_count}"
            )

        return fill_count

    async def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price: int,
        client_order_id: Optional[str] = None,
        time_in_force: str = "gtc",
    ) -> Optional[Dict]:
        """Place a limit order."""
        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would place: {action.upper()} {count} {side.upper()} "
                f"@ {price}c on {ticker} ({time_in_force.upper()})"
            )
            # Simulate full fill for dry run
            return {
                "dry_run": True,
                "ticker": ticker,
                "action": action,
                "side": side,
                "count": count,
                "price": price,
                "order": {"remaining_count": 0, "fill_count": count},
            }

        if not client_order_id:
            client_order_id = str(uuid.uuid4())

        try:
            if side == "yes":
                result = await self.client.place_order(
                    ticker=ticker,
                    action=action,
                    side=side,
                    count=count,
                    yes_price=price,
                    client_order_id=client_order_id,
                    time_in_force=time_in_force,
                )
            else:
                result = await self.client.place_order(
                    ticker=ticker,
                    action=action,
                    side=side,
                    count=count,
                    no_price=price,
                    client_order_id=client_order_id,
                    time_in_force=time_in_force,
                )

            order = result.get("order", {})
            filled = order.get("fill_count", 0)  # Kalshi uses "fill_count"
            remaining = order.get("remaining_count", count)
            logger.info(
                f"Order placed: {action.upper()} {count} {side.upper()} @ {price}c on {ticker} "
                f"({time_in_force.upper()}) - filled={filled}, remaining={remaining}"
            )
            self.orders_placed += 1
            return result
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None

    async def execute_arb(
        self,
        arb: ArbOpportunity,
        cfg: Config,
        books: Optional[Dict[str, "Orderbook"]] = None,
    ) -> Tuple[bool, bool]:
        """Execute arbitrage with concurrent IOC orders. Unwinds if only one leg fills."""
        trade_cost = arb.qty * (arb.buy_price + arb.hedge_price)

        if not self.can_trade(trade_cost):
            logger.warning(f"Arb skipped due to exposure limit")
            return (False, False)

        # Determine hedge side based on relationship type
        # SAME_YES: buy YES on A, buy NO on B (YES_A = YES_B)
        # OPPOSITE: buy YES on A, buy YES on B (YES_A = NO_B)
        hedge_side = "no" if cfg.relationship == RelationshipType.SAME_YES else "yes"

        # Execute both legs concurrently
        leg1_task = self.place_order(
            ticker=arb.buy_ticker,
            action="buy",
            side="yes",
            count=arb.qty,
            price=arb.buy_price,
            time_in_force="ioc",
        )
        leg2_task = self.place_order(
            ticker=arb.hedge_ticker,
            action="buy",
            side=hedge_side,
            count=arb.qty,
            price=arb.hedge_price,
            time_in_force="ioc",
        )

        leg1_result, leg2_result = await asyncio.gather(leg1_task, leg2_task)

        leg1_filled = self.verify_fill(leg1_result, arb.qty)
        leg2_filled = self.verify_fill(leg2_result, arb.qty)

        matched_qty = min(leg1_filled, leg2_filled)

        if matched_qty > 0:
            self.record_fill(arb.buy_ticker, "yes", "buy", matched_qty, arb.buy_price)
            self.record_fill(
                arb.hedge_ticker, hedge_side, "buy", matched_qty, arb.hedge_price
            )
            self.orders_filled += 2
            logger.info(f"Arb executed: {matched_qty} contracts matched")

            # Unwind any excess from partial fills
            if leg1_filled > matched_qty:
                excess = leg1_filled - matched_qty
                logger.warning(f"Unwinding {excess} excess YES contracts on leg 1...")
                await self._unwind_position(
                    arb.buy_ticker, "yes", excess, arb.buy_price, cfg, books
                )
            if leg2_filled > matched_qty:
                excess = leg2_filled - matched_qty
                logger.warning(
                    f"Unwinding {excess} excess {hedge_side.upper()} contracts on leg 2..."
                )
                await self._unwind_position(
                    arb.hedge_ticker, hedge_side, excess, arb.hedge_price, cfg, books
                )
            return (True, True)

        # Neither filled or both failed - unwind any partial fills
        if leg1_filled > 0:
            logger.warning("Only leg 1 filled - unwinding...")
            await self._unwind_position(
                arb.buy_ticker, "yes", leg1_filled, arb.buy_price, cfg, books
            )
        if leg2_filled > 0:
            logger.warning("Only leg 2 filled - unwinding...")
            await self._unwind_position(
                arb.hedge_ticker, hedge_side, leg2_filled, arb.hedge_price, cfg, books
            )
        if leg1_filled == 0 and leg2_filled == 0:
            logger.info("Neither leg filled - no position opened")
        return (False, False)

    async def _unwind_position(
        self,
        ticker: str,
        side: str,
        qty: int,
        entry_price: int,
        cfg: Config,
        books: Optional[Dict[str, "Orderbook"]] = None,
    ) -> bool:
        """Unwind a position at the best available price."""
        unwind_price = 1  # Fallback to floor price
        slippage = entry_price - 1  # Worst case slippage

        if books and ticker in books:
            book = books[ticker]
            # Get the appropriate bid based on what side we're selling
            best_bid = book.best_yes_bid() if side == "yes" else book.best_no_bid()

            if best_bid is not None:
                unwind_price = best_bid
                slippage = entry_price - best_bid

                if slippage > cfg.max_unwind_slippage_cents:
                    logger.error(
                        f"LARGE UNWIND SLIPPAGE: bought {side.upper()} @ {entry_price}c, "
                        f"best bid now {best_bid}c, slippage={slippage}c "
                        f"(max={cfg.max_unwind_slippage_cents}c). Proceeding anyway."
                    )
                elif slippage > 0:
                    logger.warning(
                        f"Unwind slippage: bought {side.upper()} @ {entry_price}c, "
                        f"selling @ {best_bid}c, loss={slippage}c per contract"
                    )
                else:
                    logger.info(
                        f"Unwinding {side.upper()} at {best_bid}c "
                        f"(no loss vs entry {entry_price}c)"
                    )

        unwind_result = await self.place_order(
            ticker=ticker,
            action="sell",
            side=side,
            count=qty,
            price=unwind_price,
            time_in_force="ioc",
        )

        if unwind_result:
            # Check how many actually filled (IOC can partial fill)
            order = unwind_result.get("order", {})
            filled = (
                order.get("fill_count", qty)  # Kalshi uses "fill_count"
                if not unwind_result.get("dry_run")
                else qty
            )
            remaining = qty - filled

            total_loss = slippage * filled
            fee_loss = calculate_fee(entry_price, qty, False)  # Fee on original buy

            if remaining > 0:
                logger.error(
                    f"PARTIAL UNWIND: only {filled}/{qty} contracts filled! "
                    f"{remaining} {side.upper()} contracts still open on {ticker}. "
                    f"Manual intervention may be needed."
                )
                # Record the partial position we're stuck with
                self.record_fill(ticker, side, "buy", remaining, entry_price)
                return False

            logger.info(
                f"Position fully unwound at {unwind_price}c - "
                f"slippage loss: {total_loss}c, entry fee lost: {fee_loss:.0f}c"
            )
            return True
        else:
            logger.error(
                f"CRITICAL: Failed to unwind {side.upper()} on {ticker}! "
                f"Manual intervention needed."
            )
            # Record the position we're stuck with
            self.record_fill(ticker, side, "buy", qty, entry_price)
            return False


# -----------------------------------------------------------------------------
# Strategy
# -----------------------------------------------------------------------------


class IdenticalMarketsStrategy:
    """Strategy for monitoring two markets using existing WebSocket client."""

    def __init__(
        self,
        cfg: Config,
        ws_client: KalshiWebSocketClient,
        executor: Optional[OrderExecutor] = None,
        on_arb: Optional[Callable[[ArbOpportunity], None]] = None,
    ):
        self.cfg = cfg
        self.ws_client = ws_client
        self.executor = executor
        self.on_arb = on_arb

        self.books: Dict[str, Orderbook] = {
            cfg.ticker_a: Orderbook(ticker=cfg.ticker_a),
            cfg.ticker_b: Orderbook(ticker=cfg.ticker_b),
        }

        self.arb_count = 0
        self.last_arb_time: Optional[float] = None
        self.total_edge_cents = 0.0
        self.start_time = time.time()

        # Lock + flag to prevent concurrent arb attempts
        self._executing = False
        self._execution_lock = asyncio.Lock()

    async def handle_message(self, data: Dict):
        """Handle incoming WebSocket message."""
        msg_type = data.get("type") or data.get("msg")

        if msg_type == "orderbook_snapshot":
            await self._handle_snapshot(data)
        elif msg_type in ("orderbook_delta", "orderbook_update"):
            await self._handle_delta(data)

    async def _handle_snapshot(self, data: Dict):
        """Handle full orderbook snapshot."""
        seq = data.get("seq", 0)

        # Data may be nested inside 'msg' field
        msg = data.get("msg", data)
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if ticker not in self.books:
            return

        yes_levels = msg.get("yes", []) or []
        no_levels = msg.get("no", []) or []

        self.books[ticker].apply_snapshot(yes_levels, no_levels, seq)
        logger.debug(f"Snapshot received for {ticker}: {self.books[ticker]}")

        await self._check_arb()

    async def _handle_delta(self, data: Dict):
        """Handle incremental orderbook update."""
        seq = data.get("seq")

        # Data may be nested inside 'msg' field
        msg = data.get("msg", data)
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if ticker not in self.books:
            return

        side = msg.get("side")
        price = msg.get("price")
        delta = msg.get("delta")

        if side and price is not None and delta is not None:
            if not self.books[ticker].apply_delta(side, price, delta, seq):
                # WS client also handles gaps, but log locally
                logger.warning(
                    f"Sequence gap for {ticker}, waiting for fresh snapshot..."
                )
                return

        await self._check_arb()

    async def _check_arb(self):
        """Check for arbitrage opportunity and notify."""
        book_a = self.books[self.cfg.ticker_a]
        book_b = self.books[self.cfg.ticker_b]

        arb = detect_arb(book_a, book_b, self.cfg)

        if arb:
            self.arb_count += 1
            self.last_arb_time = time.time()
            self.total_edge_cents += arb.net_edge_cents
            logger.debug(f"Arb detected: {arb}")

            if self.on_arb:
                self.on_arb(arb)

    def _format_uptime(self) -> str:
        """Format uptime as human-readable string."""
        elapsed = int(time.time() - self.start_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"

    def print_status(self):
        """Print current status."""
        book_a = self.books[self.cfg.ticker_a]
        book_b = self.books[self.cfg.ticker_b]

        print(f"\n{'='*70}")
        print(f"Uptime: {self._format_uptime()}")
        print(f"Market A: {book_a}")
        print(f"Market B: {book_b}")

        if self.cfg.relationship == RelationshipType.SAME_YES:
            # For same_yes: Buy YES cheap + Buy NO cheap (on other market) should cost < 100c
            a_yes_ask = book_a.best_yes_ask()  # Cost to buy YES on A
            a_no_ask = book_a.best_no_ask()  # Cost to buy NO on A (= 100 - YES_A_bid)
            b_yes_ask = book_b.best_yes_ask()  # Cost to buy YES on B
            b_no_ask = book_b.best_no_ask()  # Cost to buy NO on B (= 100 - YES_B_bid)

            if a_yes_ask and b_no_ask:
                total1 = a_yes_ask + b_no_ask
                profit1 = 100 - total1
                fee1 = taker_fee_cents(a_yes_ask, 1) + taker_fee_cents(b_no_ask, 1)
                print(
                    f"Buy YES_A ({a_yes_ask}c) + Buy NO_B ({b_no_ask}c) = {total1}c -> {profit1}c gross, {profit1 - fee1:.0f}c net"
                )
            if b_yes_ask and a_no_ask:
                total2 = b_yes_ask + a_no_ask
                profit2 = 100 - total2
                fee2 = taker_fee_cents(b_yes_ask, 1) + taker_fee_cents(a_no_ask, 1)
                print(
                    f"Buy YES_B ({b_yes_ask}c) + Buy NO_A ({a_no_ask}c) = {total2}c -> {profit2}c gross, {profit2 - fee2:.0f}c net"
                )
        else:
            a_ask, b_ask = book_a.best_yes_ask(), book_b.best_yes_ask()
            if a_ask and b_ask:
                total = a_ask + b_ask
                gross_edge = 100 - total if total < 100 else 0
                fee = taker_fee_cents(a_ask, 1) + taker_fee_cents(b_ask, 1)
                print(
                    f"Combined YES cost: {a_ask}c + {b_ask}c = {total}c (gross={gross_edge}c, net={gross_edge - fee:.0f}c)"
                )

        print(
            f"\nArbs found: {self.arb_count}, Total edge captured: {self.total_edge_cents:.1f}c"
        )

        if self.executor:
            # Show exposure and positions
            exposure = self.executor.get_exposure()
            max_exposure = self.cfg.max_exposure_cents
            exposure_pct = (exposure / max_exposure * 100) if max_exposure > 0 else 0
            print(
                f"\nExposure: ${exposure/100:.2f} / ${max_exposure/100:.2f} ({exposure_pct:.1f}%)"
            )

            if self.executor.positions:
                print("Positions:")
                for ticker, qty in self.executor.positions.items():
                    if qty != 0:
                        direction = "LONG" if qty > 0 else "SHORT"
                        print(f"  {ticker}: {direction} {abs(qty)} contracts")

            print(
                f"Orders placed: {self.executor.orders_placed}, Filled: {self.executor.orders_filled}"
            )

        print(f"{'='*70}\n")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


async def run_strategy(cfg: Config):
    """Run the identical markets strategy."""
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    if not api_key_id:
        raise ValueError("KALSHI_API_KEY_ID not set in environment")

    private_key_path = PROJECT_ROOT / "kalshi_private_key.pem"
    if not private_key_path.exists():
        raise ValueError(f"Private key not found: {private_key_path}")

    private_key = load_private_key(private_key_path)

    rest_client = AsyncKalshiClient()
    executor = OrderExecutor(rest_client, cfg=cfg, dry_run=cfg.dry_run)

    # Load existing positions only for the two markets we're trading
    tracked_tickers = {cfg.ticker_a, cfg.ticker_b}
    await executor.load_existing_positions(tracked_tickers)

    strategy: Optional[IdenticalMarketsStrategy] = None

    async def on_ws_message(data: Dict):
        if strategy:
            await strategy.handle_message(data)

    ws_client = KalshiWebSocketClient(
        api_key_id=api_key_id,
        private_key=private_key,
        on_message=on_ws_message,
    )

    async def handle_arb(arb: ArbOpportunity):
        async with strategy._execution_lock:
            try:
                if cfg.mode == TradingMode.MONITOR:
                    return
                if not strategy:
                    return
                await executor.execute_arb(arb, cfg, books=strategy.books)
            finally:
                strategy._executing = False

    def on_arb(arb: ArbOpportunity):
        # Check BEFORE creating task - don't queue multiple arbs
        if strategy and strategy._executing:
            logger.debug("Arb skipped: execution already in progress")
            return
        strategy._executing = True
        asyncio.create_task(handle_arb(arb))

    strategy = IdenticalMarketsStrategy(
        cfg, ws_client, executor=executor, on_arb=on_arb
    )

    async def status_printer():
        while ws_client.is_running:
            await asyncio.sleep(5)
            strategy.print_status()

    async def subscribe_after_connect():
        # Wait until WebSocket is actually connected
        while not ws_client.ws:
            await asyncio.sleep(0.2)
        await ws_client.subscribe_to_ticker(cfg.ticker_a)
        await ws_client.subscribe_to_ticker(cfg.ticker_b)
        logger.info(f"Subscribed to {cfg.ticker_a} and {cfg.ticker_b}")

    loop = asyncio.get_event_loop()

    async def cleanup():
        logger.info("Cleaning up...")
        await ws_client.stop()
        await rest_client.close()

    def signal_handler():
        logger.info("Shutdown requested...")
        asyncio.create_task(cleanup())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    logger.info(f"Starting identical markets monitor")
    logger.info(f"  Market A: {cfg.ticker_a}")
    logger.info(f"  Market B: {cfg.ticker_b}")
    logger.info(f"  Relationship: {cfg.relationship.value}")
    logger.info(f"  Mode: {cfg.mode.value}")
    logger.info(f"  Min edge: {cfg.min_edge_cents}c")
    logger.info(f"  Max exposure: ${cfg.max_exposure_cents/100:.2f}")
    logger.info(f"  Max unwind slippage: {cfg.max_unwind_slippage_cents}c")
    logger.info(f"  Dry run: {cfg.dry_run}")

    try:
        await asyncio.gather(
            ws_client.start(), subscribe_after_connect(), status_printer()
        )
    except asyncio.CancelledError:
        pass
    finally:
        await rest_client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Monitor two identical Kalshi markets for arbitrage opportunities."
    )
    parser.add_argument("--ticker-a", "-a", required=True, help="First market ticker")
    parser.add_argument("--ticker-b", "-b", required=True, help="Second market ticker")
    parser.add_argument(
        "--relationship",
        "-r",
        choices=["same_yes", "opposite"],
        default="same_yes",
        help="How the markets relate: same_yes (YES_A=YES_B) or opposite (YES_A=NO_B)",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=["monitor", "taker"],
        default="monitor",
        help="Trading mode: monitor (watch only) or taker (execute trades)",
    )
    parser.add_argument("--qty", "-q", type=int, default=1, help="Contracts per trade")
    parser.add_argument(
        "--min-edge", type=float, default=1.0, help="Minimum net edge in cents"
    )
    parser.add_argument(
        "--max-exposure",
        type=float,
        default=100.0,
        help="Maximum total exposure in dollars (default: $100)",
    )
    parser.add_argument(
        "--max-unwind-slippage",
        type=int,
        default=15,
        help="Max slippage in cents before warning on unwind (default: 15)",
    )
    parser.add_argument(
        "--live", action="store_true", help="Actually place orders (default is dry run)"
    )

    args = parser.parse_args()

    cfg = Config(
        ticker_a=args.ticker_a.upper(),
        ticker_b=args.ticker_b.upper(),
        relationship=RelationshipType(args.relationship),
        mode=TradingMode(args.mode),
        qty=args.qty,
        min_edge_cents=args.min_edge,
        max_exposure_cents=int(args.max_exposure * 100),  # Convert dollars to cents
        max_unwind_slippage_cents=args.max_unwind_slippage,
        dry_run=not args.live,
    )

    asyncio.run(run_strategy(cfg))


if __name__ == "__main__":
    main()
