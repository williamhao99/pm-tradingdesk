#!/usr/bin/env python3
"""Scrape xG data for all NHL games (2024 and 2025 seasons)."""

import asyncio
import csv
import logging
import sys
from pathlib import Path
from typing import Dict, List

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from nhl.moneypuck import MoneyPuckScraper

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


async def scrape_season(
    scraper: MoneyPuckScraper, season: int, start_game: int, end_game: int
) -> List[Dict]:
    """
    Scrape all games for a season.

    Args:
        scraper: MoneyPuckScraper instance
        season: Season year (2024, 2025)
        start_game: First game number (e.g., 1)
        end_game: Last game number (e.g., 1312 for full season)

    Returns:
        List of game data dicts
    """
    results = []

    for game_num in range(start_game, end_game + 1):
        # Format: {season}02{game_num:04d}
        # Example: 2025020319 = 2025 season, regular season (02), game 0319
        game_id = f"{season}02{game_num:04d}"

        try:
            game_xg = await scraper.get_period_xg(game_id)

            # Calculate goal differentials for each period (goals scored in that period only)
            p1_diff = game_xg.period_1_xg.home_goals - game_xg.period_1_xg.away_goals
            p2_diff = (
                game_xg.period_2_xg.home_goals - game_xg.period_1_xg.home_goals
            ) - (game_xg.period_2_xg.away_goals - game_xg.period_1_xg.away_goals)
            p3_diff = (
                game_xg.period_3_xg.home_goals - game_xg.period_2_xg.home_goals
            ) - (game_xg.period_3_xg.away_goals - game_xg.period_2_xg.away_goals)

            results.append(
                {
                    "game_id": game_id,
                    "season": season,
                    "home_team": game_xg.home_team,
                    "away_team": game_xg.away_team,
                    "regulation_winner": game_xg.regulation_winner,
                    "winner": game_xg.winner,
                    "p1_home_xg": round(game_xg.period_1_xg.home_xg, 2),
                    "p1_away_xg": round(game_xg.period_1_xg.away_xg, 2),
                    "p1_home_goals": game_xg.period_1_xg.home_goals,
                    "p1_away_goals": game_xg.period_1_xg.away_goals,
                    "p1_goal_diff": p1_diff,
                    "p2_home_xg": round(game_xg.period_2_xg.home_xg, 2),
                    "p2_away_xg": round(game_xg.period_2_xg.away_xg, 2),
                    "p2_home_goals": game_xg.period_2_xg.home_goals,
                    "p2_away_goals": game_xg.period_2_xg.away_goals,
                    "p2_goal_diff": p2_diff,
                    "p3_home_xg": round(game_xg.period_3_xg.home_xg, 2),
                    "p3_away_xg": round(game_xg.period_3_xg.away_xg, 2),
                    "p3_home_goals": game_xg.period_3_xg.home_goals,
                    "p3_away_goals": game_xg.period_3_xg.away_goals,
                    "p3_goal_diff": p3_diff,
                }
            )

            if game_num % 50 == 0:
                logger.info(f"Scraped {game_num}/{end_game} games for {season} season")

        except Exception as e:
            # Most failures are 404 (game doesn't exist yet)
            if "404" not in str(e):
                logger.warning(f"Failed {game_id}: {e}")
            continue

    logger.info(f"{season} season: {len(results)} games scraped")
    return results


async def main() -> None:
    """Scrape 2024-25 and 2025-26 seasons."""
    async with MoneyPuckScraper() as scraper:
        all_results = []

        # 2024-25 season: Games 1-1312 (82 games × 32 teams / 2)
        logger.info("Scraping 2024-25 season (full season)...")
        results_2024 = await scrape_season(scraper, 2024, 1, 1312)
        all_results.extend(results_2024)

        # 2025-26 season: Games 1-1312 (ongoing season, some games may not exist yet)
        logger.info("Scraping 2025-26 season (ongoing)...")
        results_2025 = await scrape_season(scraper, 2025, 1, 1312)
        all_results.extend(results_2025)

        # Save to CSV
        output_file = Path("nhl_xg_data.csv")

        with open(output_file, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "game_id",
                    "season",
                    "home_team",
                    "away_team",
                    "regulation_winner",
                    "winner",
                    "p1_home_xg",
                    "p1_away_xg",
                    "p1_home_goals",
                    "p1_away_goals",
                    "p1_goal_diff",
                    "p2_home_xg",
                    "p2_away_xg",
                    "p2_home_goals",
                    "p2_away_goals",
                    "p2_goal_diff",
                    "p3_home_xg",
                    "p3_away_xg",
                    "p3_home_goals",
                    "p3_away_goals",
                    "p3_goal_diff",
                ],
            )
            writer.writeheader()
            writer.writerows(all_results)

        logger.info(f"\nSaved {len(all_results)} games to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
