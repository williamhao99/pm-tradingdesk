#!/bin/bash
# Clean temporary files (logs, cache) - preserves state

cd "$(dirname "$0")/.."

echo "Cleaning temporary files..."

# Remove log files
find . -maxdepth 1 -name "*.log" -type f -delete 2>/dev/null
find . -maxdepth 1 -name "*.log.[0-9]*" -type f -delete 2>/dev/null

# Remove JSONL trade logs
find . -maxdepth 1 -name "*.jsonl" -type f -delete 2>/dev/null
find . -maxdepth 1 -name "*.jsonl.[0-9]*" -type f -delete 2>/dev/null

# Remove Python cache
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -type f -delete 2>/dev/null
find . -name "*.pyo" -type f -delete 2>/dev/null

echo "Done. State files preserved."
