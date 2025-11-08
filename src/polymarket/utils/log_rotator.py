"""Log rotation utility with backup and retention management."""

import os
import time
from pathlib import Path
from typing import Optional, Callable


class LogRotator:
    """Manages log file rotation with backups and retention."""

    def __init__(
        self,
        log_file: Path,
        max_bytes: int = 10 * 1024 * 1024,  # 10 MB default
        backup_count: int = 7,  # Keep 7 backups (7 days)
        rotation_time_seconds: int = 86400,  # 24 hours
        logger: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize log rotator.

        Args:
            log_file: Path to log file
            max_bytes: Max file size before rotation (default: 10 MB)
            backup_count: Number of backup files to keep (default: 7)
            rotation_time_seconds: Time between rotations (default: 24h)
            logger: Optional logging function
        """
        self.log_file = log_file
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.rotation_time_seconds = rotation_time_seconds
        self.logger = logger or (lambda msg: None)

        self.last_rotation_check = time.time()

    def should_rotate(self) -> bool:
        """Check if log should be rotated (size OR time based)."""
        if not self.log_file.exists():
            return False

        # Check file size
        try:
            size = self.log_file.stat().st_size
            if size >= self.max_bytes:
                return True
        except OSError:
            return False

        # Check time since last rotation
        current_time = time.time()
        time_since_check = current_time - self.last_rotation_check

        if time_since_check >= self.rotation_time_seconds:
            return True

        return False

    def rotate(self):
        """
        Rotate log file with backups.

        Pattern: log.txt -> log.txt.1 -> log.txt.2 -> ... -> log.txt.N (deleted)
        """
        if not self.log_file.exists():
            return

        try:
            # Remove oldest backup if at limit
            oldest_backup = self.log_file.with_suffix(
                f"{self.log_file.suffix}.{self.backup_count}"
            )
            if oldest_backup.exists():
                oldest_backup.unlink()

            # Shift existing backups
            for i in range(self.backup_count - 1, 0, -1):
                src = self.log_file.with_suffix(f"{self.log_file.suffix}.{i}")
                dst = self.log_file.with_suffix(f"{self.log_file.suffix}.{i + 1}")

                if src.exists():
                    src.rename(dst)

            # Move current log to .1 backup
            backup = self.log_file.with_suffix(f"{self.log_file.suffix}.1")
            self.log_file.rename(backup)

            # Create new empty log file
            self.log_file.touch()

            self.last_rotation_check = time.time()

            self.logger(
                f"[LOG ROTATION] Rotated {self.log_file.name} "
                f"(size: {backup.stat().st_size:,} bytes, keeping {self.backup_count} backups)"
            )

        except Exception as e:
            self.logger(f"[LOG ROTATION ERROR] Failed to rotate {self.log_file}: {e}")

    def check_and_rotate(self):
        """Check if rotation needed and perform if necessary."""
        if self.should_rotate():
            self.rotate()

    def cleanup_old_backups(self):
        """Remove backup files older than retention policy."""
        try:
            for i in range(self.backup_count + 1, self.backup_count + 100):
                old_backup = self.log_file.with_suffix(f"{self.log_file.suffix}.{i}")
                if old_backup.exists():
                    old_backup.unlink()
                    self.logger(f"[LOG CLEANUP] Removed old backup: {old_backup.name}")
                else:
                    break
        except Exception as e:
            self.logger(f"[LOG CLEANUP ERROR] {e}")
