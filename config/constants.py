"""Configuration constants"""

# API endpoints
PROD_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_API_URL = "https://demo.kalshi.com/trade-api/v2"

API_REQUEST_TIMEOUT = 30
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 10

# Market order pricing (aggressive fill strategy)
MARKET_BUY_MAX_PRICE = 99
MARKET_SELL_MIN_PRICE = 1

# Binary market constraints
MIN_PRICE = 1
MAX_PRICE = 99
PRICE_SUM = 100  # yes_price + no_price = 100

# Pagination limits
MAX_MARKETS_PER_PAGE = 100
MAX_POSITIONS_PER_PAGE = 1000
MAX_ORDERS_PER_PAGE = 100
MAX_FILLS_PER_PAGE = 100
DEFAULT_FILLS_LIMIT = 20
DEFAULT_SEARCH_RESULTS = 10

# Cache TTL (seconds)
MARKET_CACHE_TTL = 5.0
ORDERBOOK_CACHE_TTL = 2.0
DNS_CACHE_TTL = 300

# Connection pooling
MAX_TOTAL_CONNECTIONS = 100
MAX_CONNECTIONS_PER_HOST = 50
KEEPALIVE_TIMEOUT = 30

# Rate limiting (token bucket)
RATE_LIMIT_REQUESTS = 100
RATE_LIMIT_PERIOD = 1.0

# Parallel processing
MAX_PARALLEL_MARKET_FETCHES = 10

# WebSocket configuration
WS_COMPRESSION_ENABLED = True
WS_COMPRESSION_LEVEL = 1
WS_COMPRESSION_THRESHOLD = 0.9
MARKET_UPDATE_INTERVAL = 2.0
PERFORMANCE_WINDOW_SIZE = 1000
COMPRESSION_RATIO_WINDOW = 100

# Display formatting
MAX_TITLE_LENGTH = 70
MAX_TITLE_LENGTH_SHORT = 80
TRUNCATION_SUFFIX = "..."

# Error messages
ERROR_NO_API_KEY = "KALSHI_API_KEY_ID must be set in .env file"
ERROR_NO_PRIVATE_KEY = "Private key file not found"
ERROR_INVALID_PRICE = "Price must be between 1-99 cents"
ERROR_INVALID_ACTION = "Action must be 'buy' or 'sell'"
ERROR_INVALID_SIDE = "Side must be 'yes' or 'no'"
ERROR_INVALID_ORDER_TYPE = "Order type must be 'limit' or 'market'"
