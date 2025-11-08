"""Message formatting for Telegram notifications."""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from src.polymarket.utils.position_tracker_state import NetPosition, PositionStatus
from src.polymarket.utils.telegram_notifier import escape_markdown


@dataclass
class BetInfo:
    """Information about a bet for message formatting."""
    trader_name: str
    outcome: str
    market_title: str
    market_url: str
    formatted_price: str
    implied_odds: str
    formatted_time: str
    side: str  # Current trade side
    trader_profile_url: Optional[str] = None  # Optional Polymarket profile URL


@dataclass
class ConvictionInfo:
    """Conviction information for position."""
    label: str  # EXTREME, HIGH, MEDIUM, LOW, MINIMAL
    percentage: float
    marker: str  # ASCII marker like [!!!], [!!], [!], [-], [ ]


class MessageFormatter:
    """Formats Telegram messages for bet alerts."""

    # Circle-based conviction markers
    CONVICTION_MARKERS = {
        "EXTREME": "●●●●",
        "HIGH": "●●●○",
        "MEDIUM": "●●○○",
        "LOW": "●○○○",
        "MINIMAL": "○○○○",
    }

    def __init__(self, verbose: bool = False, logger=None):
        """
        Initialize message formatter.

        Args:
            verbose: Enable verbose logging
            logger: Optional logging function
        """
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)

    def _format_trader_name(self, trader_name: str, profile_url: Optional[str] = None) -> str:
        """
        Format trader name as hyperlink if URL available, otherwise escaped text.

        Args:
            trader_name: Trader name
            profile_url: Optional Polymarket profile URL

        Returns:
            Formatted trader name (hyperlink or escaped text)
        """
        if profile_url:
            # Markdown format: [Text](URL) - escape brackets in trader name
            name_escaped = trader_name.replace('[', '\\[').replace(']', '\\]')
            return f"[{name_escaped}]({profile_url})"
        else:
            # No URL, just escape for safety
            return escape_markdown(trader_name)

    def format_new_position(
        self,
        bet: BetInfo,
        net_pos: NetPosition,
        portfolio_value: Optional[float] = None,
        conviction: Optional[ConvictionInfo] = None,
    ) -> str:
        """
        Format message for new position.

        Args:
            bet: Bet information
            net_pos: Net position
            portfolio_value: Optional portfolio value
            conviction: Optional conviction info

        Returns:
            Formatted Telegram message
        """
        position_side = "BUY" if net_pos.is_long else "SELL"
        display_amount = net_pos.get_display_amount()
        position_size = abs(net_pos.shares)

        outcome_safe = escape_markdown(bet.outcome)
        trader_name_formatted = self._format_trader_name(bet.trader_name, bet.trader_profile_url)
        market_title_safe = escape_markdown(bet.market_title)

        # Fish/fade warning
        is_fish = "Fish" in bet.trader_name
        fade_warning = "\n\n*[!] FADE THIS TRADE [!]*" if is_fish else ""

        # Opposite action for fish
        opposite_action = ""
        if is_fish:
            opposite_side = "SELL" if position_side == "BUY" else "BUY"
            opposite_action = f"\n*Recommended Action:* {opposite_side} {outcome_safe}"

        # Conviction line
        conviction_line = self._format_conviction(display_amount, portfolio_value, conviction)

        message = f"""*{position_side} {outcome_safe} @ {bet.formatted_price}* (Implied: {bet.implied_odds})
*Trader:* {trader_name_formatted}{fade_warning}
━━━━━━━━━━━━━━━━━━

*Conviction:* {conviction.marker if conviction else '[ ]'} {conviction.label if conviction else 'MINIMAL'} ({conviction.percentage if conviction else 0:.1f}% of positions)
*Total Stake:* ${display_amount:,.2f}
*Position Size:* {position_size:,.0f} shares

*Market:* {market_title_safe}{opposite_action}
*First Trade:* {bet.formatted_time}

[Open Market]({bet.market_url})"""

        return message

    def format_position_update(
        self,
        bet: BetInfo,
        net_pos: NetPosition,
        first_time: datetime,
        update_count: int,
        portfolio_value: Optional[float] = None,
        conviction: Optional[ConvictionInfo] = None,
    ) -> str:
        """
        Format message for position update.

        Args:
            bet: Bet information
            net_pos: Net position
            first_time: Time of first trade
            update_count: Number of updates
            portfolio_value: Optional portfolio value
            conviction: Optional conviction info

        Returns:
            Formatted Telegram message
        """
        position_side = "BUY" if net_pos.is_long else "SELL"
        display_amount = net_pos.get_display_amount()
        position_size = abs(net_pos.shares)

        outcome_safe = escape_markdown(bet.outcome)
        trader_name_formatted = self._format_trader_name(bet.trader_name, bet.trader_profile_url)
        market_title_safe = escape_markdown(bet.market_title)

        # Fish/fade warning
        is_fish = "Fish" in bet.trader_name
        fade_warning = "\n\n*[!] FADE THIS TRADE [!]*" if is_fish else ""

        # Opposite action for fish
        opposite_action = ""
        if is_fish:
            opposite_side = "SELL" if position_side == "BUY" else "BUY"
            opposite_action = f"\n*Recommended Action:* {opposite_side} {outcome_safe}"

        # Conviction line
        conviction_line = self._format_conviction(display_amount, portfolio_value, conviction)

        message = f"""*{position_side} {outcome_safe} @ {bet.formatted_price}* (Implied: {bet.implied_odds})
*Trader:* {trader_name_formatted}{fade_warning}
━━━━━━━━━━━━━━━━━━

*Conviction:* {conviction.marker if conviction else '[ ]'} {conviction.label if conviction else 'MINIMAL'} ({conviction.percentage if conviction else 0:.1f}% of positions)
*Total Stake:* ${display_amount:,.2f}
*Position Size:* {position_size:,.0f} shares

*Market:* {market_title_safe}{opposite_action}
*First Trade:* {first_time.strftime('%I:%M:%S %p')}
*Latest:* {bet.formatted_time} *[UPDATED x{update_count}]*

[Open Market]({bet.market_url})"""

        return message

    def format_stale_addition(
        self,
        bet: BetInfo,
        net_pos: NetPosition,
        first_time: datetime,
        previous_total: float,
        portfolio_value: Optional[float] = None,
        conviction: Optional[ConvictionInfo] = None,
    ) -> str:
        """
        Format message for adding to stale position (>30 min old).

        Args:
            bet: Bet information
            net_pos: Net position
            first_time: Time of first trade
            previous_total: Previous USDC total
            portfolio_value: Optional portfolio value
            conviction: Optional conviction info

        Returns:
            Formatted Telegram message
        """
        position_side = "BUY" if net_pos.is_long else "SELL"
        display_amount = net_pos.get_display_amount()
        position_size = abs(net_pos.shares)

        # Calculate time since first trade
        current_time = datetime.now()
        time_since = current_time - first_time
        hours = time_since.total_seconds() / 3600

        outcome_safe = escape_markdown(bet.outcome)
        trader_name_formatted = self._format_trader_name(bet.trader_name, bet.trader_profile_url)
        market_title_safe = escape_markdown(bet.market_title)

        # Fish/fade warning
        is_fish = "Fish" in bet.trader_name
        fade_warning = "\n\n*[!] FADE THIS TRADE [!]*" if is_fish else ""

        # Opposite action for fish
        opposite_action = ""
        if is_fish:
            opposite_side = "SELL" if position_side == "BUY" else "BUY"
            opposite_action = f"\n*Recommended Action:* {opposite_side} {outcome_safe}"

        # Conviction line
        conviction_line = self._format_conviction(display_amount, portfolio_value, conviction)

        message = f"""*[ADDING] {position_side} {outcome_safe} @ {bet.formatted_price}* (Implied: {bet.implied_odds})
*Trader:* {trader_name_formatted}{fade_warning}
━━━━━━━━━━━━━━━━━━

*Original bet:* {hours:.1f}h ago (${previous_total:,.2f})

*Conviction:* {conviction.marker if conviction else '[ ]'} {conviction.label if conviction else 'MINIMAL'} ({conviction.percentage if conviction else 0:.1f}% of positions)
*Total Stake:* ${display_amount:,.2f}
*Position Size:* {position_size:,.0f} shares

*Market:* {market_title_safe}{opposite_action}
*First Trade:* {first_time.strftime('%I:%M:%S %p')}
*Latest:* {bet.formatted_time}

[Open Market]({bet.market_url})"""

        return message

    def format_position_close(
        self,
        bet: BetInfo,
        net_pos: NetPosition,
        original_stake: float,
    ) -> str:
        """
        Format message for position close.

        Args:
            bet: Bet information
            net_pos: Net position
            original_stake: Original stake amount

        Returns:
            Formatted Telegram message
        """
        pnl = net_pos.get_pnl()
        pnl_display = f"+${abs(pnl):,.2f}" if pnl > 0 else f"-${abs(pnl):,.2f}"
        pnl_pct = (pnl / original_stake * 100) if original_stake > 0 else 0

        outcome_safe = escape_markdown(bet.outcome)
        trader_name_formatted = self._format_trader_name(bet.trader_name, bet.trader_profile_url)

        # Determine status-specific header
        status = net_pos.get_status()
        if status == PositionStatus.PROFIT_TAKEN:
            status_label = "PROFIT TAKEN"
        elif status == PositionStatus.LOSS_REALIZED:
            status_label = "LOSS REALIZED"
        else:
            status_label = "CLOSED"

        message = f"""*[POSITION {status_label}] {outcome_safe} @ {bet.formatted_price}* (Implied: {bet.implied_odds})
*Trader:* {trader_name_formatted}
━━━━━━━━━━━━━━━━━━

*P&L:* {pnl_display} ({pnl_pct:+.1f}%)
*Original Stake:* ${original_stake:,.2f}
*Close Time:* {bet.formatted_time}

[Open Market]({bet.market_url})"""

        return message

    def _format_conviction(
        self,
        stake: float,
        portfolio_value: Optional[float],
        conviction: Optional[ConvictionInfo],
    ) -> str:
        """
        Format conviction line for message.

        Args:
            stake: Stake amount
            portfolio_value: Portfolio value
            conviction: Conviction info

        Returns:
            Formatted conviction line (with leading newline) or empty string
        """
        if not portfolio_value or portfolio_value <= 0 or not conviction:
            return ""

        marker = self.CONVICTION_MARKERS.get(conviction.label, "")

        # Note: "positions" not "portfolio" since we exclude cash balance
        return f"\n*Conviction:* {marker} {conviction.label} ({conviction.percentage:.1f}% of positions)"
