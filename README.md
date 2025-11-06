# Prediction Market Trading Desk

My personal trading infrastructure for Kalshi and Polymarket.

Features: Real-time web dashboard with <50ms WebSocket latency, ultra-low latency hotkey trader (150-300ms execution), and Polymarket multi-wallet activity monitoring via Telegram bot.

Status: Active development. Currently running in production on my personal VM (DigitalOcean); public demo coming soon.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` to add API credentials. Optional: configure Telegram for mobile alerts.

## Usage

```bash
# Kalshi trading dashboard
./scripts/run-dashboard.sh

# Kalshi hotkey trader
./scripts/run-hotkey-trader.sh

# Polymarket monitor
venv/bin/python -m src.polymarket.bots.sports_monitor --wallet 0x...
```