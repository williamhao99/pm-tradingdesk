#!/usr/bin/env python3
"""Auto-generate hotkeys.json from Kalshi market series using async parallel fetching."""

import sys
import json
import re
import os
import time
import base64
import asyncio
from pathlib import Path
from typing import List, Dict, Optional

import aiohttp
from dotenv import load_dotenv
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from src.kalshi.clients.kalshi_client import KalshiClient

load_dotenv()


def _get_auth_headers(path: str, private_key, api_key_id: str) -> Dict[str, str]:
    """Generate RSA-PSS authentication headers."""
    timestamp = str(int(time.time() * 1000))
    msg = f"{timestamp}GET{path}"

    signature = private_key.sign(
        msg.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode()

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


async def _make_authenticated_request_async(
    session: aiohttp.ClientSession, path: str, private_key, api_key_id: str
) -> Dict:
    """Make async authenticated API request."""
    headers = _get_auth_headers(path, private_key, api_key_id)
    url = f"https://api.elections.kalshi.com{path}"

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_market_detail_async(
    session: aiohttp.ClientSession,
    ticker: str,
    private_key,
    api_key_id: str,
    index: int,
    total: int,
) -> Optional[Dict]:
    """Fetch market detail with progress display."""
    try:
        detail_path = f"/trade-api/v2/markets/{ticker}"
        detail_data = await _make_authenticated_request_async(
            session, detail_path, private_key, api_key_id
        )

        if "market" in detail_data:
            print(f"  [{index}/{total}] {ticker}")
            return detail_data["market"]
        return None
    except Exception as e:
        print(f"  [FAILED] {ticker}: {e}")
        return None


async def _fetch_all_market_details_async(
    markets: List[Dict], private_key, api_key_id: str
) -> List[Dict]:
    """Fetch market details in parallel using asyncio.gather."""
    print(f"Fetching details for {len(markets)} markets in parallel...")

    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_market_detail_async(
                session,
                market.get("ticker"),
                private_key,
                api_key_id,
                i + 1,
                len(markets),
            )
            for i, market in enumerate(markets)
            if market.get("ticker")
        ]

        results = await asyncio.gather(*tasks)

    detailed_markets = [m for m in results if m is not None]
    print(f"Fetched detailed data for {len(detailed_markets)} markets")
    return detailed_markets


def fetch_markets_by_pattern(search_pattern: str) -> List[Dict]:
    """Fetch markets by series ticker with async parallel fetching."""
    print(f"Searching for markets matching: {search_pattern}")

    try:
        api_key_id = os.getenv("KALSHI_API_KEY_ID")
        if not api_key_id:
            raise ValueError("KALSHI_API_KEY_ID not found in environment")

        project_root = Path(__file__).parent.parent.parent.parent
        key_file = project_root / "kalshi_private_key.pem"

        with open(key_file, "r", encoding="utf-8") as f:
            private_key_pem = f.read()

        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None, backend=default_backend()
        )

        path = f"/trade-api/v2/markets?limit=200&series_ticker={search_pattern}&status=open"
        headers = _get_auth_headers(path, private_key, api_key_id)
        url = f"https://api.elections.kalshi.com{path}"

        import requests

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        markets = data.get("markets", [])
        print(f"Found {len(markets)} markets from list endpoint")

        if not markets:
            return []

        detailed_markets = asyncio.run(
            _fetch_all_market_details_async(markets, private_key, api_key_id)
        )

        return detailed_markets

    except Exception as e:
        print(f"ERROR: Error fetching markets: {e}")
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
            "type": "market",
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
            "type": "market",
        },
    }


def save_hotkeys_config(
    config: Dict, output_path: str = "src/kalshi/tools/hotkeys.json"
):
    """Write hotkeys config to file."""
    project_root = Path(__file__).parent.parent.parent.parent
    output_file = project_root / output_path

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    print(f"\nSaved hotkeys to {output_file}")


def interactive_mode():
    """Interactive CLI for hotkey generation."""
    print("\n" + "=" * 80)
    print("KALSHI HOTKEY GENERATOR")
    print("=" * 80)

    print("\nExamples:")
    print("  - KXEARNINGSMENTIONAAPL (Apple earnings mentions)")
    print("  - KXEARNINGSMENTIONROKU (Roku earnings mentions)")
    print("  - KXEARNINGSMENTIONTSLA (Tesla earnings mentions)")

    search_pattern = input("\nEnter series ticker pattern: ").strip().upper()

    if not search_pattern:
        print("ERROR: No pattern provided")
        return

    count_input = input("\nDefault share count [200]: ").strip()
    default_count = int(count_input) if count_input else 200

    print("\nConnecting to Kalshi...")

    markets = fetch_markets_by_pattern(search_pattern)

    if not markets:
        print("ERROR: No markets found")
        return

    print(f"\nFound {len(markets)} markets:")
    for i, market in enumerate(markets[:10], 1):
        ticker = market.get("ticker")
        yes_sub_title = market.get("yes_sub_title", "")
        print(f"  {i}. {ticker}")
        if yes_sub_title:
            print(f"     Strike: {yes_sub_title}")

    if len(markets) > 10:
        print(f"  ... and {len(markets) - 10} more")

    print("\nGenerating hotkeys configuration...")

    config = generate_hotkeys_config(markets, default_count=default_count)

    if not config["hotkeys"]:
        print("ERROR: No hotkeys generated")
        return

    print(f"\nGenerated {len(config['hotkeys'])} hotkeys:")
    for keyword, hk_config in list(config["hotkeys"].items())[:5]:
        print(f"  - '{keyword}' -> BUY {default_count} YES")

    if len(config["hotkeys"]) > 5:
        print(f"  ... and {len(config['hotkeys']) - 5} more")

    save_confirm = input("\nSave to hotkeys.json? [Y/n]: ").strip().lower()

    if save_confirm in ["", "y", "yes"]:
        save_hotkeys_config(config)
        print("\nDone! Run ./scripts/run-hotkey-trader.sh to start trading")
    else:
        print("\nCancelled")


def main():
    if len(sys.argv) > 1:
        search_pattern = sys.argv[1].upper()
        default_count = int(sys.argv[2]) if len(sys.argv) > 2 else 200

        markets = fetch_markets_by_pattern(search_pattern)

        if markets:
            config = generate_hotkeys_config(markets, default_count=default_count)
            save_hotkeys_config(config)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
