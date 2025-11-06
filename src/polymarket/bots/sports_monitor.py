#!/usr/bin/env python3
"""Sports betting copy trading monitor with enhanced alerts."""

import argparse
import json
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Deque
from collections import defaultdict, deque
from pathlib import Path

import requests
from dotenv import load_dotenv

# Import utilities
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.polymarket.utils.telegram_notifier import TelegramNotifier, escape_markdown
from src.polymarket.utils.portfolio_tracker import PortfolioTracker
from src.polymarket.utils.state_manager import StateManager
from src.polymarket.utils.log_rotator import LogRotator
from src.polymarket.clients.polymarket_data_client import PolymarketDataClient

load_dotenv()


@dataclass
class SportsBet:
    """Represents a sports bet."""

    transaction_hash: str
    timestamp: int
    side: str  # BUY or SELL
    size: str
    usdc_size: str
    price: str
    market_title: str
    market_slug: str
    outcome: str
    trader_name: str = "Unknown"
    wallet_address: str = ""

    @property
    def formatted_time(self) -> str:
        try:
            dt = datetime.fromtimestamp(self.timestamp)
            return dt.strftime("%I:%M:%S %p")
        except (ValueError, TypeError, OSError):
            return "Invalid time"

    @property
    def formatted_price(self) -> str:
        try:
            return f"{float(self.price) * 100:.1f}%"
        except (ValueError, TypeError):
            return f"{self.price}"

    @property
    def market_url(self) -> str:
        return f"https://polymarket.com/event/{self.market_slug}"

    @property
    def implied_odds(self) -> str:
        """American odds format."""
        try:
            prob = float(self.price)

            if prob <= 0 or prob >= 1:
                return "N/A"

            if prob >= 0.5:
                odds = -100 * prob / (1 - prob)
                return f"{int(odds)}"
            else:
                odds = 100 * (1 - prob) / prob
                return f"+{int(odds)}"
        except (ValueError, TypeError, ZeroDivisionError):
            return "N/A"


class SportsMonitor:
    """Sports betting copy trading monitor for multiple wallets."""

    # API Configuration
    DATA_API_BASE = "https://data-api.polymarket.com"

    # Timing Constants
    STALE_MESSAGE_THRESHOLD_SECONDS = 1800  # 30 minutes before sending new message
    PORTFOLIO_CACHE_TTL_SECONDS = 3600  # 1 hour portfolio cache TTL
    STATE_CLEANUP_INTERVAL_SECONDS = 86400  # 24 hours state cleanup
    STATE_SAVE_DEBOUNCE_SECONDS = 10  # Debounce state saves to max once per 10 seconds
    LOG_ROTATION_CHECK_INTERVAL = 300  # Check for log rotation every 5 minutes

    # Log Rotation Settings
    LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per log file
    LOG_BACKUP_COUNT = 7  # Keep 7 days of backups
    LOG_ROTATION_TIME_SECONDS = 86400  # Rotate every 24 hours

    # Portfolio Thresholds
    PORTFOLIO_CACHE_INVALIDATION_THRESHOLD = (
        0.10  # 10% of portfolio triggers cache invalidation
    )

    # Conviction Thresholds
    CONVICTION_EXTREME_PCT = 10.0
    CONVICTION_HIGH_PCT = 5.0
    CONVICTION_MEDIUM_PCT = 2.0
    CONVICTION_LOW_PCT = 0.5
    CONVICTION_HYSTERESIS_PCT = 0.2  # Deadband to prevent label flapping

    def __init__(
        self,
        wallets: List[Tuple[str, str, Optional[int]]],
        poll_interval: int = 30,
        verbose: bool = False,
        log_file: str = "sports_monitor.log",
        state_file: str = "sports_monitor_state.json",
        telegram_chat_id: Optional[str] = None,
    ):
        # Store wallets with (address, name, min_shares)
        self.wallets = [
            (addr.lower(), name, min_shares) for addr, name, min_shares in wallets
        ]

        # Create min_shares lookup dict for fast access
        self.min_shares_by_wallet: Dict[str, Optional[int]] = {
            addr.lower(): min_shares for addr, _, min_shares in wallets
        }

        self.poll_interval = poll_interval
        self.verbose = verbose
        self.log_file = Path(log_file)
        self.state_file = Path(state_file)

        self.seen_transactions: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=1000)
        )

        # Cumulative share tracking for min_shares thresholds
        self.cumulative_shares: Dict[Tuple[str, str, str, str], float] = {}
        self.threshold_crossed: Dict[Tuple[str, str, str, str], bool] = {}
        self.last_shares_cleanup = time.time()

        # Initialize time tracking (needed by _log() method)
        self.total_alerts = 0
        self.start_time = time.time()
        self.last_state_cleanup = time.time()
        self.last_log_rotation_check = time.time()
        self.last_weekly_backup_cleanup = time.time()

        # Initialize log rotators
        self.main_log_rotator = LogRotator(
            log_file=self.log_file,
            max_bytes=self.LOG_MAX_BYTES,
            backup_count=self.LOG_BACKUP_COUNT,
            rotation_time_seconds=self.LOG_ROTATION_TIME_SECONDS,
            logger=lambda msg: self._log_raw(msg),
        )

        self.trades_log_file = Path("sports_trades.jsonl")
        self.trades_log_rotator = LogRotator(
            log_file=self.trades_log_file,
            max_bytes=self.LOG_MAX_BYTES,
            backup_count=self.LOG_BACKUP_COUNT,
            rotation_time_seconds=self.LOG_ROTATION_TIME_SECONDS,
            logger=lambda msg: self._log_raw(msg),
        )

        # Clean up orphaned log backups on startup (from config changes or crashes)
        self.main_log_rotator.cleanup_old_backups()
        self.trades_log_rotator.cleanup_old_backups()

        # Initialize PolymarketDataClient (replaces manual session creation)
        self.api_client = PolymarketDataClient(
            base_url=self.DATA_API_BASE,
            timeout=10,
            max_retries=5,
            backoff_factor=0.5,
            verbose=verbose,
            logger=self._log,
        )

        # Initialize TelegramNotifier (with DEV_MODE support from environment)
        dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"

        if dev_mode:
            telegram_bot_token = os.getenv("DEV_TELEGRAM_BOT_TOKEN")
            telegram_chat_id = (
                telegram_chat_id
                or os.getenv("DEV_TELEGRAM_CHAT_ID")
                or os.getenv("TELEGRAM_CHAT_ID")
            )
            self._log("[DEV MODE] Using development Telegram bot for testing")
        else:
            telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")

        self.telegram = TelegramNotifier(
            bot_token=telegram_bot_token or "",
            chat_id=telegram_chat_id or "",
            session=self.api_client.session,
            stale_threshold_seconds=self.STALE_MESSAGE_THRESHOLD_SECONDS,
            verbose=verbose,
            logger=self._log,
        )

        mode_indicator = "[DEV MODE] " if dev_mode else ""
        self._log(
            f"{mode_indicator}[OK] Telegram notifications enabled (chat_id: {telegram_chat_id})"
            if self.telegram.enabled
            else f"{mode_indicator}[INFO] Telegram notifications disabled (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"
        )

        # Initialize PortfolioTracker
        self.portfolio = PortfolioTracker(
            data_api_base=self.DATA_API_BASE,
            session=self.api_client.session,
            cache_ttl_seconds=self.PORTFOLIO_CACHE_TTL_SECONDS,
            invalidation_threshold=self.PORTFOLIO_CACHE_INVALIDATION_THRESHOLD,
            verbose=verbose,
            logger=self._log,
        )

        # Initialize StateManager
        self.state_manager = StateManager(
            state_file=self.state_file,
            debounce_seconds=self.STATE_SAVE_DEBOUNCE_SECONDS,
            verbose=verbose,
            logger=self._log,
        )

        # Set conviction thresholds from class constants
        self.portfolio.conviction_extreme_pct = self.CONVICTION_EXTREME_PCT
        self.portfolio.conviction_high_pct = self.CONVICTION_HIGH_PCT
        self.portfolio.conviction_medium_pct = self.CONVICTION_MEDIUM_PCT
        self.portfolio.conviction_low_pct = self.CONVICTION_LOW_PCT
        self.portfolio.conviction_hysteresis_pct = self.CONVICTION_HYSTERESIS_PCT

        # Validate wallets and log configuration
        for wallet, name, min_shares in self.wallets:
            self.api_client.validate_wallet_address(wallet)
            filter_note = (
                f" (filtering: {min_shares:,}+ shares only)" if min_shares else ""
            )
            self._log(f"[OK] Monitoring wallet: {name} ({wallet}){filter_note}")

        self._load_state()

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _log_raw(self, message: str):
        """Write directly to log without timestamp (used by log rotators)."""
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"{message}\n")
        except Exception:
            pass  # Fail silently to avoid recursion

    def _log(self, message: str):
        """Write to log with timestamp and rotation."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}"

        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"{log_entry}\n")
        except Exception as e:
            print(f"[LOG ERROR] {e}")

    def _load_state(self):
        """Load state from file."""
        data = self.state_manager.load()

        if not data:
            return

        # Load telegram messages into notifier
        telegram_data = data.get("telegram_messages", {})
        if telegram_data:
            self.telegram.load_state_from_persistence(telegram_data)

        # Clean old messages (>7 days)
        cutoff_time = datetime.now() - timedelta(days=7)
        removed = self.telegram.cleanup_old_messages(cutoff_time)
        if removed > 0:
            self._log(f"[OK] Cleaned {removed} old entries (>7 days)")
            self._save_state()

        # Count loaded messages
        message_count = len(self.telegram.messages)
        if message_count > 0:
            self._log(f"[OK] Loaded {message_count} telegram message mappings")

        # Load portfolio cache with TTL validation
        portfolio_data = data.get("portfolio_cache", {})
        if portfolio_data:
            self.portfolio.load_cache_from_persistence(portfolio_data)
            cache_stats = self.portfolio.get_cache_stats()
            if cache_stats["cached_wallets"] > 0:
                self._log(
                    f"[OK] Loaded {cache_stats['cached_wallets']} portfolio cache entries"
                )

        # Load cumulative shares tracking (for min_shares thresholds)
        cumulative_data = data.get("cumulative_shares", {})
        if cumulative_data:
            # Convert JSON string keys back to tuples
            self.cumulative_shares = {
                tuple(eval(k)): v for k, v in cumulative_data.items()
            }
            self._log(
                f"[OK] Loaded {len(self.cumulative_shares)} cumulative share positions"
            )

        # Load threshold crossed flags
        threshold_data = data.get("threshold_crossed", {})
        if threshold_data:
            # Convert JSON string keys back to tuples
            self.threshold_crossed = {
                tuple(eval(k)): v for k, v in threshold_data.items()
            }
            self._log(
                f"[OK] Loaded {len(self.threshold_crossed)} threshold crossed flags"
            )

        # Load seen transactions (deduplication)
        seen_tx_data = data.get("seen_transactions", {})
        if seen_tx_data:
            for wallet, tx_list in seen_tx_data.items():
                # Convert list back to deque with maxlen=1000
                self.seen_transactions[wallet] = deque(tx_list, maxlen=1000)
            total_tx = sum(len(txs) for txs in self.seen_transactions.values())
            self._log(f"[OK] Loaded {total_tx} seen transactions across {len(seen_tx_data)} wallets")

    def _save_state(self):
        """Save state to file."""
        # Convert tuple keys to string for JSON serialization
        cumulative_shares_serializable = {str(k): v for k, v in self.cumulative_shares.items()}
        threshold_crossed_serializable = {str(k): v for k, v in self.threshold_crossed.items()}

        # Convert deques to lists for JSON serialization
        seen_tx_serializable = {
            wallet: list(tx_deque) for wallet, tx_deque in self.seen_transactions.items()
        }

        data = {
            "telegram_messages": self.telegram.get_state_for_persistence(),
            "portfolio_cache": self.portfolio.export_cache_for_persistence(),
            "cumulative_shares": cumulative_shares_serializable,
            "threshold_crossed": threshold_crossed_serializable,
            "seen_transactions": seen_tx_serializable,
        }
        self.state_manager.save(data)

    def _mark_state_dirty(self):
        """Mark state dirty for debounced save."""
        self.state_manager.mark_dirty()

        # Save if debounce window has passed
        if self.state_manager.should_save():
            self._save_state()

    def _cleanup_old_state(self):
        """Remove message mappings older than 7 days."""
        cutoff_time = datetime.now() - timedelta(days=7)
        removed = self.telegram.cleanup_old_messages(cutoff_time)

        if removed > 0:
            self._log(f"[CLEANUP] Removed {removed} old state entries (>7 days)")
            self._save_state()

    def _cleanup_cumulative_shares(self):
        """Clean up cumulative shares and threshold flags for positions no longer being tracked."""
        tracked_keys = set(self.telegram.messages.keys())

        # Clean up threshold flags for positions that are no longer tracked (7+ days old, removed from Telegram state)
        orphaned_thresholds = set(self.threshold_crossed.keys()) - tracked_keys
        for key in orphaned_thresholds:
            del self.threshold_crossed[key]

        # Clean up cumulative shares for positions that are no longer tracked
        # Once a position is tracked via Telegram, we keep cumulative_shares briefly in case of restarts,
        # but remove it when the Telegram message is removed (7+ day cleanup)
        orphaned_cumulative = set(self.cumulative_shares.keys()) - tracked_keys
        if orphaned_cumulative:
            for key in orphaned_cumulative:
                del self.cumulative_shares[key]
            if self.verbose:
                self._log(
                    f"[CLEANUP] Removed {len(orphaned_cumulative)} orphaned cumulative share entries"
                )

        if orphaned_thresholds:
            self._log(
                f"[CLEANUP] Removed {len(orphaned_thresholds)} orphaned threshold flags"
            )

    def fetch_recent_trades(self, wallet_address: str, limit: int = 50) -> List[Dict]:
        """Fetch recent trades from Data API."""
        return self.api_client.fetch_recent_trades(wallet_address, limit)

    def parse_trade(
        self, trade_data: Dict, wallet_address: str, trader_name: str
    ) -> Optional[SportsBet]:
        """Parse trade into SportsBet."""
        tx_hash = trade_data.get("transactionHash")

        if not tx_hash:
            return None

        if tx_hash in self.seen_transactions[wallet_address]:
            return None

        bet = SportsBet(
            transaction_hash=tx_hash,
            timestamp=trade_data.get("timestamp", 0),
            side=trade_data.get("side", ""),
            size=str(trade_data.get("size", 0)),
            usdc_size=str(trade_data.get("usdcSize", 0)),
            price=str(trade_data.get("price", 0)),
            market_title=trade_data.get("title", "Unknown"),
            market_slug=trade_data.get("slug", ""),
            outcome=trade_data.get("outcome", "Unknown"),
            trader_name=trader_name,
            wallet_address=wallet_address,
        )

        self.seen_transactions[wallet_address].append(tx_hash)
        return bet

    def _should_filter_bet(self, bet: SportsBet) -> bool:
        """Check if bet should be filtered by min_shares threshold with cumulative tracking."""
        min_shares = self.min_shares_by_wallet.get(bet.wallet_address.lower())

        if min_shares is None:
            return False

        try:
            new_shares = float(bet.size)
        except (ValueError, TypeError):
            return False

        position_key = (
            bet.wallet_address.lower(),
            bet.market_slug.lower(),
            bet.outcome.upper(),
            bet.side.upper(),
        )

        # Position already being tracked (threshold previously crossed)
        if self.telegram.has_tracked_message(position_key):
            return False

        # Update cumulative shares
        current_total = self.cumulative_shares.get(position_key, 0.0)
        new_total = current_total + new_shares
        self.cumulative_shares[position_key] = new_total

        already_crossed = self.threshold_crossed.get(position_key, False)

        if new_total >= min_shares:
            if not already_crossed:
                self.threshold_crossed[position_key] = True
                self._log(
                    f"[THRESHOLD CROSSED] {bet.trader_name}: {new_total:,.0f} shares >= {min_shares:,} threshold "
                    f"(cumulative from {current_total:,.0f})"
                )
            return False

        if self.verbose:
            self._log(
                f"[FILTERED] {bet.trader_name}: {new_total:,.0f} shares < {min_shares:,} threshold (cumulative)"
            )
        return True

    def alert_bet(self, bet: SportsBet):
        """Send bet alert."""
        # Check if bet should be filtered
        if self._should_filter_bet(bet):
            return

        self.total_alerts += 1

        is_fish = "Fish" in bet.trader_name
        fade_marker = "[FADE] " if is_fish else ""
        alert = (
            f"{fade_marker}NEW BET FROM {bet.trader_name.upper()} | "
            f"{bet.side} {bet.outcome} | ${bet.usdc_size} @ {bet.formatted_price} | "
            f"{bet.market_title}"
        )
        self._log(alert)

        self._send_or_update_telegram(bet)
        self._log_bet(bet)

    def _handle_stale_message(
        self,
        bet: SportsBet,
        message_key: Tuple[str, str, str, str],
        usdc_amount: float,
        existing_usdc: float,
        first_time: datetime,
        new_update_count: int,
        portfolio_value: Optional[float],
    ):
        """Handle adding to stale position (>30 min old)."""
        new_total_usdc = existing_usdc + usdc_amount
        conviction_label = self.portfolio.get_conviction_label(
            new_total_usdc, portfolio_value
        )

        # Format message with total stake and context about previous position
        message = self._format_telegram_message(
            bet,
            new_total_usdc,
            datetime.fromtimestamp(bet.timestamp),
            0,
            is_update=False,
            portfolio_value=portfolio_value,
            addition_context={
                "previous_total": existing_usdc,
                "new_total": new_total_usdc,
                "first_time": first_time,
            },
        )

        # Send new message and track with total stake
        msg_id = self.telegram.send_and_track(
            message_key,
            message,
            new_total_usdc,
            datetime.fromtimestamp(bet.timestamp),
            conviction_label,
        )

        if msg_id:
            self._mark_state_dirty()

            if self.verbose:
                self._log(
                    f"[TELEGRAM] Sent new message for stale position (ID: {msg_id}, total: ${new_total_usdc:.2f})"
                )

    def _handle_existing_message(
        self,
        bet: SportsBet,
        message_key: Tuple[str, str, str, str],
        usdc_amount: float,
        portfolio_value: Optional[float],
    ):
        """Update existing tracked position."""
        state = self.telegram.get_message_state(message_key)
        if not state:
            # Message not tracked, treat as new
            self._handle_new_message(bet, message_key, usdc_amount, portfolio_value)
            return

        new_total_usdc = state.total_usdc + usdc_amount
        new_update_count = state.update_count + 1

        # Check if message is stale (too old to update)
        current_time = datetime.fromtimestamp(bet.timestamp)
        is_stale = self.telegram.is_message_stale(state, current_time)

        if is_stale:
            self._handle_stale_message(
                bet,
                message_key,
                usdc_amount,
                state.total_usdc,
                state.first_time,
                new_update_count,
                portfolio_value,
            )
            return

        # Not stale - proceed with normal update
        new_conviction = self.portfolio.get_conviction_label(
            new_total_usdc, portfolio_value, state.conviction_label
        )

        message = self._format_telegram_message(
            bet,
            new_total_usdc,
            state.first_time,
            new_update_count,
            is_update=True,
            portfolio_value=portfolio_value,
        )

        # Try to update existing message
        success = self.telegram.update_and_track(
            message_key,
            state.message_id,
            message,
            new_total_usdc,
            state.first_time,
            new_update_count,
            new_conviction,
            state.conviction_label,
        )

        if success:
            self._mark_state_dirty()
        else:
            # Update failed (message deleted), resend as new
            self.telegram.untrack_message(message_key)
            self._handle_new_message(bet, message_key, usdc_amount, portfolio_value)

    def _handle_new_message(
        self,
        bet: SportsBet,
        message_key: Tuple[str, str, str, str],
        usdc_amount: float,
        portfolio_value: Optional[float],
    ):
        """Track new position."""
        conviction_label = self.portfolio.get_conviction_label(
            usdc_amount, portfolio_value
        )

        message = self._format_telegram_message(
            bet,
            usdc_amount,
            datetime.fromtimestamp(bet.timestamp),
            0,
            is_update=False,
            portfolio_value=portfolio_value,
        )

        msg_id = self.telegram.send_and_track(
            message_key,
            message,
            usdc_amount,
            datetime.fromtimestamp(bet.timestamp),
            conviction_label,
        )

        if msg_id:
            self._mark_state_dirty()

    def _send_or_update_telegram(self, bet: SportsBet):
        """Send or update Telegram message with conviction tracking."""
        if not self.telegram.enabled:
            return

        try:
            usdc_amount = float(bet.usdc_size)
            message_key = self.telegram.create_message_key(
                bet.wallet_address, bet.market_slug, bet.outcome, bet.side
            )

            # Check if we should invalidate cache early (large position change)
            is_existing = self.telegram.has_tracked_message(message_key)
            existing_usdc = 0

            if is_existing:
                state = self.telegram.get_message_state(message_key)
                if state:
                    existing_usdc = state.total_usdc

            # If this bet is >10% of cached portfolio, invalidate cache
            # (likely indicates deposit/withdrawal)
            if is_existing and existing_usdc > 0:
                should_invalidate = self.portfolio.should_invalidate_for_bet(
                    bet.wallet_address, usdc_amount
                )
                if should_invalidate:
                    self.portfolio.invalidate_cache(bet.wallet_address)

            # Fetch portfolio value (with 1-hour cache or early invalidation)
            portfolio_value = self.portfolio.get_portfolio_value(bet.wallet_address)

            # If position just crossed threshold, use cumulative total for first message
            if not is_existing and message_key in self.cumulative_shares:
                # Calculate cumulative USDC by converting shares to USDC
                # Use ratio from current bet: usdc_amount / shares = price per share
                try:
                    shares_current = float(bet.size)
                    if shares_current > 0:
                        price_per_share = usdc_amount / shares_current
                        cumulative_shares_total = self.cumulative_shares[message_key]
                        cumulative_usdc = cumulative_shares_total * price_per_share

                        if self.verbose:
                            self._log(
                                f"[CUMULATIVE] Using cumulative total: {cumulative_shares_total:,.0f} shares = ${cumulative_usdc:.2f} "
                                f"(current trade: {shares_current:,.0f} shares = ${usdc_amount:.2f})"
                            )

                        usdc_amount = cumulative_usdc
                except (ValueError, TypeError, ZeroDivisionError):
                    pass  # Fall back to current bet amount

            # Route to appropriate handler
            if is_existing:
                self._handle_existing_message(
                    bet, message_key, usdc_amount, portfolio_value
                )
            else:
                self._handle_new_message(bet, message_key, usdc_amount, portfolio_value)

        except requests.RequestException as e:
            self._log(f"[TELEGRAM ERROR] Failed: {e}")
        except Exception as e:
            self._log(f"[TELEGRAM EXCEPTION] {e}")

    def _format_telegram_message(
        self,
        bet: SportsBet,
        total_usdc: float,
        first_time: datetime,
        update_count: int,
        is_update: bool,
        portfolio_value: Optional[float] = None,
        addition_context: Optional[Dict] = None,
    ) -> str:
        """Format Telegram message with conviction."""
        # Handle different header types
        if addition_context:
            # Stale position addition
            time_since = (
                datetime.fromtimestamp(bet.timestamp) - addition_context["first_time"]
            )
            hours = time_since.total_seconds() / 3600
            prev_total = addition_context["previous_total"]
            update_header = f"*[ADDING TO POSITION]*\n*Original bet:* {hours:.1f}h ago (${prev_total:.2f})\n"
        elif is_update:
            update_header = f"*[POSITION UPDATED x{update_count}]*\n"
        else:
            update_header = ""

        trader_name_safe = escape_markdown(bet.trader_name)
        outcome_safe = escape_markdown(bet.outcome)
        market_title_safe = escape_markdown(bet.market_title)

        is_fish = "Fish" in bet.trader_name
        fade_warning = (
            "\n\n*[!] FADE THIS TRADE - INVERSE SIGNAL [!]*" if is_fish else ""
        )

        # Conviction indicator
        conviction_line = ""
        if portfolio_value and portfolio_value > 0:
            conviction_label, conviction_pct = self.portfolio.calculate_conviction(
                total_usdc, portfolio_value
            )

            # ASCII conviction markers
            conviction_marker = {
                "EXTREME": "[!!!]",
                "HIGH": "[!!]",
                "MEDIUM": "[!]",
                "LOW": "[-]",
                "MINIMAL": "[ ]",
            }.get(conviction_label, "")

            # Note: "positions" not "portfolio" since we exclude cash balance
            conviction_line = f"\n*Conviction:* {conviction_marker} {conviction_label} ({conviction_pct:.1f}% of positions)"

        opposite_action = ""
        if is_fish:
            opposite_side = "SELL" if bet.side == "BUY" else "BUY"
            opposite_action = f"\n*Recommended Action:* {opposite_side} {outcome_safe}"

        message = f"""{update_header}*{bet.side} {outcome_safe}*
*Trader:* {trader_name_safe}{fade_warning}

*Total Stake:* ${total_usdc:.2f} USDC{conviction_line}
*Price:* {bet.formatted_price} (Implied: {bet.implied_odds})
*Market:* {market_title_safe}{opposite_action}

*First Trade:* {first_time.strftime('%I:%M:%S %p')}
*Latest:* {bet.formatted_time}

[Open Market]({bet.market_url})"""

        return message

    def _log_bet(self, bet: SportsBet):
        """Log bet to JSONL with rotation."""
        try:
            with open(self.trades_log_file, "a", encoding="utf-8") as f:
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "bet": asdict(bet),
                    "formatted_time": bet.formatted_time,
                    "formatted_price": bet.formatted_price,
                    "implied_odds": bet.implied_odds,
                }
                f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            if self.verbose:
                self._log(f"[DEBUG] Bet logging failed: {e}")

    def _check_and_rotate_logs(self):
        """Check and rotate logs if needed."""
        current_time = time.time()

        if (
            current_time - self.last_log_rotation_check
            > self.LOG_ROTATION_CHECK_INTERVAL
        ):
            self.main_log_rotator.check_and_rotate()
            self.trades_log_rotator.check_and_rotate()

            # Weekly cleanup of orphaned backups
            time_since_weekly_cleanup = current_time - self.last_weekly_backup_cleanup
            if time_since_weekly_cleanup > 604800:
                self.main_log_rotator.cleanup_old_backups()
                self.trades_log_rotator.cleanup_old_backups()
                self.last_weekly_backup_cleanup = current_time

            self.last_log_rotation_check = current_time

    def _send_startup_message(self):
        """Send startup notification."""
        trader_list = [(name, min_shares) for _, name, min_shares in self.wallets]
        self.telegram.send_startup_message(
            num_wallets=len(self.wallets),
            poll_interval=self.poll_interval,
            trader_list=trader_list,
        )

    def _send_shutdown_message(self):
        """Send shutdown notification."""
        uptime_seconds = int(time.time() - self.start_time)
        self.telegram.send_shutdown_message(
            uptime_seconds=uptime_seconds, total_alerts=self.total_alerts
        )

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals."""
        self._log("\n[SHUTDOWN] Received signal, stopping bot...")
        self._send_shutdown_message()

        # Final state save on shutdown
        if self.state_manager.is_dirty():
            self._log("[SHUTDOWN] Saving final state...")
            try:
                self.state_manager.force_save(
                    {
                        "telegram_messages": self.telegram.get_state_for_persistence(),
                        "portfolio_cache": self.portfolio.export_cache_for_persistence(),
                    }
                )
            except Exception as e:
                self._log(f"[SHUTDOWN ERROR] Failed to save state: {e}")

        self._cleanup_resources()
        self._log("[SHUTDOWN] Cleanup complete, exiting")
        sys.exit(0)

    def _cleanup_resources(self):
        """Clean up resources."""
        try:
            if hasattr(self, "api_client") and self.api_client:
                self.api_client.close()
        except Exception as e:
            self._log(f"[WARNING] Error closing API client: {e}")

    def run(self):
        """Run monitoring loop."""
        self._log(f"Monitoring {len(self.wallets)} trader(s)")
        self._log(f"Polling every {self.poll_interval} seconds")
        self._log(f"Log file: {self.log_file}")
        self._log("=" * 64)
        self._log("Loading recent bets to establish baseline...")

        for wallet, name, _ in self.wallets:
            try:
                initial_trades = self.fetch_recent_trades(wallet, limit=100)
                for trade in initial_trades:
                    tx_hash = trade.get("transactionHash")
                    if tx_hash:
                        self.seen_transactions[wallet].append(tx_hash)
                self._log(
                    f"  {name}: Loaded {len(self.seen_transactions[wallet])} recent bets"
                )
            except Exception as e:
                self._log(f"  WARNING: {name}: Could not load baseline: {e}")

        self._log("=" * 64)
        self._log("MONITORING ACTIVE - Waiting for new bets...")
        print(
            f"\nMonitoring {len(self.wallets)} traders. Check {self.log_file} for output.\n"
        )

        self._send_startup_message()

        while True:
            try:
                all_new_bets = []

                def fetch_wallet_trades(wallet_info):
                    wallet, name, _ = wallet_info
                    wallet_bets = []
                    try:
                        trades = self.fetch_recent_trades(wallet, limit=30)
                        for trade_data in trades:
                            bet = self.parse_trade(trade_data, wallet, name)
                            if bet:
                                wallet_bets.append(bet)
                    except Exception as e:
                        self._log(f"[ERROR] {name}: {e}")
                        if self.verbose:
                            self._log(traceback.format_exc())
                    return wallet_bets

                # Bet processing must remain single-threaded for thread safety
                with ThreadPoolExecutor(
                    max_workers=min(8, len(self.wallets))
                ) as executor:
                    results = executor.map(fetch_wallet_trades, self.wallets)
                    for wallet_bets in results:
                        all_new_bets.extend(wallet_bets)

                for bet in sorted(all_new_bets, key=lambda b: b.timestamp):
                    self.alert_bet(bet)

                current_time = time.time()

                # Check and rotate logs
                self._check_and_rotate_logs()

                # Cleanup old state
                if (
                    current_time - self.last_state_cleanup
                    > self.STATE_CLEANUP_INTERVAL_SECONDS
                ):
                    self._cleanup_old_state()
                    self.last_state_cleanup = current_time

                # Cleanup cumulative shares
                if (
                    current_time - self.last_shares_cleanup
                    > self.STATE_CLEANUP_INTERVAL_SECONDS
                ):
                    self._cleanup_cumulative_shares()
                    self.last_shares_cleanup = current_time

                # Final state save at end of poll if dirty
                if self.state_manager.is_dirty():
                    self._save_state()

                if self.verbose:
                    uptime = int(time.time() - self.start_time)
                    self._log(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Poll complete | "
                        f"Alerts: {self.total_alerts} | Uptime: {uptime}s"
                    )

                time.sleep(self.poll_interval)

            except Exception as e:
                self._log(f"[ERROR] {e}")
                if self.verbose:
                    self._log(traceback.format_exc())
                time.sleep(self.poll_interval)

    def _print_summary(self):
        """Print summary."""
        uptime = int(time.time() - self.start_time)
        summary = f"""
{'=' * 64}
SESSION SUMMARY
{'=' * 64}
Total Bets Alerted:  {self.total_alerts}
Session Duration:    {uptime // 60}m {uptime % 60}s
Traders Monitored:   {len(self.wallets)}
{'=' * 64}
"""
        self._log(summary)
        print(summary)


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Monitor multiple sports bettors for copy trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single wallet (legacy mode)
  python -m src.polymarket.bots.sports_monitor --wallet 0x123... --name "Trader1"

  # Multiple wallets from config file
  python -m src.polymarket.bots.sports_monitor --config config/trader_list.json

  # Config file format:
  {
    "wallets": [
      {"address": "0x123...", "name": "Trader1"},
      {"address": "0x456...", "name": "Trader2", "min_shares": 50000}
    ]
  }

  # Optional min_shares field: only alert when trader takes positions >= min_shares
""",
    )

    parser.add_argument(
        "--config",
        "-c",
        help="JSON config file with multiple wallets",
    )

    parser.add_argument(
        "--wallet",
        "-w",
        help="Single wallet address to monitor (legacy mode)",
    )

    parser.add_argument(
        "--name",
        "-n",
        default="Friend",
        help="Trader name for single wallet (default: Friend)",
    )

    parser.add_argument(
        "--poll-interval",
        "-p",
        type=int,
        default=30,
        help="Seconds between polls (default: 30)",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "--log-file",
        "-l",
        default="sports_monitor.log",
        help="Log file path (default: sports_monitor.log)",
    )

    parser.add_argument(
        "--telegram-chat-id",
        help="Override Telegram chat ID from .env (for personal vs group routing)",
    )

    parser.add_argument(
        "--state-file",
        default="sports_monitor_state.json",
        help="State file path (default: sports_monitor_state.json)",
    )

    args = parser.parse_args()

    wallets = []

    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                config = json.load(f)
                for wallet_config in config.get("wallets", []):
                    address = wallet_config.get("address", "").strip()
                    name = wallet_config.get("name", "Unknown").strip()
                    min_shares = wallet_config.get("min_shares")

                    # Validate min_shares
                    if min_shares is not None:
                        if not isinstance(min_shares, int):
                            raise ValueError(f"min_shares must be integer for {name}, got {type(min_shares).__name__}")
                        if min_shares < 0:
                            raise ValueError(f"min_shares must be non-negative for {name}, got {min_shares}")

                    if address:
                        wallets.append((address, name, min_shares))
            print(f"Loaded {len(wallets)} wallet(s) from {args.config}")
        except Exception as e:
            print(f"ERROR: Failed to load config file: {e}")
            return

    elif args.wallet:
        wallets = [(args.wallet, args.name, None)]

    else:
        parser.error("Must provide either --config or --wallet")

    if not wallets:
        print("ERROR: No wallets to monitor")
        return

    monitor = SportsMonitor(
        wallets=wallets,
        poll_interval=args.poll_interval,
        verbose=args.verbose,
        log_file=args.log_file,
        state_file=args.state_file,
        telegram_chat_id=args.telegram_chat_id,
    )

    monitor.run()


if __name__ == "__main__":
    main()
