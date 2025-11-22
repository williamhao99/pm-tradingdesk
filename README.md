# Prediction Market Trading Desk

My personal trading infrastructure for prediction markets.

Status: Active development

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Usage

```bash
# Kalshi trading dashboard
./scripts/run-dashboard.sh
```

## Kalshi Dashboard - API Endpoints

**HTTP**
- `GET /` - Dashboard UI
- `GET /api/health` - System health
- `GET /api/metrics` - Performance stats

**WebSocket `/ws`**
- `get_balance`, `get_positions`, `get_fills`, `get_orders` - Portfolio data
- `quick_order`, `cancel_order` - Trading
- `lookup_ticker`, `get_orderbook`, `unsubscribe_market` - Market data
- `get_hotkeys`, `start_hotkey_bot`, `stop_hotkey_bot`, `bot_execute_hotkey`, `generate_hotkeys` - Hotkey automation
- `take_snapshot`, `get_analytics` - Analytics

## NHL Data Collection

Scrape expected goals (xG) and game results from MoneyPuck.com for NHL prediction markets.

```bash
# Scrape all games from 2024-25 and 2025-26 seasons
python3 scripts/scrape_nhl_games.py
```

Output: `nhl_xg_data.csv` with period-by-period xG, actual goals, and winners.