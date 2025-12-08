#!/bin/bash
# Identical markets arbitrage monitor

cd "$(dirname "$0")/.." || exit 1

# --- CONFIG ---
TICKER_A=KXNCAAFUNDEFEATED-25-TXAM
TICKER_B=KXNCAAFGAME-25NOV28TXAMTEX-TXAM
RELATIONSHIP=same_yes  # same_yes or opposite
MODE=taker             # monitor or taker
QTY=10
MIN_EDGE=3.5
MAX_EXPOSURE=700
LIVE=true
# --------------

if [ -d "venv" ]; then
    source venv/bin/activate
fi

ARGS=(
    -a "$TICKER_A"
    -b "$TICKER_B"
    -r "$RELATIONSHIP"
    -m "$MODE"
    -q "$QTY"
    --min-edge "$MIN_EDGE"
    --max-exposure "$MAX_EXPOSURE"
)

if [ "$LIVE" = true ]; then
    ARGS+=(--live)
    echo "*** LIVE TRADING MODE ***"
else
    echo "Dry run mode (no real orders)"
fi

echo "Tickers: $TICKER_A / $TICKER_B"
echo "Relationship: $RELATIONSHIP | Mode: $MODE"
echo "Max exposure: \$$MAX_EXPOSURE"
echo ""

python3 -m src.kalshi.strategies.identical_markets "${ARGS[@]}"
