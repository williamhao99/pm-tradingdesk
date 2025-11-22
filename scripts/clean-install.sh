#!/bin/bash
# Clean dependency reinstall

echo "Cleaning old dependencies and cache..."

# Remove virtual environment
rm -rf venv

# Remove Python cache (recursively across entire workspace)
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -type f -name "*.pyc" -delete 2>/dev/null
find . -type f -name "*.pyo" -delete 2>/dev/null
find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null

# Remove pytest cache
rm -rf .pytest_cache

# Remove any build artifacts
rm -rf build/ dist/

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
