#!/bin/bash
# Clean temporary files (logs, cache) - preserves state

cd "$(dirname "$0")/.."

echo "Cleaning temporary files..."

# Remove log files from data/ folder
rm -f data/*.log data/*.log.[0-9]* 2>/dev/null

# Remove JSONL trade logs from data/ folder
rm -f data/*.jsonl data/*.jsonl.[0-9]* 2>/dev/null

# Remove Python cache
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -type f -delete 2>/dev/null
find . -name "*.pyo" -type f -delete 2>/dev/null

echo "Done. State files in data/ preserved."
