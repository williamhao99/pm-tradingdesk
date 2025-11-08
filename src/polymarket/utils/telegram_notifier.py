"""Telegram notification handler with message tracking and updates."""

import ast
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Tuple, Callable
import requests


class UpdateStatus(Enum):
    """Status of a message update attempt."""
    SUCCESS = "success"  # Update succeeded
    MESSAGE_DELETED = "deleted"  # Message was deleted by user
    NETWORK_ERROR = "network_error"  # Transient network/API error (should retry)
    UNKNOWN_ERROR = "unknown"  # Other error


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown (basic mode)."""
    special_chars = ["_", "*", "`", "["]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


@dataclass
class MessageState:
    """State for a tracked Telegram message."""

    message_id: int
    total_usdc: float
    first_time: datetime
    update_count: int
    conviction_label: str


class TelegramNotifier:
    """Handles Telegram messaging with message tracking and updates."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        session: requests.Session,
        stale_threshold_seconds: int = 1800,
        verbose: bool = False,
        logger: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token
            chat_id: Telegram chat ID
            session: Requests session for API calls
            stale_threshold_seconds: Time before message considered stale (default: 30 min)
            verbose: Enable verbose logging
            logger: Optional logging function
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session
        self.stale_threshold_seconds = stale_threshold_seconds
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)
        self.enabled = bool(bot_token and chat_id)

        # Message tracking: key -> MessageState (3-tuple: wallet, market, outcome)
        # Note: SIDE is excluded to track NET position across BUY and SELL
        self.messages: Dict[Tuple[str, str, str], MessageState] = {}
        self._lock = threading.Lock()

    def create_message_key(
        self, wallet: str, market_slug: str, outcome: str
    ) -> Tuple[str, str, str]:
        """Create normalized tracking key (3-tuple: wallet, market, outcome)."""
        return (
            wallet.lower(),
            market_slug.lower(),
            outcome.upper(),
        )

    def send_message(self, text: str) -> Optional[int]:
        """Send message, returning message ID."""
        if not self.enabled:
            return None

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()

            result = response.json()
            return result.get("result", {}).get("message_id")

        except requests.HTTPError as e:
            # Log the response body for 400 errors to see the actual issue
            if e.response is not None:
                try:
                    error_detail = e.response.json()
                    self.logger(
                        f"[TELEGRAM ERROR] Failed to send message: {e.response.status_code} - {error_detail}"
                    )
                except:
                    self.logger(f"[TELEGRAM ERROR] Failed to send message: {e}")
            else:
                self.logger(f"[TELEGRAM ERROR] Failed to send message: {e}")
            return None
        except requests.RequestException as e:
            self.logger(f"[TELEGRAM ERROR] Failed to send message: {e}")
            return None

    def update_message(self, message_id: int, text: str) -> UpdateStatus:
        """
        Update existing message.

        Returns:
            UpdateStatus indicating success or specific failure type
        """
        if not self.enabled:
            return UpdateStatus.UNKNOWN_ERROR

        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return UpdateStatus.SUCCESS

        except requests.HTTPError as e:
            if e.response:
                status_code = e.response.status_code

                # Handle 400 errors (bad request)
                if status_code == 400:
                    # Check specific error type from description
                    try:
                        error_data = e.response.json()
                        description = error_data.get("description", "").lower()

                        # Message content unchanged - treat as success
                        if "message is not modified" in description:
                            if self.verbose:
                                self.logger(
                                    f"[TELEGRAM] Message {message_id} unchanged (skipped update)"
                                )
                            return UpdateStatus.SUCCESS

                        # Message deleted by user
                        elif "message to edit not found" in description or "message not found" in description:
                            self.logger(
                                f"[TELEGRAM] Message {message_id} no longer exists (deleted by user)"
                            )
                            return UpdateStatus.MESSAGE_DELETED

                    except (json.JSONDecodeError, KeyError, AttributeError):
                        # Can't parse error details, treat as unknown
                        pass

                    # Other 400 errors (bad request, invalid markdown, etc)
                    self.logger(f"[TELEGRAM ERROR] Bad request for message {message_id}: {e}")
                    return UpdateStatus.UNKNOWN_ERROR

                # Rate limit or server errors (should retry)
                elif status_code in [429, 500, 502, 503, 504]:
                    self.logger(
                        f"[TELEGRAM ERROR] Transient error ({status_code}) for message {message_id}: {e}"
                    )
                    return UpdateStatus.NETWORK_ERROR

                else:
                    self.logger(f"[TELEGRAM ERROR] HTTP {status_code} for message {message_id}: {e}")
                    return UpdateStatus.UNKNOWN_ERROR
            else:
                self.logger(f"[TELEGRAM ERROR] Failed to update message {message_id}: {e}")
                return UpdateStatus.NETWORK_ERROR

        except requests.Timeout as e:
            self.logger(f"[TELEGRAM ERROR] Timeout updating message {message_id}: {e}")
            return UpdateStatus.NETWORK_ERROR

        except requests.RequestException as e:
            self.logger(f"[TELEGRAM ERROR] Network error updating message {message_id}: {e}")
            return UpdateStatus.NETWORK_ERROR

    def track_message(
        self,
        key: Tuple[str, str, str],
        message_id: int,
        usdc_amount: float,
        timestamp: datetime,
        conviction_label: str,
    ):
        """Track a new message for future updates."""
        with self._lock:
            self.messages[key] = MessageState(
                message_id=message_id,
                total_usdc=usdc_amount,
                first_time=timestamp,
                update_count=0,
                conviction_label=conviction_label,
            )

    def update_tracked_message(
        self,
        key: Tuple[str, str, str],
        message_id: int,
        new_total_usdc: float,
        first_time: datetime,
        new_update_count: int,
        new_conviction: str,
    ):
        """Update tracked message state."""
        with self._lock:
            self.messages[key] = MessageState(
                message_id=message_id,
                total_usdc=new_total_usdc,
                first_time=first_time,
                update_count=new_update_count,
                conviction_label=new_conviction,
            )

    def get_message_state(
        self, key: Tuple[str, str, str]
    ) -> Optional[MessageState]:
        """Get tracked message state (thread-safe)."""
        with self._lock:
            return self.messages.get(key)

    def has_tracked_message(self, key: Tuple[str, str, str]) -> bool:
        """Check if message is tracked."""
        with self._lock:
            return key in self.messages

    def untrack_message(self, key: Tuple[str, str, str]):
        """Remove message from tracking."""
        with self._lock:
            if key in self.messages:
                del self.messages[key]

    def is_message_stale(self, state: MessageState, current_time: datetime) -> bool:
        """Check if message is too old to update."""
        time_diff = (current_time - state.first_time).total_seconds()
        return time_diff > self.stale_threshold_seconds

    def send_and_track(
        self,
        key: Tuple[str, str, str],
        text: str,
        usdc_amount: float,
        timestamp: datetime,
        conviction_label: str,
    ) -> Optional[int]:
        """Send message and track it for updates."""
        msg_id = self.send_message(text)

        if msg_id:
            self.track_message(key, msg_id, usdc_amount, timestamp, conviction_label)

            if self.verbose:
                self.logger(
                    f"[TELEGRAM] Sent new message (ID: {msg_id}, conviction: {conviction_label})"
                )

        return msg_id

    def update_and_track(
        self,
        key: Tuple[str, str, str],
        message_id: int,
        text: str,
        new_total_usdc: float,
        first_time: datetime,
        new_update_count: int,
        new_conviction: str,
        last_conviction: str,
    ) -> UpdateStatus:
        """
        Update message and track the new state.

        Returns:
            UpdateStatus indicating success or specific failure type
        """
        status = self.update_message(message_id, text)

        if status == UpdateStatus.SUCCESS:
            self.update_tracked_message(
                key,
                message_id,
                new_total_usdc,
                first_time,
                new_update_count,
                new_conviction,
            )

            if self.verbose:
                conviction_note = (
                    f" ({last_conviction} -> {new_conviction})"
                    if new_conviction != last_conviction
                    else ""
                )
                self.logger(
                    f"[TELEGRAM] Updated message {message_id} (total: ${new_total_usdc:.2f}){conviction_note}"
                )

        return status

    def send_startup_message(
        self, num_wallets: int, poll_interval: int, trader_list: list
    ):
        """
        Send startup notification.

        Args:
            num_wallets: Number of wallets being monitored
            poll_interval: Poll interval in seconds
            trader_list: List of tuples (name, min_shares) for each trader
        """
        if not self.enabled:
            return

        # Format trader list with min_shares info
        trader_lines = []
        for name, min_shares in trader_list:
            name_safe = escape_markdown(name)
            if min_shares:
                trader_lines.append(f"- {name_safe} (min: {min_shares:,} shares)")
            else:
                trader_lines.append(f"- {name_safe}")

        trader_list_formatted = "\n".join(trader_lines)

        message = f"""*[SPORTS MONITOR STARTED]*

*Status:* Online and monitoring
*Traders:* {num_wallets}
*Poll Interval:* {poll_interval}s

*Watching:*
{trader_list_formatted}

Ready to alert on new trades!"""

        try:
            self.send_message(message)
            self.logger("[OK] Startup message sent to Telegram")
        except Exception as e:
            self.logger(f"[TELEGRAM ERROR] Startup message failed: {e}")

    def send_shutdown_message(self, uptime_seconds: int, total_alerts: int):
        """Send shutdown notification."""
        if not self.enabled:
            return

        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = (
            f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
        )

        message = f"""*[SPORTS MONITOR STOPPED]*

*Status:* Offline
*Uptime:* {uptime_str}
*Total Alerts:* {total_alerts}

Bot has been stopped."""

        try:
            self.send_message(message)
            self.logger("[OK] Shutdown message sent to Telegram")
        except Exception as e:
            self.logger(f"[TELEGRAM ERROR] Shutdown message failed: {e}")

    def get_state_for_persistence(self) -> Dict:
        """
        Export message tracking state for external persistence.

        Returns dict suitable for JSON serialization.
        """
        with self._lock:
            state = {}
            for key, msg_state in self.messages.items():
                key_list = list(key)  # Convert tuple to list for JSON
                key_str = json.dumps(key_list)  # Proper JSON string, not repr
                state[key_str] = {
                    "message_id": msg_state.message_id,
                    "total_usdc": msg_state.total_usdc,
                    "first_time": msg_state.first_time.isoformat(),
                    "update_count": msg_state.update_count,
                    "conviction_label": msg_state.conviction_label,
                }
            return state

    def load_state_from_persistence(self, state_dict: Dict):
        """Load message tracking state from external persistence."""
        with self._lock:
            self.messages.clear()

            for key_str, value in state_dict.items():
                # Parse key from string (handles both JSON and Python repr formats)
                try:
                    # Try JSON format first (new format)
                    key_list = json.loads(key_str)
                except json.JSONDecodeError:
                    # Fall back to Python repr format (old format)
                    try:
                        key_list = ast.literal_eval(key_str)
                    except (ValueError, SyntaxError):
                        # Skip malformed keys
                        self.logger(f"[WARNING] Skipping malformed key: {key_str}")
                        continue

                key = tuple(key_list)

                # Handle different formats for backward compatibility
                if isinstance(value, dict):
                    self.messages[key] = MessageState(
                        message_id=value["message_id"],
                        total_usdc=value["total_usdc"],
                        first_time=datetime.fromisoformat(value["first_time"]),
                        update_count=value["update_count"],
                        conviction_label=value.get("conviction_label", "UNKNOWN"),
                    )
                else:
                    # Old format: list [msg_id, total_usdc, first_time_str, update_count, conviction]
                    if len(value) == 5:
                        self.messages[key] = MessageState(
                            message_id=value[0],
                            total_usdc=value[1],
                            first_time=datetime.fromisoformat(value[2]),
                            update_count=value[3],
                            conviction_label=value[4],
                        )

    def cleanup_old_messages(self, cutoff_time: datetime) -> int:
        """Remove messages older than cutoff_time. Returns count removed."""
        with self._lock:
            keys_to_remove = [
                key
                for key, state in self.messages.items()
                if state.first_time < cutoff_time
            ]

            for key in keys_to_remove:
                del self.messages[key]

            return len(keys_to_remove)
