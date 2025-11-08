"""Generic state persistence manager with debouncing and atomic writes."""

import json
import threading
import time
from pathlib import Path
from typing import Dict, Any, Callable, Optional


class StateManager:
    """Manages JSON state persistence with debouncing and atomic writes."""

    def __init__(
        self,
        state_file: Path,
        debounce_seconds: int = 10,
        verbose: bool = False,
        logger: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize state manager.

        Args:
            state_file: Path to state file
            debounce_seconds: Minimum seconds between saves (default: 10)
            verbose: Enable verbose logging
            logger: Optional logging function
        """
        self.state_file = state_file
        self.debounce_seconds = debounce_seconds
        self.verbose = verbose
        self.logger = logger or (lambda msg: None)

        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = time.time()

    def load(self) -> Dict[str, Any]:
        """
        Load state from file.

        Returns:
            Dict containing state data (empty dict if file doesn't exist)
        """
        if not self.state_file.exists():
            if self.verbose:
                self.logger(f"[STATE] No existing state file at {self.state_file}")
            return {}

        try:
            with self._lock:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

            if self.verbose:
                self.logger(
                    f"[STATE] Loaded state from {self.state_file} ({len(data)} top-level keys)"
                )

            return data

        except json.JSONDecodeError as e:
            self.logger(f"[STATE ERROR] Invalid JSON in {self.state_file}: {e}")
            return {}
        except Exception as e:
            self.logger(f"[STATE ERROR] Failed to load {self.state_file}: {e}")
            return {}

    def save(self, data: Dict[str, Any], force: bool = False) -> bool:
        """
        Save state to file atomically with debouncing.

        Args:
            data: State data to save
            force: Skip debouncing and save immediately

        Returns:
            True if saved, False if debounced or error
        """
        current_time = time.time()

        # Check debounce window (unless forced)
        if not force:
            time_since_last_save = current_time - self._last_save
            if time_since_last_save < self.debounce_seconds:
                self._dirty = True
                if self.verbose:
                    self.logger(
                        f"[STATE] Save debounced ({time_since_last_save:.1f}s < {self.debounce_seconds}s)"
                    )
                return False

        # Perform atomic write
        with self._lock:
            try:
                # Write to temp file first
                temp_file = self.state_file.with_suffix(".tmp")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)

                # Atomic rename
                temp_file.replace(self.state_file)

                self._dirty = False
                self._last_save = current_time

                if self.verbose:
                    self.logger(f"[STATE] Saved to {self.state_file}")

                return True

            except Exception as e:
                self.logger(f"[STATE ERROR] Failed to save {self.state_file}: {e}")
                return False

    def mark_dirty(self):
        """Mark state as dirty (needs saving)."""
        self._dirty = True

    def is_dirty(self) -> bool:
        """Check if state has unsaved changes."""
        return self._dirty

    def should_save(self) -> bool:
        """Check if debounce window has passed and state is dirty."""
        if not self._dirty:
            return False

        current_time = time.time()
        time_since_last_save = current_time - self._last_save
        return time_since_last_save >= self.debounce_seconds

    def force_save(self, data: Dict[str, Any]) -> bool:
        """
        Force immediate save (skip debouncing).

        Args:
            data: State data to save

        Returns:
            True if successful, False otherwise
        """
        return self.save(data, force=True)
