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
from src.polymarket.utils.telegram_notifier import TelegramNotifier, escape_markdown, UpdateStatus
from src.polymarket.utils.portfolio_tracker import PortfolioTracker
from src.polymarket.utils.state_manager import StateManager
from src.polymarket.utils.log_rotator import LogRotator
from src.polymarket.clients.polymarket_data_client import PolymarketDataClient
from src.polymarket.utils.position_tracker_state import PositionTracker, NetPosition, PositionStatus
from src.polymarket.utils.message_router import MessageRouter, MessageAction
from src.polymarket.utils.message_formatter import MessageFormatter, BetInfo, ConvictionInfo

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
        wallets: List[Tuple[str, str, Optional[int], Optional[str]]],
        poll_interval: int = 30,
        verbose: bool = False,
        log_file: str = "data/sports_monitor.log",
        state_file: str = "data/sports_monitor_state.json",
        telegram_chat_id: Optional[str] = None,
    ):
        # Store wallets with (address, name, min_shares, profile_url)
        self.wallets = [
            (addr.lower(), name, min_shares, profile_url)
            for addr, name, min_shares, profile_url in wallets
        ]

        # Create min_shares lookup dict
        self.min_shares_by_wallet: Dict[str, Optional[int]] = {
            addr.lower(): min_shares for addr, _, min_shares, _ in wallets
        }

        # Create profile_url lookup dict
        self.profile_url_by_wallet: Dict[str, Optional[str]] = {
            addr.lower(): profile_url for addr, _, _, profile_url in wallets
        }

        self.poll_interval = poll_interval
        self.verbose = verbose
        self.log_file = Path(log_file)
        self.state_file = Path(state_file)

        Path("data").mkdir(parents=True, exist_ok=True)

        self.seen_transactions: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=1000)
        )

        # Initialize PositionTracker utility
        self.position_tracker = PositionTracker(verbose=verbose, logger=self._log)
        self.last_shares_cleanup = time.time()
        self._poll_count = 0

        # Initialize MessageRouter utility
        self.message_router = MessageRouter(
            min_update_pct=5.0,  # 5% change required for update
            min_update_abs=100.0,  # OR $100 absolute change
            stale_threshold_seconds=self.STALE_MESSAGE_THRESHOLD_SECONDS,
            verbose=verbose,
            logger=self._log,
        )

        # Initialize MessageFormatter utility
        self.message_formatter = MessageFormatter(verbose=verbose, logger=self._log)

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

        self.trades_log_file = Path("data/sports_trades.jsonl")
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
        for wallet, name, min_shares, _ in self.wallets:
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
            self._log(f"[CLEANUP] Removed {removed} old entries (>7 days)")
            self._save_state()

        message_count = len(self.telegram.messages)
        if message_count > 0 and self.verbose:
            self._log(f"[OK] Loaded {message_count} telegram message mappings")

        # Load portfolio cache
        portfolio_data = data.get("portfolio_cache", {})
        if portfolio_data:
            self.portfolio.load_cache_from_persistence(portfolio_data)
            cache_stats = self.portfolio.get_cache_stats()
            if cache_stats["cached_wallets"] > 0 and self.verbose:
                self._log(
                    f"[OK] Loaded {cache_stats['cached_wallets']} portfolio cache entries"
                )

        # Load position tracker state
        self.position_tracker.load_from_persistence(data)

        # Migrate legacy cumulative_shares data
        legacy_cumulative = data.get("cumulative_shares", {})
        if legacy_cumulative and not data.get("net_positions"):
            self.position_tracker.migrate_legacy_data(legacy_cumulative)

        # Load seen transactions
        seen_tx_data = data.get("seen_transactions", {})
        if seen_tx_data:
            for wallet, tx_list in seen_tx_data.items():
                self.seen_transactions[wallet] = deque(tx_list, maxlen=1000)
            if self.verbose:
                total_tx = sum(len(txs) for txs in self.seen_transactions.values())
                self._log(f"[OK] Loaded {total_tx} seen transactions across {len(seen_tx_data)} wallets")

    def _save_state(self):
        """Save state to file."""
        seen_tx_serializable = {
            wallet: list(tx_deque) for wallet, tx_deque in self.seen_transactions.items()
        }

        position_state = self.position_tracker.export_for_persistence()

        data = {
            "telegram_messages": self.telegram.get_state_for_persistence(),
            "portfolio_cache": self.portfolio.export_cache_for_persistence(),
            **position_state,  # Includes net_positions and threshold_crossed
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

    def _cleanup_net_positions(self):
        """Clean up net positions and threshold flags for positions no longer being tracked."""
        tracked_keys = set(self.telegram.messages.keys())
        self.position_tracker.cleanup_orphaned_positions(tracked_keys)

    def _reconstruct_positions_at_startup(self, trade_limit: int = 1000):
        """
        Reconstruct net positions from API at startup.

        Args:
            trade_limit: Unused, kept for backwards compatibility
        """
        self._log("=" * 64)
        self._log("Loading current positions from API...")

        total_positions = 0

        for wallet, name, _, _ in self.wallets:
            try:
                reconstructed = self.api_client.reconstruct_positions_from_api(wallet)

                if not reconstructed:
                    self._log(f"  {name}: No active positions found")
                    continue

                for (market_slug, outcome), position_data in reconstructed.items():
                    shares = position_data["shares"]
                    usdc = position_data["usdc"]

                    position_key = self.position_tracker.create_position_key(
                        wallet, market_slug, outcome
                    )

                    if position_key not in self.position_tracker.positions:
                        self.position_tracker.positions[position_key] = NetPosition(
                            shares=shares, usdc=usdc
                        )
                        total_positions += 1

                        if self.verbose:
                            self._log(
                                f"    {market_slug} {outcome}: {shares:.0f} shares"
                            )

                if reconstructed:
                    self._log(
                        f"  {name}: Loaded {len(reconstructed)} position(s)"
                    )

            except Exception as e:
                self._log(f"  WARNING: {name}: Could not load positions: {e}")
                if self.verbose:
                    self._log(traceback.format_exc())

        if total_positions > 0:
            self._log(f"Successfully loaded {total_positions} total position(s)")
            self._save_state()  # Save loaded positions
        else:
            self._log("No active positions found")

        self._log("=" * 64)

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
            timestamp=trade_data.get("timestamp") or 0,
            side=trade_data.get("side") or "",
            size=str(trade_data.get("size") or 0),
            usdc_size=str(trade_data.get("usdcSize") or 0),
            price=str(trade_data.get("price") or 0),
            market_title=trade_data.get("title") or "Unknown",
            market_slug=trade_data.get("slug") or "",
            outcome=trade_data.get("outcome") or "Unknown",
            trader_name=trader_name,
            wallet_address=wallet_address,
        )

        self.seen_transactions[wallet_address].append(tx_hash)
        return bet

    def _update_and_check_position(self, bet: SportsBet) -> Tuple[Optional[NetPosition], bool, str]:
        """
        Update position and check if should alert.

        Returns:
            (net_position, should_alert, reason)
        """
        try:
            trade_shares = float(bet.size)
            trade_usdc = float(bet.usdc_size)
        except (ValueError, TypeError):
            return (None, False, "invalid_trade_data")

        net_pos = self.position_tracker.update_position(
            wallet=bet.wallet_address,
            market_slug=bet.market_slug,
            outcome=bet.outcome,
            side=bet.side,
            shares=trade_shares,
            usdc=trade_usdc,
        )

        min_shares = self.min_shares_by_wallet.get(bet.wallet_address.lower())
        is_tracked = self.telegram.has_tracked_message(
            self.position_tracker.create_position_key(
                bet.wallet_address, bet.market_slug, bet.outcome
            )
        )
        has_crossed = self.position_tracker.has_crossed_threshold(
            bet.wallet_address, bet.market_slug, bet.outcome
        )

        should_alert, reason = self.message_router.should_alert_position(
            net_pos=net_pos,
            min_shares=min_shares,
            is_tracked=is_tracked,
            has_crossed_threshold=has_crossed,
        )

        if reason == "threshold_crossed":
            self.position_tracker.mark_threshold_crossed(
                bet.wallet_address, bet.market_slug, bet.outcome
            )
            self._log(
                f"[THRESHOLD CROSSED] {bet.trader_name}: {abs(net_pos.shares):,.0f} net shares >= {min_shares:,} threshold"
            )

        if not should_alert and self.verbose:
            self._log(f"[FILTERED] {bet.trader_name}: {reason}")

        return (net_pos, should_alert, reason)

    def alert_bet(self, bet: SportsBet):
        """Send bet alert."""
        net_pos, should_alert, reason = self._update_and_check_position(bet)

        if not should_alert or not net_pos:
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

        # Handle position close vs regular alert
        if reason == "position_closed":
            self._handle_position_close(bet, net_pos)
        else:
            self._handle_telegram_notification(bet, net_pos)

        self._log_bet(bet)

    def _handle_position_close(self, bet: SportsBet, net_pos: NetPosition):
        """Send notification when trader closes position."""
        message_key = self.position_tracker.create_position_key(
            bet.wallet_address, bet.market_slug, bet.outcome
        )
        state = self.telegram.get_message_state(message_key)

        if not state:
            if self.verbose:
                self._log(f"[POSITION CLOSED] {bet.trader_name} (untracked)")
            return

        profile_url = self.profile_url_by_wallet.get(bet.wallet_address.lower())
        bet_info = BetInfo(
            trader_name=bet.trader_name,
            outcome=bet.outcome,
            market_title=bet.market_title,
            market_url=bet.market_url,
            formatted_price=bet.formatted_price,
            implied_odds=bet.implied_odds,
            formatted_time=bet.formatted_time,
            side=bet.side,
            trader_profile_url=profile_url,
        )

        message = self.message_formatter.format_position_close(
            bet=bet_info,
            net_pos=net_pos,
            original_stake=state.total_usdc,
        )

        max_retries = 3
        for attempt in range(max_retries):
            status = self.telegram.update_message(state.message_id, message)

            if status == UpdateStatus.SUCCESS:
                pnl = net_pos.get_pnl()
                pnl_display = f"+${abs(pnl):.2f}" if pnl > 0 else f"-${abs(pnl):.2f}"
                self._log(
                    f"[CLOSE] {bet.trader_name}: {bet.outcome} - P&L: {pnl_display}"
                )
                self.telegram.untrack_message(message_key)
                self.position_tracker.reset_threshold(
                    bet.wallet_address, bet.market_slug, bet.outcome
                )
                self._mark_state_dirty()
                return

            elif status == UpdateStatus.MESSAGE_DELETED:
                self._log(f"[CLOSE] Message deleted, untracking position")
                self.telegram.untrack_message(message_key)
                self.position_tracker.reset_threshold(
                    bet.wallet_address, bet.market_slug, bet.outcome
                )
                self._mark_state_dirty()
                return

            elif status == UpdateStatus.NETWORK_ERROR:
                if attempt < max_retries - 1:
                    retry_delay = 1.0 * (attempt + 1)
                    if self.verbose:
                        self._log(f"[RETRY] Network error, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                else:
                    self._log(f"[WARNING] Failed to update close message after {max_retries} attempts, untracking")
                    self.telegram.untrack_message(message_key)
                    return

            else:
                self._log(f"[WARNING] Unknown error updating close message, untracking")
                self.telegram.untrack_message(message_key)
                return

    def _handle_telegram_notification(self, bet: SportsBet, net_pos: NetPosition):
        """Unified message handling with MessageRouter and MessageFormatter."""
        if not self.telegram.enabled:
            return

        try:
            message_key = self.position_tracker.create_position_key(
                bet.wallet_address, bet.market_slug, bet.outcome
            )

            state = self.telegram.get_message_state(message_key)
            state_dict = None
            if state:
                state_dict = {
                    "total_usdc": state.total_usdc,
                    "first_time": state.first_time,
                    "update_count": state.update_count,
                    "conviction_label": state.conviction_label,
                    "message_id": state.message_id,
                }

            decision = self.message_router.decide_message_action(
                net_pos=net_pos,
                message_state=state_dict,
                current_timestamp=bet.timestamp,
            )

            if decision.action == MessageAction.SKIP:
                if self.verbose:
                    self._log(f"[SKIP] {decision.reason}")
                return

            portfolio_value = None
            conviction = None
            if not decision.skip_portfolio_fetch:
                if state and state.total_usdc > 0:
                    should_invalidate = self.portfolio.should_invalidate_for_bet(
                        bet.wallet_address, float(bet.usdc_size)
                    )
                    if should_invalidate:
                        self.portfolio.invalidate_cache(bet.wallet_address)

                portfolio_value = self.portfolio.get_portfolio_value(bet.wallet_address)

                display_amount = net_pos.get_display_amount()
                if portfolio_value and portfolio_value > 0:
                    conviction_label, conviction_pct = self.portfolio.calculate_conviction(
                        display_amount, portfolio_value
                    )
                    conviction = ConvictionInfo(
                        label=conviction_label,
                        percentage=conviction_pct,
                        marker=self.message_formatter.CONVICTION_MARKERS.get(conviction_label, ""),
                    )

            profile_url = self.profile_url_by_wallet.get(bet.wallet_address.lower())
            bet_info = BetInfo(
                trader_name=bet.trader_name,
                outcome=bet.outcome,
                market_title=bet.market_title,
                market_url=bet.market_url,
                formatted_price=bet.formatted_price,
                implied_odds=bet.implied_odds,
                formatted_time=bet.formatted_time,
                side=bet.side,
                trader_profile_url=profile_url,
            )

            if decision.action == MessageAction.NEW:
                self._send_new_message(bet_info, net_pos, message_key, portfolio_value, conviction)
            elif decision.action == MessageAction.UPDATE:
                self._update_message(bet_info, net_pos, message_key, state, portfolio_value, conviction)
            elif decision.action == MessageAction.STALE_ADDITION:
                self._send_stale_addition(bet_info, net_pos, message_key, state, portfolio_value, conviction)

        except requests.RequestException as e:
            self._log(f"[TELEGRAM ERROR] Failed: {e}")
        except Exception as e:
            self._log(f"[TELEGRAM EXCEPTION] {e}")
            if self.verbose:
                import traceback
                self._log(traceback.format_exc())

    def _send_new_message(
        self,
        bet_info: BetInfo,
        net_pos: NetPosition,
        message_key: Tuple[str, str, str],
        portfolio_value: Optional[float],
        conviction: Optional[ConvictionInfo],
    ):
        """Send new Telegram message for position."""
        message = self.message_formatter.format_new_position(
            bet=bet_info,
            net_pos=net_pos,
            portfolio_value=portfolio_value,
            conviction=conviction,
        )

        display_amount = net_pos.get_display_amount()
        conviction_label = conviction.label if conviction else "MINIMAL"

        msg_id = self.telegram.send_and_track(
            message_key,
            message,
            display_amount,
            datetime.now(),
            conviction_label,
        )

        if msg_id:
            self._mark_state_dirty()
            if self.verbose:
                self._log(f"[TELEGRAM] Sent new message (ID: {msg_id}, stake: ${display_amount:.2f})")

    def _update_message(
        self,
        bet_info: BetInfo,
        net_pos: NetPosition,
        message_key: Tuple[str, str, str],
        state: any,
        portfolio_value: Optional[float],
        conviction: Optional[ConvictionInfo],
    ):
        """Update existing Telegram message with retry logic."""
        message = self.message_formatter.format_position_update(
            bet=bet_info,
            net_pos=net_pos,
            first_time=state.first_time,
            update_count=state.update_count + 1,
            portfolio_value=portfolio_value,
            conviction=conviction,
        )

        display_amount = net_pos.get_display_amount()
        new_conviction = conviction.label if conviction else "MINIMAL"

        # Retry logic for network errors
        max_retries = 3
        for attempt in range(max_retries):
            status = self.telegram.update_and_track(
                message_key,
                state.message_id,
                message,
                display_amount,
                state.first_time,
                state.update_count + 1,
                new_conviction,
                state.conviction_label,
            )

            if status == UpdateStatus.SUCCESS:
                self._mark_state_dirty()
                if self.verbose:
                    self._log(f"[TELEGRAM] Updated message (ID: {state.message_id})")
                return

            elif status == UpdateStatus.MESSAGE_DELETED:
                # Message was deleted by user, send new message
                if self.verbose:
                    self._log(f"[TELEGRAM] Message {state.message_id} deleted, sending new message")
                self.telegram.untrack_message(message_key)
                self._send_new_message(bet_info, net_pos, message_key, portfolio_value, conviction)
                return

            elif status == UpdateStatus.NETWORK_ERROR:
                if attempt < max_retries - 1:
                    retry_delay = 1.0 * (attempt + 1)
                    if self.verbose:
                        self._log(f"[RETRY] Network error, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                else:
                    # Failed after retries, send new message
                    self._log(f"[WARNING] Failed to update message after {max_retries} attempts, sending new message")
                    self.telegram.untrack_message(message_key)
                    self._send_new_message(bet_info, net_pos, message_key, portfolio_value, conviction)
                    return

            else:  # UNKNOWN_ERROR
                # Don't retry on unknown errors, send new message
                if self.verbose:
                    self._log(f"[TELEGRAM] Unknown error updating message, sending new message")
                self.telegram.untrack_message(message_key)
                self._send_new_message(bet_info, net_pos, message_key, portfolio_value, conviction)
                return

    def _send_stale_addition(
        self,
        bet_info: BetInfo,
        net_pos: NetPosition,
        message_key: Tuple[str, str, str],
        state: any,
        portfolio_value: Optional[float],
        conviction: Optional[ConvictionInfo],
    ):
        """Send new message for stale position addition."""
        message = self.message_formatter.format_stale_addition(
            bet=bet_info,
            net_pos=net_pos,
            first_time=state.first_time,
            previous_total=state.total_usdc,
            portfolio_value=portfolio_value,
            conviction=conviction,
        )

        display_amount = net_pos.get_display_amount()
        conviction_label = conviction.label if conviction else "MINIMAL"

        msg_id = self.telegram.send_and_track(
            message_key,
            message,
            display_amount,
            datetime.now(),
            conviction_label,
        )

        if msg_id:
            self._mark_state_dirty()
            if self.verbose:
                self._log(
                    f"[TELEGRAM] Sent stale addition message (ID: {msg_id}, total: ${display_amount:.2f})"
                )

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
        trader_list = [(name, min_shares) for _, name, min_shares, _ in self.wallets]
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
        self._log("[SHUTDOWN] Saving final state...")
        try:
            # Convert deques to lists for JSON serialization
            seen_tx_serializable = {
                wallet: list(tx_deque) for wallet, tx_deque in self.seen_transactions.items()
            }

            # Get position tracker state
            position_state = self.position_tracker.export_for_persistence()

            self.state_manager.force_save(
                {
                    "telegram_messages": self.telegram.get_state_for_persistence(),
                    "portfolio_cache": self.portfolio.export_cache_for_persistence(),
                    **position_state,  # Includes net_positions and threshold_crossed
                    "seen_transactions": seen_tx_serializable,
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

        for wallet, name, _, _ in self.wallets:
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

        # Reconstruct positions from historical trades
        self._reconstruct_positions_at_startup(trade_limit=1000)

        self._log("MONITORING ACTIVE - Waiting for new bets...")
        print(
            f"\nMonitoring {len(self.wallets)} traders. Check {self.log_file} for output.\n"
        )

        self._send_startup_message()

        while True:
            try:
                all_new_bets = []

                def fetch_wallet_trades(wallet_info):
                    wallet, name, _, _ = wallet_info
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
                    self._cleanup_net_positions()
                    self.last_shares_cleanup = current_time

                # Final state save at end of poll if dirty
                if self.state_manager.is_dirty():
                    self._save_state()

                # Reduced logging: only log every 10 polls or when verbose
                self._poll_count += 1

                if self.verbose or self._poll_count % 10 == 0:
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
        default="data/sports_monitor.log",
        help="Log file path (default: data/sports_monitor.log)",
    )

    parser.add_argument(
        "--telegram-chat-id",
        help="Override Telegram chat ID from .env (for personal vs group routing)",
    )

    parser.add_argument(
        "--state-file",
        default="data/sports_monitor_state.json",
        help="State file path (default: data/sports_monitor_state.json)",
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
                    profile_url = wallet_config.get("profile_url")

                    # Validate min_shares
                    if min_shares is not None:
                        if not isinstance(min_shares, int):
                            raise ValueError(f"min_shares must be integer for {name}, got {type(min_shares).__name__}")
                        if min_shares < 0:
                            raise ValueError(f"min_shares must be non-negative for {name}, got {min_shares}")

                    if address:
                        wallets.append((address, name, min_shares, profile_url))
            print(f"Loaded {len(wallets)} wallet(s) from {args.config}")
        except Exception as e:
            print(f"ERROR: Failed to load config file: {e}")
            return

    elif args.wallet:
        wallets = [(args.wallet, args.name, None, None)]

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
