#!/bin/bash
# Clean ALL temporary files including state (nuclear option)

cd "$(dirname "$0")/.."

echo "WARNING: This will remove tracked positions, net positions, and all logs."
echo "The bot will reconstruct positions from historical trades on next startup."
echo "Press Ctrl+C to cancel, or Enter to continue..."
read -r

echo "Cleaning all temporary files..."

# Remove entire data/ folder contents
rm -rf data/* 2>/dev/null

# Recreate data/ folder
mkdir -p data

# Remove Python cache
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -type f -delete 2>/dev/null
find . -name "*.pyo" -type f -delete 2>/dev/null

echo "Done. All state cleared. Positions will be reconstructed from API on next startup."
