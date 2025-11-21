#!/bin/bash
# Clean dependency reinstall

echo "Cleaning old dependencies and cache..."

# Remove virtual environment
rm -rf venv

# Remove Python cache
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -type f -name "*.pyc" -delete
find . -type f -name "*.pyo" -delete

# Remove pytest cache
rm -rf .pytest_cache

echo "Creating fresh virtual environment..."
python3 -m venv venv

echo "Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "Clean install complete!"
echo ""
echo "To activate virtual environment:"
echo "  source venv/bin/activate"
