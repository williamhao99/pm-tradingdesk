#!/bin/bash
# Tail Sharp Traders - Sports Monitor (Multiple Wallets)

cd "$(dirname "$0")/.."

# Check virtual environment
if [ ! -d "venv" ]; then
    echo "Error: venv not found. Run: python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

echo "Starting Sports Monitor..."
echo "Monitoring traders from config/trader_list.json"
echo "Sending to group chat (-4896100438)"
echo ""

# Run sports monitor using venv Python
exec venv/bin/python -m src.polymarket.bots.sports_monitor \
    --config config/trader_list.json \
    --poll-interval 5 \
    --log-file data/sharps_monitor.log \
    --state-file data/sharps_monitor_state.json \
    --trades-log-file data/sharps_trades.jsonl \
    --telegram-chat-id -4896100438