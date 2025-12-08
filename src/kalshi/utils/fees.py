"""Kalshi fee calculations.

Formulas (Oct 2025): taker = ceil(0.07 * n * p * (1-p)), maker = ceil(0.0175 * n * p * (1-p))
"""

import math


def taker_fee_cents(price_cents: int, contracts: int) -> float:
    """Calculate taker fee in cents (rounded up)."""
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    raw = 0.07 * contracts * p * (1.0 - p)
    return math.ceil(raw * 100.0)


def maker_fee_cents(price_cents: int, contracts: int) -> float:
    """Calculate maker fee in cents (rounded up)."""
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    raw = 0.0175 * contracts * p * (1.0 - p)
    return math.ceil(raw * 100.0)


def calculate_fee(price_cents: int, contracts: int, is_maker: bool = False) -> float:
    """Calculate trading fee in cents (rounded up)."""
    if is_maker:
        return maker_fee_cents(price_cents, contracts)
    return taker_fee_cents(price_cents, contracts)
