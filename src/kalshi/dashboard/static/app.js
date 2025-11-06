/**
 * Kalshi Trading Dashboard Client
 * WebSocket-based real-time trading interface with compression support
 */

// Configuration
const CONFIG = {
  RECONNECT_MAX_ATTEMPTS: 10,
  RECONNECT_INITIAL_DELAY: 1000,
  RECONNECT_MAX_DELAY: 30000,
  RECONNECT_BACKOFF_MULTIPLIER: 1.5,
  CACHE_DEFAULT_TTL: 5000,
  CACHE_ORDERBOOK_TTL: 2000,
  FILLS_REFRESH_INTERVAL: 3000,
  METRICS_REPORT_INTERVAL: 30000,
  TOAST_DURATION: 2000,
  TOAST_FADEOUT_DURATION: 300,
  MESSAGE_AUTODISMISS_DELAY: 5000,
  MAX_TITLE_LENGTH: 85,
  MAX_POSITION_TITLE_LENGTH: 150,
  MAX_ORDERBOOK_TITLE_LENGTH: 100,
  DEFAULT_FILLS_LIMIT: 20,
};

/**
 * WebSocket client for Kalshi Trading Dashboard
 * Manages connection state, message handling, compression, and UI updates
 */
class KalshiTradingClient {
  constructor() {
    this.ws = null;
    this.reconnectAttempts = 0;
    this.reconnectDelay = CONFIG.RECONNECT_INITIAL_DELAY;
    this.isConnecting = false;
    this.compressionEnabled = true;
    this.selectedTicker = null;
    this.lastUpdate = {};
    this.metrics = {
      messageCount: 0,
      compressedCount: 0,
      lastLatency: 0,
      avgLatency: 0,
      connectionTime: null,
      bytesSaved: 0,
    };
    this.requestQueue = new Map();
    this.batchTimeout = null;
    this.cache = new Map();
    this.cacheExpiry = CONFIG.CACHE_DEFAULT_TTL;
    this.debounceTimers = new Map();
    this.fillsRefreshInterval = null;
    this.metricsReportInterval = null;
    this.init();
  }

  /**
   * Initialize client and setup connections
   */
  init() {
    this.connect();
    this.setupEventListeners();
    this.startMetricsReporting();
    this.startAutoRefresh();
    this.displayOrderbook(null);
  }

  /**
   * Start automatic refresh of fills data
   */
  startAutoRefresh() {
    this.fillsRefreshInterval = setInterval(() => {
      this.loadFills();
    }, CONFIG.FILLS_REFRESH_INTERVAL);
  }

  /**
   * Stop automatic refresh of fills data
   */
  stopAutoRefresh() {
    if (this.fillsRefreshInterval) {
      clearInterval(this.fillsRefreshInterval);
      this.fillsRefreshInterval = null;
    }
  }

  /**
   * Start periodic metrics reporting
   */
  startMetricsReporting() {
    this.metricsReportInterval = setInterval(() => {
      this.send("get_metrics");
    }, CONFIG.METRICS_REPORT_INTERVAL);
  }

  /**
   * Establish WebSocket connection to server
   */
  connect() {
    if (this.isConnecting) return;
    this.isConnecting = true;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    this.ws.binaryType = "arraybuffer";

    this.ws.onopen = () => this.handleOpen();
    this.ws.onmessage = (event) => this.handleIncomingMessage(event);
    this.ws.onclose = () => this.handleClose();
    this.ws.onerror = (error) => this.handleError(error);
  }

  /**
   * Handle WebSocket connection opened
   */
  handleOpen() {
    console.log("Connected to Kalshi Trading Dashboard (Compression Enabled)");
    this.isConnecting = false;
    this.reconnectAttempts = 0;
    this.reconnectDelay = CONFIG.RECONNECT_INITIAL_DELAY;
    this.metrics.connectionTime = Date.now();
    document.getElementById("status").classList.add("connected");
    this.loadInitialData();
  }

  /**
   * Handle incoming WebSocket message with compression support
   * @param {MessageEvent} event - WebSocket message event
   */
  handleIncomingMessage(event) {
    const startTime = performance.now();
    try {
      const data = this.parseMessage(event.data);
      this.handleMessage(data);
      const latency = performance.now() - startTime;
      this.updateMetrics(latency);
    } catch (error) {
      console.error("Failed to parse message:", error);
    }
  }

  /**
   * Parse and decompress incoming message
   * @param {ArrayBuffer|string} data - Message data (compressed or plain JSON)
   * @returns {Object} Parsed JSON message
   */
  parseMessage(data) {
    if (data instanceof ArrayBuffer) {
      const view = new Uint8Array(data);
      if (view[0] === 0x01) {
        const compressed = view.slice(1);
        const decompressed = pako.inflate(compressed, { to: "string" });
        this.metrics.compressedCount++;
        this.metrics.bytesSaved += decompressed.length - compressed.length;
        return JSON.parse(decompressed);
      } else {
        const decoder = new TextDecoder();
        return JSON.parse(decoder.decode(data));
      }
    }
    return JSON.parse(data);
  }

  /**
   * Handle WebSocket connection closed with automatic reconnection
   */
  handleClose() {
    console.log("Disconnected from server");
    this.isConnecting = false;
    document.getElementById("status").classList.remove("connected");
    if (this.reconnectAttempts < CONFIG.RECONNECT_MAX_ATTEMPTS) {
      this.reconnectAttempts++;
      this.reconnectDelay = Math.min(
        this.reconnectDelay * CONFIG.RECONNECT_BACKOFF_MULTIPLIER,
        CONFIG.RECONNECT_MAX_DELAY,
      );
      console.log(`Reconnecting in ${this.reconnectDelay}ms...`);
      setTimeout(() => this.connect(), this.reconnectDelay);
    }
  }

  /**
   * Handle WebSocket error
   * @param {Event} error - WebSocket error event
   */
  handleError(error) {
    console.error("WebSocket error:", error);
    this.isConnecting = false;
  }

  /**
   * Route incoming message to appropriate handler based on type
   * @param {Object} data - Parsed message data
   */
  handleMessage(data) {
    if (data.type && data.type !== "error") {
      this.cache.set(data.type, { data, timestamp: Date.now() });
    }
    const handlers = {
      connection: () => console.log(data.message),
      balance: () => this.displayBalance(data.balance),
      positions: () => this.displayPositions(data.positions),
      orders: () => this.displayOrders(data.orders),
      fills: () => this.displayFills(data.fills),
      ticker_lookup: () => this.handleTickerLookup(data),
      orderbook: () => this.displayOrderbook(data),
      market_update: () => this.handleMarketUpdate(data),
      order_placed: () => {
        if (data.success) {
          this.showOrderPlacedMessage(data.order_id);
          this.loadDashboardData();
        } else {
          this.showMessage(
            "error",
            `Order failed: ${data.error || "Unknown error"}`,
          );
        }
      },
      order_cancelled: () => {
        if (data.success) {
          this.showToast("Order cancelled successfully");
          this.loadDashboardData();
        } else {
          this.showMessage(
            "error",
            `Cancel failed: ${data.error || "Unknown error"}`,
          );
        }
      },
      hotkeys: () => this.displayBotHotkeys(data.hotkeys),
      hotkey_executed: () => {
        if (data.success) {
          this.showToast(`Hotkey "${data.keyword}" executed`);
          this.loadDashboardData();
        } else {
          this.showMessage(
            "error",
            `Hotkey failed: ${data.error || "Unknown error"}`,
          );
        }
      },
      bot_status: () => {
        this.updateBotStatus(data);
        if (data.message) {
          this.showToast(data.message);
        }
      },
      bot_hotkey_executed: () => {
        if (data.success) {
          this.showToast(`Bot executed: ${data.keyword}`);
        } else {
          this.showMessage(
            "error",
            `Bot execution failed: ${data.error || "Unknown error"}`,
          );
        }
      },
      hotkey_generation_status: () => {
        const statusDiv = document.getElementById("generator-status");
        if (statusDiv) {
          statusDiv.textContent = data.message;
        }
      },
      hotkey_generation_result: () => {
        const statusDiv = document.getElementById("generator-status");
        const generateBtn = document.querySelector(
          'button[onclick*="generateHotkeys"]',
        );

        // Re-enable button
        if (generateBtn) {
          generateBtn.disabled = false;
          generateBtn.textContent = "Generate Hotkeys";
        }

        if (data.success) {
          if (statusDiv) {
            statusDiv.textContent = `[OK] ${data.message}`;
          }
          this.showToast(data.message);
          if (data.bot_was_stopped) {
            this.showToast(
              "[WARNING] Bot was stopped to reload config. Click Activate to restart.",
            );
          }
        } else {
          if (statusDiv) {
            statusDiv.textContent = `[FAILED] ${data.error}`;
          }
        }
      },
      metrics: () => this.displayMetrics(data),
      error: () => this.showMessage("error", data.message),
    };

    const handler = handlers[data.type];
    if (handler) handler();
  }

  /**
   * Send message to server via WebSocket
   * @param {string} action - Action type
   * @param {Object} data - Message data
   * @param {boolean} priority - Queue message if connection lost
   */
  send(action, data = {}, priority = false) {
    const message = { action, ...data };
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
      this.metrics.messageCount++;
    } else if (priority) {
      this.requestQueue.set(action, message);
      this.showMessage("error", "Connection lost. Reconnecting...");
    }
  }

  /**
   * Send multiple messages in batch
   * @param {Array} requests - Array of {action, data} objects
   */
  batchSend(requests) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      requests.forEach((req) => this.send(req.action, req.data));
    }
  }

  /** Setup all event listeners */
  setupEventListeners() {
    this.setupTickerInputListener();
    this.setupKeyboardShortcuts();
  }

  /** Setup ticker input field event listeners */
  setupTickerInputListener() {
    const tickerInput = document.getElementById("market-ticker");
    if (!tickerInput) return;
    tickerInput.addEventListener("keypress", (e) => {
      if (e.key === "Enter") {
        const ticker = e.target.value.trim().toUpperCase();
        if (ticker) this.lookupTicker(ticker);
      }
    });
    tickerInput.addEventListener("input", (e) => {
      e.target.value = e.target.value.toUpperCase();
      this.selectedTicker = null;
      const statusElement = document.getElementById("ticker-lookup-status");
      if (statusElement) statusElement.textContent = "";
      this.displayOrderbook(null);
    });
  }

  /** Setup keyboard shortcuts (Ctrl+R, Ctrl+O, Escape) */
  setupKeyboardShortcuts() {
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.key === "r") {
        e.preventDefault();
        this.loadPositions();
      }
      if (e.ctrlKey && e.key === "o") {
        e.preventDefault();
        document.getElementById("market-search")?.focus();
      }
      if (e.key === "Escape") {
        const searchResults = document.getElementById("search-results");
        if (searchResults) {
          searchResults.innerHTML = "";
          searchResults.style.display = "none";
        }
      }
    });
  }

  /** Load initial dashboard data on connection */
  loadInitialData() {
    this.batchSend([
      { action: "get_balance" },
      { action: "get_positions" },
      { action: "get_orders", data: { status: "resting" } },
      { action: "get_fills", data: { limit: CONFIG.DEFAULT_FILLS_LIMIT } },
    ]);
  }

  /** Refresh all dashboard data */
  loadDashboardData() {
    this.send("get_balance");
    this.send("get_positions");
    this.send("get_orders", { status: "resting" });
  }

  /** Load positions from server */
  loadPositions() {
    this.send("get_positions");
  }

  /**
   * Load orders with status filter
   * @param {string} status - Order status (resting or executed)
   */
  loadOrders(status = "resting") {
    const restingBtn = document.getElementById("orders-btn-resting");
    const executedBtn = document.getElementById("orders-btn-executed");
    if (restingBtn && executedBtn) {
      if (status === "resting") {
        restingBtn.classList.remove("secondary");
        executedBtn.classList.add("secondary");
      } else {
        restingBtn.classList.add("secondary");
        executedBtn.classList.remove("secondary");
      }
    }
    this.send("get_orders", { status });
  }

  /** Load fills history */
  loadFills() {
    this.send("get_fills", { limit: CONFIG.DEFAULT_FILLS_LIMIT });
  }

  /** Toggle hotkey bot on/off */
  toggleHotkeyBot() {
    const btn = document.getElementById("bot-toggle-btn");
    const isRunning = btn.textContent.trim() === "Deactivate";
    if (isRunning) {
      this.send("stop_hotkey_bot");
    } else {
      this.send("start_hotkey_bot");
    }
  }

  /**
   * Execute bot hotkey by keyword
   * @param {string} keyword - Hotkey keyword to execute
   */
  executeBotHotkey(keyword) {
    this.send("bot_execute_hotkey", { keyword });
  }

  /** Generate hotkeys from series ticker */
  generateHotkeys() {
    const seriesInput = document.getElementById("series-ticker-input");
    const shareInput = document.getElementById("share-count-input");
    const statusDiv = document.getElementById("generator-status");
    const generateBtn = document.querySelector(
      'button[onclick*="generateHotkeys"]',
    );

    const seriesTicker = seriesInput.value.trim().toUpperCase();
    const shareCount = parseInt(shareInput.value) || 200;

    if (!seriesTicker) {
      statusDiv.textContent = "[WARNING] Please enter a series ticker";
      return;
    }
    if (generateBtn) {
      generateBtn.disabled = true;
      generateBtn.textContent = "Generating...";
    }
    statusDiv.textContent = "Starting...";
    this.send("generate_hotkeys", {
      series_ticker: seriesTicker,
      share_count: shareCount,
    });
  }

  /**
   * Update bot status UI
   * @param {Object} data - Bot status data
   */
  updateBotStatus(data) {
    const statusText = document.getElementById("bot-status-text");
    const marketTitle = document.getElementById("bot-market-title");
    const statsText = document.getElementById("bot-stats");
    const toggleBtn = document.getElementById("bot-toggle-btn");

    if (data.is_running) {
      statusText.textContent = "Active";
      statusText.style.color = "#10b981";
      toggleBtn.textContent = "Deactivate";
      toggleBtn.classList.remove("secondary");
      toggleBtn.classList.add("danger");
      if (marketTitle) {
        marketTitle.textContent = `Market Series: ${data.market_series_ticker || "Unknown"}`;
      }
      const uptime = data.uptime_seconds
        ? `${Math.floor(data.uptime_seconds)}s`
        : "0s";
      statsText.textContent = `${data.trades || 0} trades | Uptime: ${uptime}`;
      this.loadBotHotkeys();
    } else {
      statusText.textContent = "Offline";
      statusText.style.color = "var(--text-muted)";
      toggleBtn.textContent = "Activate";
      toggleBtn.classList.add("secondary");
      toggleBtn.classList.remove("danger");
      if (marketTitle) {
        marketTitle.textContent = "";
      }
      statsText.textContent = "";
      const container = document.getElementById("bot-hotkeys-grid");
      if (container) {
        container.innerHTML =
          '<div class="empty-state">Activate bot to use hotkeys</div>';
      }
    }
  }

  /** Load bot hotkeys configuration */
  loadBotHotkeys() {
    this.send("get_hotkeys");
  }

  /**
   * Display bot hotkeys in UI grid
   * @param {Object} hotkeys - Hotkeys configuration
   */
  displayBotHotkeys(hotkeys) {
    const container = document.getElementById("bot-hotkeys-grid");
    if (!container) return;

    if (!hotkeys || Object.keys(hotkeys).length === 0) {
      container.innerHTML =
        '<div class="empty-state">No hotkeys configured</div>';
      return;
    }

    const fragment = document.createDocumentFragment();

    const toTitleCase = (str) => {
      return str
        .split(" ")
        .map(
          (word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase(),
        )
        .join(" ");
    };

    Object.entries(hotkeys).forEach(([keyword, config]) => {
      const button = document.createElement("button");
      button.className = "hotkey-button";
      button.textContent = toTitleCase(keyword);
      button.title = `${config.action.toUpperCase()} ${config.count} ${config.side.toUpperCase()} @ ${config.ticker}`;

      button.addEventListener("click", () => {
        this.executeBotHotkey(keyword);
      });

      fragment.appendChild(button);
    });

    requestAnimationFrame(() => {
      container.innerHTML = "";
      container.appendChild(fragment);
    });
  }

  /**
   * Look up ticker and load orderbook
   * @param {string} ticker - Market ticker
   */
  lookupTicker(ticker) {
    if (!ticker) return;
    this.send("lookup_ticker", { ticker });
  }

  /**
   * Handle ticker lookup response
   * @param {Object} data - Lookup result data
   */
  handleTickerLookup(data) {
    const statusElement = document.getElementById("ticker-lookup-status");
    if (data.market && data.market.ticker) {
      this.selectedTicker = data.ticker;
      if (statusElement) {
        statusElement.textContent = "[OK] Market found";
        statusElement.style.color = "#10b981";
      }
      this.send("get_orderbook", { ticker: data.ticker });
    } else {
      this.selectedTicker = null;
      if (statusElement) {
        statusElement.textContent = "[FAILED] Invalid ticker - no market found";
        statusElement.style.color = "#ef4444";
      }
      this.displayOrderbook(null);
    }
  }

  /**
   * Handle market data update
   * @param {Object} data - Market update data
   */
  handleMarketUpdate(data) {
    if (data.ticker === this.selectedTicker) {
      const market = data.data?.market;
      if (market) {
        const yesPrice = market.last_price;
        const noPrice = yesPrice !== null ? 100 - yesPrice : null;

        this.displayOrderbook({
          ticker: data.ticker,
          title: market.title,
          yes_price: yesPrice,
          no_price: noPrice,
        });
      }
    }
  }

  /**
   * Place quick market order
   * @param {string} action - buy or sell
   * @param {string} side - yes or no
   * @param {number} count - Number of contracts
   */
  quickMarketOrder(action, side, count) {
    if (!this.selectedTicker) {
      this.showMessage("error", "Please select a market first");
      return;
    }

    const orderData = {
      ticker: this.selectedTicker,
      order_action: action,
      side: side,
      count: count,
      order_type: "market",
    };

    this.send("place_order", orderData, true);
  }

  /** Place limit order from form inputs */
  placeLimitOrder() {
    if (!this.selectedTicker) {
      this.showMessage("error", "Please select a market first");
      return;
    }
    const action = this.getActiveToggleValue("data-action", "buy");
    const side = this.getActiveToggleValue("data-side", "yes");
    const count = parseInt(document.getElementById("limit-count")?.value);
    const price = parseInt(document.getElementById("limit-price")?.value);
    if (!count || count <= 0) {
      this.showMessage("error", "Count must be greater than 0");
      return;
    }
    if (price < 1 || price > 99) {
      this.showMessage("error", "Price must be between 1 and 99");
      return;
    }
    const orderData = {
      ticker: this.selectedTicker,
      order_action: action,
      side: side,
      count: count,
      order_type: "limit",
    };
    if (side === "yes") {
      orderData.yes_price = price;
    } else {
      orderData.no_price = price;
    }
    this.send("place_order", orderData, true);
  }

  /**
   * Cancel order by ID
   * @param {string} orderId - Order ID to cancel
   */
  cancelOrder(orderId) {
    this.send("cancel_order", { order_id: orderId }, true);
  }

  /**
   * Toggle buy/sell action
   * @param {string} action - buy or sell
   */
  toggleAction(action) {
    this.setActiveToggle("data-action", action);
    this.updateLimitOrderButtonText();
  }

  /**
   * Toggle yes/no side
   * @param {string} side - yes or no
   */
  toggleSide(side) {
    this.setActiveToggle("data-side", side);
    this.updateLimitPrice();
    this.updateLimitOrderButtonText();
  }

  /**
   * Set active toggle button
   * @param {string} attribute - Toggle attribute name
   * @param {string} value - Toggle value
   */
  setActiveToggle(attribute, value) {
    document
      .querySelectorAll(`.btn-market[${attribute}]`)
      .forEach((btn) => btn.classList.remove("active"));
    document
      .querySelector(`.btn-market[${attribute}="${value}"]`)
      ?.classList.add("active");
  }

  /**
   * Get active toggle value
   * @param {string} attribute - Toggle attribute name
   * @param {string} defaultValue - Default if none active
   * @returns {string} Active toggle value
   */
  getActiveToggleValue(attribute, defaultValue) {
    const button = document.querySelector(`.btn-market[${attribute}].active`);
    if (!button) return defaultValue;
    const dataKey = attribute.replace("data-", "");
    const value = button.getAttribute(attribute);
    return value || defaultValue;
  }

  /** Update limit price from orderbook */
  updateLimitPrice() {
    const side = this.getActiveToggleValue("data-side", "yes");
    const cachedOrderbook = this.cache.get("orderbook");

    if (cachedOrderbook && cachedOrderbook.data) {
      const data = cachedOrderbook.data;
      const price = side === "yes" ? data.yes_price : data.no_price;

      if (price !== null && price !== undefined) {
        const priceInput = document.getElementById("limit-price");
        if (priceInput) {
          priceInput.placeholder = `${price} (last traded)`;
        }
      }
    }

    this.updateLimitOrderButtonText();
  }

  /** Update limit order button text based on form state */
  updateLimitOrderButtonText() {
    const button = document.getElementById("place-limit-order-btn");
    if (!button) return;
    const action = this.getActiveToggleValue("data-action", "buy");
    const side = this.getActiveToggleValue("data-side", "yes");
    const countInput = document.getElementById("limit-count");
    const priceInput = document.getElementById("limit-price");
    const count = countInput?.value ? parseInt(countInput.value) : null;
    const price = priceInput?.value ? parseInt(priceInput.value) : null;
    if (!action || !side) {
      button.textContent = "Select action and side";
      button.style.opacity = "0.4";
      button.style.pointerEvents = "none";
      button.style.cursor = "not-allowed";
      return;
    }
    if (!count || count <= 0) {
      button.textContent = "Enter count";
      button.style.opacity = "0.4";
      button.style.pointerEvents = "none";
      button.style.cursor = "not-allowed";
      return;
    }
    if (!price || price < 1 || price > 99) {
      button.textContent = "Enter price (1-99¢)";
      button.style.opacity = "0.4";
      button.style.pointerEvents = "none";
      button.style.cursor = "not-allowed";
      return;
    }
    const actionText = action.charAt(0).toUpperCase() + action.slice(1);
    const sideText = side.toUpperCase();
    button.textContent = `${actionText} ${count} ${sideText} @ ${price}¢`;
    button.style.opacity = "1";
    button.style.pointerEvents = "auto";
    button.style.cursor = "pointer";
  }

  /**
   * Display balance in UI
   * @param {number} cents - Balance in cents
   */
  displayBalance(cents) {
    this.updateElement("balance", this.formatCurrency(cents));
    const positionsValue = this.parseValue("positions-value");
    const portfolioValue = positionsValue + cents;
    this.updateElement("portfolio-value", this.formatCurrency(portfolioValue));
  }

  /**
   * Display positions in UI
   * @param {Array} positions - Array of position objects
   */
  displayPositions(positions) {
    const container = document.getElementById("positions-list");
    if (!container) return;

    if (positions.length === 0) {
      requestAnimationFrame(() => {
        container.innerHTML =
          '<div class="empty-state">No active positions</div>';
      });
      this.updateHeaderValues(0);
      return;
    }

    const fragment = document.createDocumentFragment();
    let totalValue = 0;

    positions.forEach((pos) => {
      const position = pos.position || 0;
      if (position === 0) return;

      const { element, value } = this.createPositionElement(pos, position);
      totalValue += value;
      fragment.appendChild(element);
    });

    requestAnimationFrame(() => {
      container.innerHTML = "";
      container.appendChild(fragment);
    });

    this.updateHeaderValues(totalValue);
  }

  /**
   * Create position list item element
   * @param {Object} pos - Position data
   * @param {number} position - Position size
   * @returns {Object} Element and current value
   */
  createPositionElement(pos, position) {
    const side = position > 0 ? "YES" : "NO";
    const contracts = Math.abs(position);
    const effectivePrice = side === "YES" ? pos.yes_price : pos.no_price;

    if (effectivePrice === null || effectivePrice === undefined) {
      return { element: document.createElement("div"), value: 0 };
    }

    const currentValue = contracts * effectivePrice;
    const realizedPnl = pos.realized_pnl || 0;

    const div = document.createElement("div");
    div.className = "list-item";
    div.addEventListener("click", (e) =>
      this.handleListItemClick(e, pos.ticker),
    );

    div.innerHTML = this.buildPositionHTML(
      pos,
      side,
      contracts,
      effectivePrice,
      currentValue,
      realizedPnl,
    );

    return { element: div, value: currentValue };
  }

  /**
   * Build position HTML
   * @param {Object} pos - Position data
   * @param {string} side - YES or NO
   * @param {number} contracts - Number of contracts
   * @param {number} price - Effective price
   * @param {number} value - Current value
   * @param {number} pnl - Realized PnL
   * @returns {string} HTML string
   */
  buildPositionHTML(pos, side, contracts, price, value, pnl) {
    const pnlColor = pnl >= 0 ? "#10b981" : "#ef4444";
    const pnlSign = pnl >= 0 ? "+" : "";
    const sideColor = side === "YES" ? "#408fff" : "#d24dff";
    const subtitle = side === "YES" ? pos.yes_sub_title : pos.no_sub_title;
    const marketTitle = pos.title || pos.ticker;
    const displayTitle = subtitle
      ? `${subtitle} - ${marketTitle}`
      : marketTitle;
    const title = this.truncate(displayTitle, CONFIG.MAX_POSITION_TITLE_LENGTH);

    return `
      <div class="list-item-header">
        <div class="list-item-title">${title}</div>
      </div>
      <div class="list-item-details" style="font-size: 15px; font-weight: 600; margin-top: 4px;">
        <span style="color: var(--text-primary);">
          ${contracts} <span style="color: ${sideColor}; font-weight: 700;">${side}</span> @ ${price}¢
          <span style="color: var(--text-muted); font-weight: 500; margin-left: 8px;">
            Current Value: ${this.formatCurrency(value)}
          </span>
        </span>
        <span style="color: ${pnlColor}; font-weight: 700;">
          PnL: ${pnlSign}${this.formatCurrency(pnl)}
        </span>
      </div>
    `;
  }

  /**
   * Display orders in UI
   * @param {Array} orders - Array of order objects
   */
  displayOrders(orders) {
    const container = document.getElementById("orders-list");
    if (!container) return;

    if (orders.length === 0) {
      requestAnimationFrame(() => {
        container.innerHTML = '<div class="empty-state">No orders found</div>';
      });
      return;
    }

    const fragment = document.createDocumentFragment();
    orders.forEach((order) => {
      fragment.appendChild(this.createOrderElement(order));
    });

    requestAnimationFrame(() => {
      container.innerHTML = "";
      container.appendChild(fragment);
    });
  }

  /**
   * Create order list item element
   * @param {Object} order - Order data
   * @returns {HTMLElement} Order element
   */
  createOrderElement(order) {
    const div = document.createElement("div");
    div.className = "list-item";
    div.addEventListener("click", (e) =>
      this.handleListItemClick(e, order.ticker),
    );

    const price = order.side === "yes" ? order.yes_price : order.no_price;
    const priceDisplay =
      price !== null && price !== undefined ? `${price}¢` : "N/A";
    const actionText = order.action === "buy" ? "Buying" : "Selling";
    const actionColor = order.action === "buy" ? "#10b981" : "#ef4444";
    const sideColor = order.side === "yes" ? "#408fff" : "#d24dff";
    const subtitle =
      order.side === "yes" ? order.yes_sub_title : order.no_sub_title;
    const marketTitle = order.title || order.ticker;
    const displayTitle = subtitle
      ? `${subtitle} - ${marketTitle}`
      : marketTitle;
    const title = this.truncate(displayTitle, CONFIG.MAX_TITLE_LENGTH);

    const cancelButton =
      order.status === "resting"
        ? `<button class="secondary cancel-order-btn" style="padding: 4px 8px; font-size: 11px;" onclick="client.cancelOrder('${order.order_id}')">Cancel</button>`
        : "";

    div.innerHTML = `
      <div class="list-item-header">
        <div class="list-item-title">${title}</div>
      </div>
      <div class="list-item-details">
        <span>
          <span style="color: ${actionColor}; font-weight: 600;">${actionText}</span>
          ${order.count || 0}
          <span style="color: ${sideColor}; font-weight: 600;">${order.side.toUpperCase()}</span>
          @ ${priceDisplay}
        </span>
        <span>${order.status}</span>
        ${cancelButton}
      </div>
    `;

    return div;
  }

  /**
   * Display fills in UI
   * @param {Array} fills - Array of fill objects
   */
  displayFills(fills) {
    const container = document.getElementById("fills-list");
    if (!container) return;

    if (fills.length === 0) {
      requestAnimationFrame(() => {
        container.innerHTML = '<div class="empty-state">No recent fills</div>';
      });
      return;
    }

    const fragment = document.createDocumentFragment();
    fills.forEach((fill) => {
      fragment.appendChild(this.createFillElement(fill));
    });

    requestAnimationFrame(() => {
      container.innerHTML = "";
      container.appendChild(fragment);
    });
  }

  /**
   * Create fill list item element
   * @param {Object} fill - Fill data
   * @returns {HTMLElement} Fill element
   */
  createFillElement(fill) {
    const div = document.createElement("div");
    div.className = "list-item";
    div.addEventListener("click", (e) =>
      this.handleListItemClick(e, fill.ticker),
    );

    const date = new Date(fill.created_time);
    const timeStr = this.formatTime(date);
    const actionText = fill.action === "buy" ? "Bought" : "Sold";
    const actionColor = fill.action === "buy" ? "#10b981" : "#ef4444";
    const sideColor = fill.side === "yes" ? "#408fff" : "#d24dff";
    const subtitle =
      fill.side === "yes" ? fill.yes_sub_title : fill.no_sub_title;
    const marketTitle = fill.title || fill.ticker;
    const displayTitle = subtitle
      ? `${subtitle} - ${marketTitle}`
      : marketTitle;
    const title = this.truncate(displayTitle, CONFIG.MAX_TITLE_LENGTH);

    div.innerHTML = `
      <div class="list-item-header">
        <div class="list-item-title">${title}</div>
      </div>
      <div class="list-item-details">
        <span>
          <span style="color: ${actionColor}; font-weight: 600;">${actionText}</span>
          ${fill.count}
          <span style="color: ${sideColor}; font-weight: 600;">${fill.side.toUpperCase()}</span>
          @ ${fill.price}¢
        </span>
        <span>${timeStr}</span>
        <span>${fill.is_taker ? "Taker" : "Maker"}</span>
      </div>
    `;

    return div;
  }

  /**
   * Display orderbook data
   * @param {Object|null} data - Orderbook data or null for empty state
   */
  displayOrderbook(data) {
    const container = document.getElementById("orderbook-info");
    if (!container) return;
    const marketOrdersSection = document.getElementById(
      "market-orders-section",
    );
    const limitOrdersSection = document.getElementById("limit-orders-section");
    if (!data) {
      const emptyHTML = this.buildOrderbookEmptyHTML();
      requestAnimationFrame(() => {
        container.innerHTML = emptyHTML;
        this.setTradingSectionsState(
          marketOrdersSection,
          limitOrdersSection,
          true,
        );
      });
      return;
    }
    this.cache.set("orderbook", { data, timestamp: Date.now() });
    const orderbookHTML = this.buildOrderbookHTML(data);
    requestAnimationFrame(() => {
      container.innerHTML = orderbookHTML;
      this.setTradingSectionsState(
        marketOrdersSection,
        limitOrdersSection,
        false,
      );
    });
    this.updateLimitPrice();
  }

  /**
   * Build empty orderbook HTML
   * @returns {string} HTML string
   */
  buildOrderbookEmptyHTML() {
    return `
      <div class="orderbook-display">
        <div class="orderbook-title">No market selected</div>
        <div class="orderbook-prices">
          <div class="price-yes">YES —</div>
          <div class="price-no">NO —</div>
        </div>
      </div>
    `;
  }

  /**
   * Build orderbook HTML
   * @param {Object} data - Orderbook data
   * @returns {string} HTML string
   */
  buildOrderbookHTML(data) {
    const yesPriceDisplay =
      data.yes_price !== null && data.yes_price !== undefined
        ? `${data.yes_price}¢`
        : "N/A";
    const noPriceDisplay =
      data.no_price !== null && data.no_price !== undefined
        ? `${data.no_price}¢`
        : "N/A";
    const title = this.truncate(data.title, CONFIG.MAX_ORDERBOOK_TITLE_LENGTH);

    return `
      <div class="orderbook-display">
        <div class="orderbook-title">${title}</div>
        <div class="orderbook-prices">
          <div class="price-yes">YES ${yesPriceDisplay}</div>
          <div class="price-no">NO ${noPriceDisplay}</div>
        </div>
      </div>
    `;
  }

  /**
   * Enable/disable trading sections
   * @param {HTMLElement} marketSection - Market section element
   * @param {HTMLElement} limitSection - Limit section element
   * @param {boolean} disabled - Whether to disable
   */
  setTradingSectionsState(marketSection, limitSection, disabled) {
    const opacity = disabled ? "0.4" : "1";
    const pointerEvents = disabled ? "none" : "auto";

    if (marketSection) {
      marketSection.style.opacity = opacity;
      marketSection.style.pointerEvents = pointerEvents;
    }
    if (limitSection) {
      limitSection.style.opacity = opacity;
      limitSection.style.pointerEvents = pointerEvents;
    }
  }

  /**
   * Show message in trade section
   * @param {string} type - Message type (success or error)
   * @param {string} message - Message text
   */
  showMessage(type, message) {
    const container = document.getElementById("trade-message");
    if (!container) return;

    const icon = type === "success" ? "[OK]" : "[X]";
    const color = type === "success" ? "#10b981" : "#ef4444";

    container.innerHTML = `
      <div style="font-size: 12px; margin-top: 8px; font-weight: 500; color: ${color};">
        ${icon} ${message}
      </div>
    `;

    setTimeout(() => {
      container.innerHTML = "";
    }, CONFIG.MESSAGE_AUTODISMISS_DELAY);
  }

  /**
   * Show order placed message
   * @param {string} orderId - Order ID
   */
  showOrderPlacedMessage(orderId) {
    const container = document.getElementById("trade-message");
    if (!container) return;

    const orderIdText = orderId ? ` - ${orderId}` : "";
    container.innerHTML = `
      <div style="font-size: 14px; margin-top: 8px; font-weight: 600; color: #10b981;">
        [OK] Order placed successfully${orderIdText}
      </div>
    `;

    setTimeout(() => {
      if (container.innerHTML.includes("Order placed successfully")) {
        container.innerHTML = "";
      }
    }, CONFIG.MESSAGE_AUTODISMISS_DELAY);
  }

  /**
   * Show toast notification
   * @param {string} message - Toast message
   */
  showToast(message) {
    const container = document.getElementById("toast-container");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = "toast";
    toast.innerHTML = `
      <div class="toast-icon">[OK]</div>
      <div>${message}</div>
    `;

    container.appendChild(toast);

    setTimeout(() => {
      toast.classList.add("exit");
      setTimeout(() => {
        container.removeChild(toast);
      }, CONFIG.TOAST_FADEOUT_DURATION);
    }, CONFIG.TOAST_DURATION);
  }

  /**
   * Display performance metrics
   * @param {Object} data - Metrics data
   */
  displayMetrics(data) {
    console.log("Performance Metrics:", {
      client: data.client,
      server: data.server,
      frontend: {
        messages: this.metrics.messageCount,
        compressed: this.metrics.compressedCount,
        avgLatency: this.metrics.avgLatency.toFixed(2) + "ms",
        bytesSaved: (this.metrics.bytesSaved / 1024).toFixed(2) + " KB",
        uptime: this.metrics.connectionTime
          ? ((Date.now() - this.metrics.connectionTime) / 1000).toFixed(0) + "s"
          : "N/A",
      },
    });
  }

  /**
   * Update element text content
   * @param {string} id - Element ID
   * @param {string} value - New value
   */
  updateElement(id, value) {
    const element = document.getElementById(id);
    if (element && element.textContent !== value) {
      requestAnimationFrame(() => {
        element.textContent = value;
      });
    }
  }

  /**
   * Update header values (positions and portfolio)
   * @param {number} positionsValueCents - Total positions value in cents
   */
  updateHeaderValues(positionsValueCents) {
    this.updateElement(
      "positions-value",
      this.formatCurrency(positionsValueCents),
    );

    const cashBalance = this.parseValue("balance");
    const portfolioValue = positionsValueCents + cashBalance;
    this.updateElement("portfolio-value", this.formatCurrency(portfolioValue));
  }

  /**
   * Parse currency value from element
   * @param {string} id - Element ID
   * @returns {number} Value in cents
   */
  parseValue(id) {
    const element = document.getElementById(id);
    if (!element) return 0;
    return parseFloat(element.textContent.replace(/[$,]/g, "")) * 100;
  }

  /**
   * Handle list item click (copy ticker)
   * @param {Event} event - Click event
   * @param {string} ticker - Ticker to copy
   */
  handleListItemClick(event, ticker) {
    if (event.target.tagName === "BUTTON") return;
    navigator.clipboard
      .writeText(ticker)
      .then(() => this.showToast("Copied to clipboard!"))
      .catch(() => this.showToast("Failed to copy"));
  }

  /**
   * Format cents as currency
   * @param {number} cents - Value in cents
   * @returns {string} Formatted currency string
   */
  formatCurrency(cents) {
    const dollars = cents / 100;
    return `$${dollars.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  /**
   * Truncate string to max length
   * @param {string} str - String to truncate
   * @param {number} maxLength - Maximum length
   * @returns {string} Truncated string
   */
  truncate(str, maxLength) {
    if (!str) return "";
    return str.length > maxLength
      ? str.substring(0, maxLength - 3) + "..."
      : str;
  }

  /**
   * Format date as time string
   * @param {Date|string} date - Date to format
   * @returns {string} Formatted time string
   */
  formatTime(date) {
    const options = {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: true,
    };
    return date.toLocaleString("en-US", options);
  }

  /**
   * Update performance metrics
   * @param {number} latency - Message latency in ms
   */
  updateMetrics(latency) {
    this.metrics.lastLatency = latency;
    this.metrics.avgLatency =
      (this.metrics.avgLatency * this.metrics.messageCount + latency) /
      (this.metrics.messageCount + 1);
    this.metrics.messageCount++;
  }

  /**
   * Debounce function execution
   * @param {string} key - Debounce key
   * @param {Function} func - Function to debounce
   * @param {number} delay - Delay in ms
   */
  debounce(key, func, delay) {
    if (this.debounceTimers.has(key)) {
      clearTimeout(this.debounceTimers.get(key));
    }
    this.debounceTimers.set(key, setTimeout(func, delay));
  }
}

const client = new KalshiTradingClient();

window.switchTab = (tabName) => {
  document
    .querySelectorAll(".tab-content")
    .forEach((tab) => tab.classList.remove("active"));
  document
    .querySelectorAll(".tab-btn")
    .forEach((btn) => btn.classList.remove("active"));
  document.getElementById(`${tabName}-tab`)?.classList.add("active");
  event.target.classList.add("active");
  if (tabName === "positions") {
    client.loadPositions();
  }
};

// Export for global access
window.quickMarketOrder = (action, side, count) =>
  client.quickMarketOrder(action, side, count);
window.placeLimitOrder = () => client.placeLimitOrder();
window.toggleAction = (action) => client.toggleAction(action);
window.toggleSide = (side) => client.toggleSide(side);
window.updateLimitOrderButton = () => client.updateLimitOrderButtonText();
window.loadOrders = (status) => client.loadOrders(status);
window.loadFills = () => client.loadFills();
window.loadPositions = () => client.loadPositions();
