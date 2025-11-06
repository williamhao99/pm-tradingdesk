#!/usr/bin/env python3
"""Polymarket user wallet address lookup from username or profile URL."""

import argparse
import re
import requests
from typing import Optional


class PolymarketUserLookup:
    """Polymarket user wallet lookup utility."""

    GAMMA_API_BASE = "https://gamma-api.polymarket.com"

    @staticmethod
    def extract_username_from_url(url: str) -> Optional[str]:
        """Extract username from profile URL."""
        match = re.search(r"@([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)

        match = re.search(r"/profile/(0x[a-fA-F0-9]{40})", url)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def search_user_by_username(username: str) -> Optional[dict]:
        """Search for user by username via profile page parsing."""
        print(f"[INFO] Searching for user: {username}")
        print("[WARN] The Polymarket API doesn't have a public user lookup endpoint.")
        print(
            "[WARN] You'll need to manually find the wallet address from the profile page.\n"
        )

        print("How to find the wallet address manually:")
        print(f"1. Visit: https://polymarket.com/@{username}")
        print("2. Right-click on the page and select 'Inspect' or 'View Page Source'")
        print("3. Search for '0x' to find Ethereum addresses in the HTML")
        print(
            "4. The wallet address should be a 42-character string starting with '0x'\n"
        )

        try:
            url = f"https://polymarket.com/@{username}"
            print(f"[INFO] Attempting to fetch profile page: {url}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            content = response.text
            wallet_pattern = r"0x[a-fA-F0-9]{40}"
            matches = re.findall(wallet_pattern, content)

            if matches:
                print(f"\n[SUCCESS] Found {len(matches)} potential wallet address(es):")
                seen = set()
                unique_matches = []
                for addr in matches:
                    addr_lower = addr.lower()
                    if addr_lower not in seen:
                        seen.add(addr_lower)
                        unique_matches.append(addr)

                for i, addr in enumerate(unique_matches, 1):
                    print(f"  {i}. {addr}")

                print(
                    "\n[INFO] The first address is usually the user's wallet address."
                )
                print(
                    "[INFO] You can verify by checking their activity on the Data API:\n"
                )
                print(
                    f"  curl 'https://data-api.polymarket.com/activity?user={unique_matches[0]}&limit=10'\n"
                )

                return {"wallet_addresses": unique_matches, "username": username}
            else:
                print("[ERROR] No wallet addresses found in profile page")
                return None

        except requests.RequestException as e:
            print(f"[ERROR] Failed to fetch profile page: {e}")
            return None

    @staticmethod
    def verify_wallet_activity(wallet_address: str) -> bool:
        """Verify wallet has Polymarket activity."""
        try:
            url = "https://data-api.polymarket.com/activity"
            params = {"user": wallet_address.lower(), "limit": 1}

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            has_activity = len(data) > 0

            if has_activity:
                print(f"[SUCCESS] Wallet {wallet_address} has Polymarket activity")
                activity = data[0]
                print(
                    f"  Last activity: {activity.get('type')} at timestamp {activity.get('timestamp')}"
                )
                print(f"  Market: {activity.get('title', 'Unknown')}")
            else:
                print(f"[WARN] Wallet {wallet_address} has no Polymarket activity")

            return has_activity

        except requests.RequestException as e:
            print(f"[ERROR] Failed to verify wallet activity: {e}")
            return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Find Polymarket user wallet addresses",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Search by username
  python src/polymarket/tools/user_lookup.py --username SomeTrader

  # Search from profile URL
  python src/polymarket/tools/user_lookup.py --url "https://polymarket.com/@SomeTrader"

  # Verify a known wallet address
  python src/polymarket/tools/user_lookup.py --verify 0x1234567890123456789012345678901234567890
        """,
    )

    parser.add_argument("--username", "-u", help="Polymarket username (without @)")

    parser.add_argument("--url", help="Full Polymarket profile URL")

    parser.add_argument(
        "--verify",
        "-v",
        help="Verify a wallet address has Polymarket activity",
    )

    args = parser.parse_args()

    lookup = PolymarketUserLookup()

    if args.verify:
        if not args.verify.startswith("0x") or len(args.verify) != 42:
            print("[ERROR] Invalid wallet address format")
            print("        Expected: 0x followed by 40 hexadecimal characters")
            return

        lookup.verify_wallet_activity(args.verify)

    elif args.username:
        lookup.search_user_by_username(args.username)

    elif args.url:
        username = lookup.extract_username_from_url(args.url)
        if username:
            if username.startswith("0x"):
                print(f"[INFO] Found wallet address in URL: {username}")
                lookup.verify_wallet_activity(username)
            else:
                lookup.search_user_by_username(username)
        else:
            print(f"[ERROR] Could not extract username from URL: {args.url}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
