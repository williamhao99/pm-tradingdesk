#!/bin/bash
# Code Formatting (Black + Prettier)

cd "$(dirname "$0")/.."

# Check virtual environment
if [ ! -d "venv" ]; then
    echo "Error: venv not found. Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Format Python
echo "Formatting Python files..."
venv/bin/python3 -m black src/ config/ tests/

# Format JS/HTML
if command -v npx &> /dev/null; then
    echo "Formatting JavaScript and HTML..."
    npx --yes prettier --write "**/*.{js,html}" --ignore-path .prettierignore
    echo "Done!"
else
    echo "Warning: npx not found (skipping JS/HTML formatting)"
    echo "To format JS/HTML: Install Node.js, then run: npx prettier --write \"**/*.{js,html}\""
fi
