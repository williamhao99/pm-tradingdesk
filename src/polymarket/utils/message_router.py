"""Message routing logic for determining how to handle bet alerts."""

from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum
from datetime import datetime

from src.polymarket.utils.position_tracker_state import NetPosition, PositionStatus


class MessageAction(Enum):
    """Action to take for a bet alert."""
    SKIP = "skip"  # Don't send any message
    NEW = "new"  # Send new message
    UPDATE = "update"  # Update existing message
    CLOSE = "close"  # Send position close notification
    STALE_ADDITION = "stale_addition"  # Send new message for stale position


@dataclass
class MessageDecision:
    """Decision about what message action to take."""
    action: MessageAction
    reason: str
    skip_portfolio_fetch: bool = False  # Optimization: skip portfolio fetch if true


class MessageRouter:
    """Routes bet alerts to appropriate message handlers."""

    def __init__(
        self,
        min_update_pct: float = 5.0,
        min_update_abs: float = 100.0,
        stale_threshold_seconds: int = 1800,
        verbose: bool = False,
        logger=None,
    ):
        """
        Initialize message router.

        Args:
            min_update_pct: Minimum % change to trigger update (default 5%)
            min_update_abs: Minimum absolute change to trigger update (default $100)
            stale_threshold_seconds: Time before message considered stale (default 30 min)
            verbose: Enable verbose logging
            logger: Optional logging function
        """
        self.min_update_pct = min_update_pct
        self.min_update_abs = min_update_abs
        self.stale_threshold_seconds = stale_threshold_seconds
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)

    def should_alert_position(
        self,
        net_pos: NetPosition,
        min_shares: Optional[int],
        is_tracked: bool,
        has_crossed_threshold: bool,
    ) -> Tuple[bool, str]:
        """
        Determine if position should generate an alert.

        Args:
            net_pos: Current net position
            min_shares: Minimum shares threshold (None = no threshold)
            is_tracked: Whether position is already being tracked
            has_crossed_threshold: Whether threshold was previously crossed

        Returns:
            (should_alert, reason)
        """
        # Position already tracked - always alert (for updates/closes)
        if is_tracked:
            status = net_pos.get_status()
            if status != PositionStatus.ACTIVE:
                return (True, "position_closed")
            return (True, "position_update")

        # Position closed but not tracked - skip
        if net_pos.is_closed:
            return (False, "closed_untracked")

        # Check threshold
        if min_shares is not None:
            if abs(net_pos.shares) >= min_shares:
                if not has_crossed_threshold:
                    return (True, "threshold_crossed")
                return (True, "above_threshold")
            else:
                return (False, "below_threshold")

        # No threshold, always alert
        return (True, "no_threshold")

    def decide_message_action(
        self,
        net_pos: NetPosition,
        message_state: Optional[dict],
        current_timestamp: int,
    ) -> MessageDecision:
        """
        Decide what action to take for a message.

        Args:
            net_pos: Current net position
            message_state: Existing message state (None if no message exists)
            current_timestamp: Current trade timestamp

        Returns:
            MessageDecision with action and reason
        """
        if net_pos.is_closed:
            if message_state:
                return MessageDecision(
                    action=MessageAction.CLOSE,
                    reason="position_closed",
                    skip_portfolio_fetch=True,
                )
            else:
                return MessageDecision(
                    action=MessageAction.SKIP,
                    reason="closed_untracked",
                    skip_portfolio_fetch=True,
                )

        if not message_state:
            return MessageDecision(
                action=MessageAction.NEW,
                reason="new_position",
                skip_portfolio_fetch=False,
            )

        old_usdc = message_state.get("total_usdc", 0)
        new_usdc = net_pos.get_display_amount()

        if not self._is_significant_change(old_usdc, new_usdc):
            return MessageDecision(
                action=MessageAction.SKIP,
                reason="change_too_small",
                skip_portfolio_fetch=True,
            )

        first_time = message_state.get("first_time")
        if first_time:
            current_time = datetime.fromtimestamp(current_timestamp)
            if isinstance(first_time, str):
                first_time = datetime.fromisoformat(first_time)

            time_diff = (current_time - first_time).total_seconds()
            if time_diff > self.stale_threshold_seconds:
                return MessageDecision(
                    action=MessageAction.STALE_ADDITION,
                    reason="stale_position",
                    skip_portfolio_fetch=False,
                )

        return MessageDecision(
            action=MessageAction.UPDATE,
            reason="significant_change",
            skip_portfolio_fetch=False,
        )

    def _is_significant_change(self, old_usdc: float, new_usdc: float) -> bool:
        """
        Check if change is significant enough to warrant update.

        Args:
            old_usdc: Previous USDC amount
            new_usdc: New USDC amount

        Returns:
            True if change is significant
        """
        if old_usdc <= 0:
            return True

        change_pct = abs((new_usdc - old_usdc) / old_usdc * 100)
        change_abs = abs(new_usdc - old_usdc)

        is_significant = change_pct >= self.min_update_pct or change_abs >= self.min_update_abs

        if not is_significant and self.verbose:
            self.logger(
                f"[SKIP UPDATE] Change too small: {change_pct:.1f}% (${change_abs:.2f})"
            )

        return is_significant
