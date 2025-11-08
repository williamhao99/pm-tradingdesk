"""Position tracking for net positions across BUY/SELL trades."""

import ast
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List
from enum import Enum


class PositionStatus(Enum):
    """Status of a position."""
    ACTIVE = "active"  # Position open with positive net USDC invested
    PROFIT_TAKEN = "profit_taken"  # Closed with profit (negative net USDC)
    LOSS_REALIZED = "loss_realized"  # Closed with loss (positive net USDC, but closed)
    CLOSED = "closed"  # Closed near break-even


@dataclass
class NetPosition:
    """Tracks net position for a (wallet, market, outcome) tuple."""

    shares: float = 0.0  # Net shares (BUY adds, SELL subtracts)
    usdc: float = 0.0  # Net USDC invested (BUY adds, SELL subtracts)

    @property
    def is_closed(self) -> bool:
        """Check if position is effectively closed (< 1 share)."""
        return abs(self.shares) < 1.0

    @property
    def is_long(self) -> bool:
        """Check if position is net long (positive shares)."""
        return self.shares > 0

    def get_status(self) -> PositionStatus:
        """Determine position status based on shares and USDC."""
        if not self.is_closed:
            return PositionStatus.ACTIVE

        # Position closed - check profit/loss
        if self.usdc < -1.0:  # Took out more than invested (profit)
            return PositionStatus.PROFIT_TAKEN
        elif self.usdc > 1.0:  # Lost money
            return PositionStatus.LOSS_REALIZED
        else:  # Break-even (within $1)
            return PositionStatus.CLOSED

    def get_display_amount(self) -> float:
        """Get amount to display in messages (always positive)."""
        return abs(self.usdc)

    def get_pnl(self) -> float:
        """
        Get profit/loss for closed positions.
        Positive = profit, Negative = loss
        """
        if not self.is_closed:
            return 0.0
        return -self.usdc  # Negative USDC = profit

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {"shares": self.shares, "usdc": self.usdc}

    @classmethod
    def from_dict(cls, data: dict) -> "NetPosition":
        """Create from dict (JSON deserialization)."""
        return cls(shares=data.get("shares", 0.0), usdc=data.get("usdc", 0.0))


class PositionTracker:
    """Manages net positions across BUY and SELL trades."""

    def __init__(self, verbose: bool = False, logger=None):
        """
        Initialize position tracker.

        Args:
            verbose: Enable verbose logging
            logger: Optional logging function
        """
        self.positions: Dict[Tuple[str, str, str], NetPosition] = {}
        self.threshold_crossed: Dict[Tuple[str, str, str], bool] = {}
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)

    def create_position_key(
        self, wallet: str, market_slug: str, outcome: str
    ) -> Tuple[str, str, str]:
        """Create normalized position key (3-tuple: wallet, market, outcome)."""
        return (wallet.lower(), market_slug.lower(), outcome.upper())

    def update_position(
        self,
        wallet: str,
        market_slug: str,
        outcome: str,
        side: str,
        shares: float,
        usdc: float,
    ) -> NetPosition:
        """
        Update position with new trade.

        Args:
            wallet: Wallet address
            market_slug: Market slug
            outcome: Outcome name
            side: "BUY" or "SELL"
            shares: Trade size in shares
            usdc: Trade size in USDC

        Returns:
            Updated NetPosition
        """
        position_key = self.create_position_key(wallet, market_slug, outcome)

        if position_key not in self.positions:
            self.positions[position_key] = NetPosition()

        net_pos = self.positions[position_key]
        previous_shares = net_pos.shares
        previous_usdc = net_pos.usdc

        # Update NET position (BUY adds, SELL subtracts)
        if side.upper() == "BUY":
            net_pos.shares += shares
            net_pos.usdc += usdc
        elif side.upper() == "SELL":
            net_pos.shares -= shares
            net_pos.usdc -= usdc

        return net_pos

    def get_position(self, wallet: str, market_slug: str, outcome: str) -> Optional[NetPosition]:
        """Get position for key."""
        position_key = self.create_position_key(wallet, market_slug, outcome)
        return self.positions.get(position_key)

    def has_position(self, wallet: str, market_slug: str, outcome: str) -> bool:
        """Check if position exists."""
        position_key = self.create_position_key(wallet, market_slug, outcome)
        return position_key in self.positions

    def mark_threshold_crossed(self, wallet: str, market_slug: str, outcome: str):
        """Mark that position has crossed min_shares threshold."""
        position_key = self.create_position_key(wallet, market_slug, outcome)
        self.threshold_crossed[position_key] = True

    def has_crossed_threshold(self, wallet: str, market_slug: str, outcome: str) -> bool:
        """Check if position has crossed threshold."""
        position_key = self.create_position_key(wallet, market_slug, outcome)
        return self.threshold_crossed.get(position_key, False)

    def reset_threshold(self, wallet: str, market_slug: str, outcome: str):
        """Reset threshold flag (for closed positions)."""
        position_key = self.create_position_key(wallet, market_slug, outcome)
        if position_key in self.threshold_crossed:
            del self.threshold_crossed[position_key]

    def cleanup_orphaned_positions(self, tracked_keys: set):
        """
        Remove positions no longer being tracked.

        Args:
            tracked_keys: Set of position keys currently tracked by TelegramNotifier
        """
        orphaned_positions = set(self.positions.keys()) - tracked_keys
        orphaned_thresholds = set(self.threshold_crossed.keys()) - tracked_keys

        for key in orphaned_positions:
            del self.positions[key]

        for key in orphaned_thresholds:
            del self.threshold_crossed[key]

        total_cleaned = len(orphaned_positions) + len(orphaned_thresholds)
        if total_cleaned > 0 and self.verbose:
            self.logger(
                f"[CLEANUP] Removed {len(orphaned_positions)} positions, "
                f"{len(orphaned_thresholds)} thresholds"
            )

    def export_for_persistence(self) -> dict:
        """Export state for JSON serialization."""
        return {
            "net_positions": {
                str(k): v.to_dict() for k, v in self.positions.items()
            },
            "threshold_crossed": {str(k): v for k, v in self.threshold_crossed.items()},
        }

    def load_from_persistence(self, data: dict):
        """Load state from persisted data."""
        # Load net positions
        net_positions_data = data.get("net_positions", {})
        if net_positions_data:
            self.positions = {}
            for k, v in net_positions_data.items():
                try:
                    key = tuple(ast.literal_eval(k))
                    self.positions[key] = NetPosition.from_dict(v)
                except Exception as e:
                    self.logger(f"[WARNING] Failed to load net position {k}: {e}")

            if self.positions and self.verbose:
                self.logger(f"[OK] Loaded {len(self.positions)} net positions")

        # Load threshold flags
        threshold_data = data.get("threshold_crossed", {})
        if threshold_data:
            self.threshold_crossed = {tuple(ast.literal_eval(k)): v for k, v in threshold_data.items()}
            if self.verbose:
                self.logger(f"[OK] Loaded {len(self.threshold_crossed)} threshold crossed flags")

    def migrate_legacy_data(self, legacy_cumulative: dict):
        """
        Migrate from old cumulative_shares format.

        Args:
            legacy_cumulative: Dict with 4-tuple keys (wallet, market, outcome, side)
        """
        if not legacy_cumulative:
            return

        self.logger("[INFO] Migrating legacy cumulative_shares to net_positions format")

        for k, shares in legacy_cumulative.items():
            try:
                key_tuple = tuple(ast.literal_eval(k))
                if len(key_tuple) == 4:
                    # Old format: (wallet, market, outcome, side)
                    wallet, market, outcome, side = key_tuple
                    pos_key = (wallet.lower(), market.lower(), outcome.upper())

                    # Initialize if doesn't exist
                    if pos_key not in self.positions:
                        self.positions[pos_key] = NetPosition()

                    # Use side to determine if we add or subtract
                    # Note: USDC is estimated and will be corrected as new trades come in
                    if side.upper() == "BUY":
                        self.positions[pos_key].shares += shares
                    elif side.upper() == "SELL":
                        self.positions[pos_key].shares -= shares
            except Exception as e:
                self.logger(f"[WARNING] Failed to migrate cumulative share {k}: {e}")

        if self.positions:
            self.logger(
                f"[OK] Migrated {len(self.positions)} positions from legacy format"
            )
