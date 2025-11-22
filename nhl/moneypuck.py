"""MoneyPuck.com xG data scraper."""

import asyncio
import logging
from io import StringIO
from typing import Optional, Tuple

import aiohttp
import pandas as pd

from nhl.models import GameXG, PeriodXG

logger = logging.getLogger(__name__)


class MoneyPuckScraper:
    """
    Fetch xG data from MoneyPuck.

    Features:
    - Async HTTP session with lazy initialization
    - Automatic team name resolution via NHL API
    - Full game data extraction (all 3 periods + OT/SO)
    """

    BASE_URL = "https://moneypuck.com/moneypuck/gameData"

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazy session initialization with thread safety."""
        async with self._session_lock:
            if self.session is None:
                self.session = aiohttp.ClientSession()
            return self.session

    async def close(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None

    async def _get_team_names(self, game_id: str) -> Tuple[str, str]:
        """
        Fetch team names from NHL API.

        Args:
            game_id: Format "2024020001"

        Returns:
            Tuple of (home_team_abbrev, away_team_abbrev)
        """
        nhl_api_url = f"https://api-web.nhle.com/v1/gamecenter/{game_id}/landing"

        try:
            session = await self._ensure_session()
            async with session.get(nhl_api_url) as response:
                response.raise_for_status()
                data = await response.json()

            home_team = data["homeTeam"]["abbrev"]
            away_team = data["awayTeam"]["abbrev"]
            return (home_team, away_team)

        except Exception as e:
            logger.warning(f"Failed to fetch team names for {game_id}: {e}")
            return ("HOME", "AWAY")

    async def get_period_xg(self, game_id: str) -> GameXG:
        """
        Get xG at end of period 1, period 2, and period 3, plus final winner.

        Args:
            game_id: Format "2025020319"

        Returns:
            GameXG with P1, P2, P3 expected goals, regulation winner, and final winner
        """
        session = await self._ensure_session()

        # Extract season from game_id (first 4 chars: "2024")
        # MoneyPuck directory format: {season}{season+1} (e.g., "20242025")
        season = int(game_id[:4])
        season_dir = f"{season}{season+1}"
        url = f"{self.BASE_URL}/{season_dir}/{game_id}.csv"

        logger.info("Fetching: %s", url)

        async with session.get(url) as response:
            response.raise_for_status()
            content = await response.text()

        df = pd.read_csv(StringIO(content))

        # Fetch team names from NHL API
        home_team, away_team = await self._get_team_names(game_id)

        # Period 1: 0-1200 seconds (20 minutes)
        # Period 2: 1200-2400 seconds (20 minutes)
        # Period 3: 2400-3600 seconds (20 minutes)
        # Regulation ends at 3600 seconds
        p1_data = df[df["time"] <= 1200].iloc[-1]
        p2_data = df[df["time"] <= 2400].iloc[-1]
        p3_data = df[df["time"] <= 3600].iloc[-1]

        # Get final score (last row includes OT/SO if it happened)
        final_data = df.iloc[-1]

        period_1 = PeriodXG(
            period=1,
            home_xg=p1_data.get("homeTeamExpectedGoals", 0.0),
            away_xg=p1_data.get("awayTeamExpectedGoals", 0.0),
            home_goals=int(p1_data.get("homeTeamGoals", 0)),
            away_goals=int(p1_data.get("awayTeamGoals", 0)),
        )

        period_2 = PeriodXG(
            period=2,
            home_xg=p2_data.get("homeTeamExpectedGoals", 0.0),
            away_xg=p2_data.get("awayTeamExpectedGoals", 0.0),
            home_goals=int(p2_data.get("homeTeamGoals", 0)),
            away_goals=int(p2_data.get("awayTeamGoals", 0)),
        )

        period_3 = PeriodXG(
            period=3,
            home_xg=p3_data.get("homeTeamExpectedGoals", 0.0),
            away_xg=p3_data.get("awayTeamExpectedGoals", 0.0),
            home_goals=int(p3_data.get("homeTeamGoals", 0)),
            away_goals=int(p3_data.get("awayTeamGoals", 0)),
        )

        # Determine regulation winner (after 60 minutes)
        if period_3.home_goals > period_3.away_goals:
            regulation_winner = "home"
        elif period_3.away_goals > period_3.home_goals:
            regulation_winner = "away"
        else:
            regulation_winner = "tied"

        # Determine final winner (including OT/SO)
        final_home_goals = int(final_data.get("homeTeamGoals", 0))
        final_away_goals = int(final_data.get("awayTeamGoals", 0))

        if final_home_goals > final_away_goals:
            winner = "home"
        elif final_away_goals > final_home_goals:
            winner = "away"
        else:
            # Should never happen in NHL (someone always wins)
            # But handle edge case for ongoing/incomplete games
            winner = regulation_winner if regulation_winner != "tied" else "home"

        return GameXG(
            game_id=game_id,
            home_team=home_team,
            away_team=away_team,
            period_1_xg=period_1,
            period_2_xg=period_2,
            period_3_xg=period_3,
            regulation_winner=regulation_winner,
            winner=winner,
        )
