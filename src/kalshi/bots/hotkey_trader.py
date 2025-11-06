#!/usr/bin/env python3
"""Ultra-low latency keyword-triggered trading bot (150-300ms execution)."""

import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.kalshi.clients.kalshi_client import KalshiClient


class HotkeyTrader:
    """Keyword-triggered instant order execution with precomputed parameters."""

    def __init__(self, config_path: str = "src/kalshi/tools/hotkeys.json"):
        self.client = KalshiClient()

        project_root = Path(__file__).parent.parent.parent.parent
        config_file = project_root / config_path

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except FileNotFoundError:
            print(f"ERROR: Config file not found: {config_file}")
            print("Creating default config...")
            self._create_default_config(config_file)
            with open(config_file, "r", encoding="utf-8") as f:
                self.config = json.load(f)

        self.hotkeys = self.config.get("hotkeys", {})
        self.defaults = self.config.get("defaults", {})

        # Precompute order parameters at initialization for O(1) execution
        self._precomputed_orders = {}
        for keyword, hk_config in self.hotkeys.items():
            normalized_key = keyword.lower().strip()
            ticker = hk_config["ticker"]
            side = hk_config.get("side", self.defaults["side"])
            action = hk_config.get("action", self.defaults["action"])
            count = hk_config.get("count", self.defaults["count"])
            order_type = hk_config.get("type", self.defaults["type"])

            self._precomputed_orders[normalized_key] = (
                ticker,
                side,
                action,
                count,
                order_type,
            )

        self.trades_executed = 0
        self.total_latency = 0.0

    def _create_default_config(self, config_file: Path):
        """Create default hotkeys.json."""
        default_config = {
            "hotkeys": {
                "example": {
                    "ticker": "KALSHI-EXAMPLE-123",
                    "side": "yes",
                    "action": "buy",
                    "count": 100,
                    "type": "market",
                    "description": "Example hotkey - edit hotkeys.json to customize",
                }
            },
            "defaults": {
                "side": "yes",
                "action": "buy",
                "count": 100,
                "type": "market",
            },
        }

        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)

    def execute_hotkey(self, keyword: str) -> Optional[Dict]:
        """Execute order for keyword."""
        start_ns = time.perf_counter_ns()

        keyword_normalized = keyword.lower().strip()

        if keyword_normalized not in self._precomputed_orders:
            print(f"ERROR: Unknown hotkey: '{keyword}'")
            print("\nAvailable hotkeys:")
            for key, config in self.hotkeys.items():
                desc = config.get("description", "")
                print(f"  - {key:20} -> {config['ticker']:30} {desc}")
            return None

        ticker, side, action, count, order_type = self._precomputed_orders[
            keyword_normalized
        ]

        try:
            print(f"EXECUTING: {keyword} -> {ticker}")
            print(f"   {action.upper()} {count} {side.upper()} @ MARKET")

            order = self.client.place_order(
                ticker=ticker,
                action=action,
                side=side,
                count=count,
                order_type=order_type,
            )

            latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            self.trades_executed += 1
            self.total_latency += latency_ms

            try:
                if hasattr(order, "order"):
                    order_obj = order.order
                    order_id = getattr(order_obj, "order_id", "unknown")
                    status = getattr(order_obj, "status", "unknown")
                else:
                    order_id = "unknown"
                    status = "placed"
            except Exception:
                order_id = "unknown"
                status = "placed"

            print(f"SUCCESS: ORDER PLACED ({latency_ms:.0f}ms)")
            print(f"   Order ID: {order_id}")
            print(f"   Status: {status}")
            print(f"   Ticker: {ticker}")
            print(f"   {action.upper()} {count} {side.upper()}")

            avg_latency = self.total_latency / self.trades_executed
            print(
                f"\nSession Stats: {self.trades_executed} trades, avg {avg_latency:.0f}ms"
            )

            return order

        except Exception as e:
            latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            print(f"ERROR: ORDER FAILED ({latency_ms:.0f}ms)")
            print(f"   Error: {e}")
            return None

    def list_hotkeys(self):
        """Display configured hotkeys."""
        if not self.hotkeys:
            print("ERROR: No hotkeys configured. Edit hotkeys.json to add hotkeys.")
            return

        default_count = self.defaults.get("count", 100)
        default_side = self.defaults.get("side", "yes").upper()
        default_action = self.defaults.get("action", "buy").upper()

        print(
            f"\nCONFIGURED HOTKEYS ({default_action} {default_count} {default_side}):"
        )
        print("=" * 80)

        for keyword, config in self.hotkeys.items():
            ticker = config.get("ticker", "")
            description = config.get("description", "")
            count = config.get("count", default_count)
            side = config.get("side", self.defaults.get("side", "yes")).upper()
            action = config.get("action", self.defaults.get("action", "buy")).upper()

            print(f"  '{keyword:20s}' -> {ticker:30s} ({action} {count} {side})")
            if description:
                print(f"{'':25s}   {description}")

        print("=" * 80)
        print(f"Total: {len(self.hotkeys)} hotkeys configured")

    def run(self):
        """Run interactive REPL."""
        print("\nHOTKEY TRADER | Commands: list, stats, quit")

        self.list_hotkeys()

        print("\nReady...\n")

        while True:
            try:
                keyword = input(">>> ").strip()

                if not keyword:
                    continue

                if keyword.lower() in ["quit", "exit", "q"]:
                    print("\nExiting hotkey trader...")
                    break

                elif keyword.lower() == "list":
                    self.list_hotkeys()

                elif keyword.lower() == "stats":
                    if self.trades_executed > 0:
                        avg_latency = self.total_latency / self.trades_executed
                        print("\nSESSION STATISTICS:")
                        print(f"   Trades executed: {self.trades_executed}")
                        print(f"   Average latency: {avg_latency:.0f}ms")
                        print(f"   Total latency:   {self.total_latency:.0f}ms")
                    else:
                        print("\nNo trades executed yet")

                else:
                    self.execute_hotkey(keyword)

                print()

            except KeyboardInterrupt:
                print("\n\nExiting hotkey trader...")
                break

            except EOFError:
                print("\n\nExiting hotkey trader...")
                break


def main():
    trader = HotkeyTrader()
    trader.run()


if __name__ == "__main__":
    main()
