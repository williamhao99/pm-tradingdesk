#!/usr/bin/env python3
"""Fetch current metrics from dashboard."""

import json
import sys
from datetime import datetime
from pathlib import Path
import requests

DASHBOARD_URL = "http://localhost:8000"


def get_metrics():
    """Fetch metrics from dashboard."""
    try:
        response = requests.get(f"{DASHBOARD_URL}/api/metrics", timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError:
        print("Dashboard not running")
        print("Start: ./scripts/run-dashboard.sh")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def print_metrics(metrics):
    """Print metrics summary."""
    order = metrics.get("order_placement", {})
    ws = metrics.get("websocket", {})
    cache = metrics.get("cache", {})

    print("\n" + "=" * 60)
    print("Current Metrics")
    print("=" * 60)

    if order.get("total_orders", 0) > 0:
        print(f"\nOrders: {order.get('total_orders', 0)}")
        print(f"  p99: {order.get('p99_ms', 0):.0f}ms")
        print(f"  Success: {order.get('success_rate_pct', 100):.1f}%")

    if ws.get("total_messages", 0) > 0:
        msg = ws.get("message_processing", {})
        print(f"\nWebSocket: {ws.get('total_messages', 0):,} messages")
        print(f"  p99: {msg.get('p99_ms', 0):.1f}ms")
        print(f"  Throughput: {ws.get('throughput_per_sec', 0):.1f}/sec")

    total_cache = cache.get("hits", 0) + cache.get("misses", 0)
    if total_cache > 0:
        print(f"\nCache: {cache.get('hit_rate_pct', 0):.1f}% hit rate")

    print(f"\nUptime: {metrics.get('uptime_hours', 0):.2f}h")
    print("=" * 60 + "\n")


def save_metrics(metrics):
    """Save metrics to file."""
    metrics["snapshot_timestamp"] = datetime.now().isoformat()
    output_file = Path(__file__).parent / "performance_metrics.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    print(f"Saved: {output_file}\n")


if __name__ == "__main__":
    metrics = get_metrics()
    print_metrics(metrics)
    save_metrics(metrics)
