"""Data models for NHL game xG data."""

from dataclasses import dataclass


@dataclass
class PeriodXG:
    """Expected goals and actual goals at end of period."""

    period: int
    home_xg: float
    away_xg: float
    home_goals: int
    away_goals: int


@dataclass
class GameXG:
    """xG data for an NHL game."""

    game_id: str
    home_team: str
    away_team: str
    period_1_xg: PeriodXG
    period_2_xg: PeriodXG
    period_3_xg: PeriodXG
    regulation_winner: str  # "home", "away", or "tied" (after 60 minutes)
    winner: str  # "home" or "away" (final winner including OT/SO)
