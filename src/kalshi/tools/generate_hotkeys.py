#!/usr/bin/env python3
"""Auto-generate hotkeys.json from Kalshi market series."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv

from config.constants import KALSHI_BASE_URL, PROJECT_ROOT
from src.kalshi.auth import get_auth_headers, load_private_key

load_dotenv()


async def _fetch_market_detail(
    session: aiohttp.ClientSession,
    ticker: str,
    private_key: rsa.RSAPrivateKey,
    api_key_id: str,
    index: int,
    total: int,
) -> Optional[Dict]:
    """Fetch market detail."""
    try:
        detail_path = f"/trade-api/v2/markets/{ticker}"
        headers = get_auth_headers(private_key, api_key_id, "GET", detail_path)
        url = f"{KALSHI_BASE_URL}{detail_path}"

        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()

            if "market" in data:
                print(f"  [{index}/{total}] {ticker}")
                return data["market"]
            return None
    except (aiohttp.ClientError, ValueError, KeyError) as e:
        print(f"  [FAILED] {ticker}: {e}")
        return None


async def _fetch_all_market_details(
    session: aiohttp.ClientSession,
    markets: List[Dict],
    private_key: rsa.RSAPrivateKey,
    api_key_id: str,
) -> List[Dict]:
    """Fetch market details in parallel."""
    print(f"Fetching details for {len(markets)} markets...")

    tasks = []
    for i, market in enumerate(markets):
        ticker = market.get("ticker")
        if ticker:
            task = _fetch_market_detail(
                session, ticker, private_key, api_key_id, i + 1, len(markets)
            )
            tasks.append(task)

    detailed_markets = await asyncio.gather(*tasks, return_exceptions=True)

    valid_markets = [
        m for m in detailed_markets if m is not None and not isinstance(m, Exception)
    ]

    print(f"Fetched {len(valid_markets)} markets")
    return valid_markets


async def fetch_markets_by_pattern(search_pattern: str) -> List[Dict]:
    """Fetch markets by series ticker."""
    print(f"Searching for markets matching: {search_pattern}")

    try:
        api_key_id = os.getenv("KALSHI_API_KEY_ID")
        if not api_key_id:
            raise ValueError("KALSHI_API_KEY_ID not found in environment")

        key_file = PROJECT_ROOT / "kalshi_private_key.pem"
        private_key = load_private_key(key_file)

        path = f"/trade-api/v2/markets?limit=200&series_ticker={search_pattern}&status=open"
        headers = get_auth_headers(private_key, api_key_id, "GET", path)
        url = f"{KALSHI_BASE_URL}{path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

            markets = data.get("markets", [])
            print(f"Found {len(markets)} markets")

            if not markets:
                return []

            detailed_markets = await _fetch_all_market_details(
                session, markets, private_key, api_key_id
            )
            return detailed_markets

    except (aiohttp.ClientError, ValueError, OSError) as e:
        print(f"Error fetching markets: {e}")
        return []


def extract_keyword_from_market(market: Dict) -> Optional[str]:
    """Extract keyword from yes_sub_title."""
    keyword = market.get("yes_sub_title") or market.get("no_sub_title")

    if not keyword:
        return None

    keyword = keyword.lower().strip()

    if "/" in keyword:
        keyword = keyword.split("/")[0].strip()

    return keyword


def generate_hotkeys_config(
    markets: List[Dict],
    default_count: int = 200,
    default_side: str = "yes",
    default_action: str = "buy",
    custom_keywords: Optional[Dict[str, str]] = None,
) -> Dict:
    """Generate hotkeys config sorted alphabetically by keyword."""
    hotkeys = {}
    custom_keywords = custom_keywords or {}

    for market in markets:
        ticker = market.get("ticker")
        title = market.get("title", "")
        yes_sub_title = market.get("yes_sub_title", "")

        if not ticker:
            continue

        if ticker in custom_keywords:
            keyword = custom_keywords[ticker]
        else:
            keyword = extract_keyword_from_market(market)

        if not keyword:
            print(f"WARNING: Skipping {ticker} - couldn't extract keyword")
            continue

        description = (
            yes_sub_title
            if yes_sub_title
            else (title[:50] if title else f"{keyword} mention")
        )

        hotkeys[keyword] = {
            "ticker": ticker,
            "side": default_side,
            "action": default_action,
            "count": default_count,
            "type": "limit",
            "description": description,
        }

        print(f"  '{keyword}' -> {ticker} ({yes_sub_title})")

    sorted_hotkeys = dict(sorted(hotkeys.items(), key=lambda x: x[0]))

    return {
        "hotkeys": sorted_hotkeys,
        "defaults": {
            "side": default_side,
            "action": default_action,
            "count": default_count,
            "type": "limit",
        },
    }


def save_hotkeys_config(
    config: Dict, output_path: str = "src/kalshi/tools/hotkeys.json"
) -> None:
    """Write hotkeys config to file."""
    output_file = PROJECT_ROOT / output_path

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    print(f"\nSaved hotkeys to {output_file}")


async def interactive_mode() -> None:
    """Interactive CLI for hotkey generation."""
    print("\nKalshi Hotkey Generator")
    print("-" * 40)

    print("\nExamples:")
    print("  - KXEARNINGSMENTIONAAPL")
    print("  - KXEARNINGSMENTIONTSLA")

    search_pattern = input("\nEnter series ticker: ").strip().upper()

    if not search_pattern:
        print("No pattern provided")
        return

    count_input = input("Default share count [200]: ").strip()
    default_count = int(count_input) if count_input else 200

    print("\nFetching markets...")

    markets = await fetch_markets_by_pattern(search_pattern)

    if not markets:
        print("No markets found")
        return

    print(f"\nFound {len(markets)} markets")

    config = generate_hotkeys_config(markets, default_count=default_count)

    if not config["hotkeys"]:
        print("No hotkeys generated")
        return

    print(f"\nGenerated {len(config['hotkeys'])} hotkeys")

    save_confirm = input("\nSave to hotkeys.json? [Y/n]: ").strip().lower()

    if save_confirm in ["", "y", "yes"]:
        save_hotkeys_config(config)
        print("Done! Run ./scripts/run-hotkey-trader.sh to start trading")
    else:
        print("Cancelled")


async def main_async() -> None:
    """Async main entry point."""
    if len(sys.argv) > 1:
        search_pattern = sys.argv[1].upper()
        default_count = int(sys.argv[2]) if len(sys.argv) > 2 else 200

        markets = await fetch_markets_by_pattern(search_pattern)

        if markets:
            config = generate_hotkeys_config(markets, default_count=default_count)
            save_hotkeys_config(config)
    else:
        await interactive_mode()


def main() -> None:
    """Sync wrapper for async main."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
