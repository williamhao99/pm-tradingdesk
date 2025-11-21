"""Configuration constants."""

from pathlib import Path


# Project paths
PROJECT_ROOT = Path(__file__).parent.parent

# API endpoints
KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BASE_URL = "https://api.elections.kalshi.com"  # Without /trade-api/v2 suffix
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# API timeouts (seconds)
API_REQUEST_TIMEOUT = 10  # If Kalshi doesn't respond in 10s, something is wrong

# Retry configuration
RETRY_MAX_ATTEMPTS = 2  # Try once, retry once if 5xx error
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_BACKOFF_MULTIPLIER = 2

# Cache configuration
MARKET_CACHE_TTL = 300.0  # 5 minutes - market metadata rarely changes

# Pagination limits
MAX_POSITIONS_PER_PAGE = 1000
DEFAULT_FILLS_LIMIT = 20

# Error messages
ERROR_NO_API_KEY = "KALSHI_API_KEY_ID must be set in .env file"
ERROR_NO_PRIVATE_KEY = "Private key file not found"
