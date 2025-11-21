#!/bin/bash
# Generate hotkeys from Kalshi markets

cd "$(dirname "$0")/.."

# Check virtual environment
if [ ! -d "venv" ]; then
    echo "Error: venv not found. Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Check .env
if [ ! -f .env ]; then
    echo "Error: .env not found. Run: cp .env.example .env"
    exit 1
fi

# Check private key
if [ ! -f kalshi_private_key.pem ]; then
    echo "Error: kalshi_private_key.pem not found"
    exit 1
fi

echo "Starting hotkey generator..."

# Run the generator
exec venv/bin/python -m src.kalshi.tools.generate_hotkeys "$@"
