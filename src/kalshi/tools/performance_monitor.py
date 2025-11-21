"""Lightweight performance monitoring for trading operations."""

import time
from typing import Dict, Optional
from collections import defaultdict, deque
from contextlib import contextmanager


class RollingStats:
    """
    Rolling statistics tracker with fixed memory footprint.

    Maintains last N samples for percentile calculation.
    Min/max are all-time values; mean/percentiles are rolling.
    """

    def __init__(self, max_samples: int = 1000):
        self.samples = deque(maxlen=max_samples)
        self.count = 0
        self.min_ms = float("inf")
        self.max_ms = 0.0

    def add(self, value_ms: float) -> None:
        """Add sample."""
        self.samples.append(value_ms)
        self.count += 1
        self.min_ms = min(self.min_ms, value_ms)
        self.max_ms = max(self.max_ms, value_ms)

    def summary(self) -> Dict:
        """Compute summary statistics."""
        if self.count == 0:
            return {
                "count": 0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "mean_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
            }

        sorted_samples = sorted(self.samples)
        n = len(sorted_samples)

        return {
            "count": self.count,
            "min_ms": round(self.min_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "mean_ms": round(sum(self.samples) / len(self.samples), 2),
            "p50_ms": round(sorted_samples[n // 2], 2),
            "p95_ms": round(sorted_samples[int(n * 0.95)], 2),
            "p99_ms": round(sorted_samples[int(n * 0.99)], 2),
        }


class PerformanceMonitor:
    """
    Performance metrics tracker for trading operations.

    Tracks latency, throughput, and success rates for orders, API calls,
    WebSocket messages, and cache operations.
    """

    def __init__(self, max_samples: int = 1000):
        self.ws_message_latencies = RollingStats(max_samples)
        self.order_placement_latencies = RollingStats(max_samples)
        self.reconnection_times = RollingStats(max_samples)
        self.api_call_latencies = RollingStats(max_samples)

        self.ws_messages_received = 0
        self.ws_reconnections = 0
        self.orders_placed = 0
        self.orders_failed = 0
        self.api_calls_total = 0
        self.api_calls_by_status = defaultdict(int)
        self.cache_hits = 0
        self.cache_misses = 0

        self.recent_messages = deque(maxlen=100)
        self.recent_orders = deque(maxlen=100)
        self.recent_api_calls = deque(maxlen=100)

        self.start_time = time.time()

    def track_order_placement(self, latency_ms: float, success: bool) -> None:
        """Track order placement latency and success."""
        self.order_placement_latencies.add(latency_ms)
        if success:
            self.orders_placed += 1
        else:
            self.orders_failed += 1
        self.recent_orders.append(time.time())

    def track_ws_message(self, processing_latency_ms: float) -> None:
        """Track WebSocket message processing."""
        self.ws_message_latencies.add(processing_latency_ms)
        self.ws_messages_received += 1
        self.recent_messages.append(time.time())

    def track_ws_reconnection(self, downtime_ms: float) -> None:
        """Track WebSocket reconnection."""
        self.reconnection_times.add(downtime_ms)
        self.ws_reconnections += 1

    def track_api_call(self, path: str, latency_ms: float, status_code: int) -> None:
        """Track API call."""
        self.api_call_latencies.add(latency_ms)
        self.api_calls_total += 1
        self.api_calls_by_status[status_code] += 1
        self.recent_api_calls.append(time.time())

    def track_cache_access(self, hit: bool) -> None:
        """Track cache access."""
        if hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

    @contextmanager
    def track_operation(self, operation_type: str):
        """Context manager for operation tracking."""
        start = time.time()
        success = False
        try:
            yield
            success = True
        finally:
            latency_ms = (time.time() - start) * 1000
            if operation_type == "order":
                self.track_order_placement(latency_ms, success)
            elif operation_type == "ws_message":
                self.track_ws_message(latency_ms)

    def _get_message_throughput(self) -> float:
        """Calculate recent WebSocket throughput."""
        if len(self.recent_messages) < 2:
            return 0.0
        time_span = self.recent_messages[-1] - self.recent_messages[0]
        return len(self.recent_messages) / time_span if time_span > 0 else 0.0

    def _get_order_throughput(self) -> float:
        """Calculate recent order throughput."""
        if len(self.recent_orders) < 2:
            return 0.0
        time_span = self.recent_orders[-1] - self.recent_orders[0]
        return len(self.recent_orders) / time_span if time_span > 0 else 0.0

    def _get_order_success_rate(self) -> float:
        """Calculate order success rate."""
        total = self.orders_placed + self.orders_failed
        return (self.orders_placed / total * 100) if total > 0 else 100.0

    def _get_api_call_throughput(self) -> float:
        """Calculate recent API call throughput."""
        if len(self.recent_api_calls) < 2:
            return 0.0
        time_span = self.recent_api_calls[-1] - self.recent_api_calls[0]
        return len(self.recent_api_calls) / time_span if time_span > 0 else 0.0

    def _get_cache_hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total * 100) if total > 0 else 0.0

    def get_summary(self) -> Dict:
        """Return complete performance metrics."""
        uptime_hrs = (time.time() - self.start_time) / 3600

        return {
            "uptime_hours": round(uptime_hrs, 2),
            "order_placement": {
                **self.order_placement_latencies.summary(),
                "total_orders": self.orders_placed,
                "success_rate_pct": round(self._get_order_success_rate(), 2),
                "throughput_per_sec": round(self._get_order_throughput(), 2),
            },
            "websocket": {
                "message_processing": self.ws_message_latencies.summary(),
                "reconnection": self.reconnection_times.summary(),
                "total_messages": self.ws_messages_received,
                "total_reconnections": self.ws_reconnections,
                "throughput_per_sec": round(self._get_message_throughput(), 2),
            },
            "api_calls": {
                **self.api_call_latencies.summary(),
                "total_calls": self.api_calls_total,
                "throughput_per_sec": round(self._get_api_call_throughput(), 2),
                "by_status": dict(self.api_calls_by_status),
            },
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate_pct": round(self._get_cache_hit_rate(), 2),
            },
        }

    def print_summary(self) -> None:
        """Print performance summary."""
        summary = self.get_summary()

        print("\n" + "=" * 60)
        print("Performance Metrics")
        print("=" * 60)

        print(f"\nUptime: {summary['uptime_hours']:.1f}h")

        order = summary["order_placement"]
        if order["total_orders"] > 0:
            print(
                f"\nOrders: {order['total_orders']} ({order['success_rate_pct']:.1f}% success)"
            )
            print(
                f"  Latency: p50={order['p50_ms']:.0f}ms p95={order['p95_ms']:.0f}ms p99={order['p99_ms']:.0f}ms"
            )
            print(f"  Throughput: {order['throughput_per_sec']:.1f}/sec")

        ws = summary["websocket"]
        if ws["total_messages"] > 0:
            msg = ws["message_processing"]
            print(f"\nWebSocket: {ws['total_messages']:,} messages")
            print(
                f"  Processing: p50={msg['p50_ms']:.1f}ms p95={msg['p95_ms']:.1f}ms p99={msg['p99_ms']:.1f}ms"
            )
            print(f"  Throughput: {ws['throughput_per_sec']:.1f}/sec")
            if ws["total_reconnections"] > 0:
                print(
                    f"  Reconnections: {ws['total_reconnections']} (p99={ws['reconnection']['p99_ms']:.0f}ms)"
                )

        api = summary["api_calls"]
        if api["total_calls"] > 0:
            print(f"\nAPI: {api['total_calls']:,} calls")
            print(
                f"  Latency: p50={api['p50_ms']:.0f}ms p95={api['p95_ms']:.0f}ms p99={api['p99_ms']:.0f}ms"
            )
            print(f"  Throughput: {api['throughput_per_sec']:.1f}/sec")
            if api["by_status"]:
                status_str = ", ".join(
                    [f"{k}:{v}" for k, v in sorted(api["by_status"].items())]
                )
                print(f"  Status: {status_str}")

        cache = summary["cache"]
        total_cache = cache["hits"] + cache["misses"]
        if total_cache > 0:
            print(
                f"\nCache: {cache['hit_rate_pct']:.1f}% hit rate ({cache['hits']}/{total_cache})"
            )

        print("=" * 60 + "\n")


# Global singleton
_monitor = PerformanceMonitor()


def get_monitor() -> PerformanceMonitor:
    """Return global monitor instance."""
    return _monitor
