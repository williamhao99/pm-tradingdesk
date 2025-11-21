/**
 * Main - Application orchestration and business logic
 * Handles message routing, trading actions, and initialization
 */

import { WebSocketClient } from "./websocket-client.js";
import { UIController } from "./ui-controller.js";

/**
 * Main application controller
 */
class KalshiTradingClient {
  constructor() {
    this.wsClient = new WebSocketClient(
      (data) => this.handleMessage(data),
      () => this.ui.loadInitialData(),
    );

    this.ui = new UIController(this.wsClient);

    this.init();
  }

  /**
   * Initialize application
   */
  init() {
    this.wsClient.connect();
    this.setupEventListeners();
    this.ui.displayOrderbook(null);
  }

  /**
   * Setup event listeners
   */
  setupEventListeners() {
    this.setupTickerInputListener();
    this.setupKeyboardShortcuts();
  }

  /**
   * Setup ticker input field
   */
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
      this.ui.selectedTicker = null;
      const statusElement = document.getElementById("ticker-lookup-status");
      if (statusElement) statusElement.textContent = "";
      this.ui.displayOrderbook(null);
    });
  }

  /**
   * Setup keyboard shortcuts
   */
  setupKeyboardShortcuts() {
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.key === "r") {
        e.preventDefault();
        this.ui.loadPositions();
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

  /**
   * Handle incoming messages
   */
  handleMessage(data) {
    const handlers = {
      connection: () => console.log(data.message),
      balance: () => this.ui.displayBalance(data),
      positions: () => this.ui.displayPositions(data),
      orders: () => this.ui.displayOrders(data.orders),
      fills: () => this.ui.displayFills(data.fills),
      ticker_lookup: () => this.handleTickerLookup(data),
      orderbook: () => this.ui.displayOrderbook(data),
      market_update: () => this.handleMarketUpdate(data),
      orderbook_update: () => this.handleMarketUpdate(data),
      kalshi_ws_status: () => {
        if (data.connected) {
          console.log("Kalshi WebSocket reconnected");
        } else {
          console.log("Kalshi WebSocket disconnected - reconnecting...");
          this.ui.showToast("Live data reconnecting...");
        }
      },
      new_fill: () => {
        this.ui.loadFills();
      },
      order_placed: () => {
        if (data.success) {
          const orderIdText = data.order_id ? ` (${data.order_id})` : "";
          this.ui.showToast(`Order placed successfully${orderIdText}`);
          this.ui.loadDashboardData();
        } else {
          this.ui.showToast(`Order failed: ${data.error || "Unknown error"}`);
        }
      },
      order_cancelled: () => {
        if (data.success) {
          this.ui.showToast("Order cancelled successfully");
          this.ui.loadDashboardData();
        } else {
          this.ui.showToast(`Cancel failed: ${data.error || "Unknown error"}`);
        }
      },
      hotkeys: () => this.ui.displayBotHotkeys(data.hotkeys),
      hotkey_executed: () => {
        if (data.success) {
          this.ui.showToast(`Hotkey "${data.keyword}" executed`);
          this.ui.loadDashboardData();
        } else {
          this.ui.showToast(`Hotkey failed: ${data.error || "Unknown error"}`);
        }
      },
      bot_status: () => {
        this.ui.updateBotStatus(data);
        if (data.message) {
          this.ui.showToast(data.message);
        }
      },
      bot_hotkey_executed: () => {
        if (data.success) {
          this.ui.showToast(`Bot executed: ${data.keyword}`);
        } else {
          this.ui.showToast(
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

        if (generateBtn) {
          generateBtn.disabled = false;
          generateBtn.textContent = "Generate Hotkeys";
        }

        if (data.success) {
          if (statusDiv) {
            statusDiv.textContent = `[OK] ${data.message}`;
          }
          this.ui.showToast(data.message);
          if (data.bot_was_stopped) {
            this.ui.showToast(
              "[WARNING] Bot was stopped to reload config. Click Activate to restart.",
            );
          }
        } else {
          if (statusDiv) {
            statusDiv.textContent = `[FAILED] ${data.error}`;
          }
        }
      },
      analytics: () => this.ui.displayAnalytics(data.data),
      error: () => this.ui.showToast(data.message),
    };

    const handler = handlers[data.type];
    if (handler) handler();
  }

  /**
   * Handle ticker lookup
   */
  handleTickerLookup(data) {
    const statusElement = document.getElementById("ticker-lookup-status");

    if (data.market && data.market.ticker) {
      this.ui.selectedTicker = data.ticker;
      if (statusElement) {
        statusElement.textContent = "[OK] Market found";
        statusElement.style.color = "#10b981";
      }

      // Unsubscribe from previous ticker
      if (
        this.ui.currentSubscribedTicker &&
        this.ui.currentSubscribedTicker !== data.ticker
      ) {
        this.wsClient.send("unsubscribe_market", {
          ticker: this.ui.currentSubscribedTicker,
        });
      }

      this.ui.currentSubscribedTicker = data.ticker;
      this.wsClient.send("get_orderbook", { ticker: data.ticker });
    } else {
      this.ui.selectedTicker = null;
      if (statusElement) {
        statusElement.textContent = "[FAILED] Invalid ticker - no market found";
        statusElement.style.color = "#ef4444";
      }
      this.ui.displayOrderbook(null);
    }
  }

  /**
   * Handle market update (ticker/orderbook updates)
   */
  handleMarketUpdate(data) {
    const ticker = data.ticker;

    if (!ticker || !this.ui.currentOrderbookData) {
      return;
    }

    if (ticker !== this.ui.currentOrderbookData.ticker) {
      return;
    }

    // Handle different update types
    if (data.type === "market_update") {
      const market = data.data?.market;
      if (market) {
        this.ui.currentOrderbookData.yes_price =
          market.last_price || market.yes_price;
        this.ui.currentOrderbookData.no_price = market.no_price;
        this.ui.displayOrderbook(this.ui.currentOrderbookData);
      }
    } else if (data.type === "orderbook_update") {
      // Real-time WebSocket update
      const wsData = data.data || {};
      const msg = wsData.msg || wsData.type;

      // Handle ticker updates (top of book)
      if (msg === "ticker") {
        const yes_bid = wsData.yes_bid;
        const no_bid = wsData.no_bid;

        if (yes_bid !== undefined && no_bid !== undefined) {
          this.ui.currentOrderbookData.yes_price = yes_bid;
          this.ui.currentOrderbookData.no_price = no_bid;
          this.ui.displayOrderbook(this.ui.currentOrderbookData);
          this.ui.updateLimitPrice();
        }
      }
      // Handle orderbook delta updates (full depth)
      else if (msg === "orderbook_delta" || msg === "orderbook_update") {
        this.applyOrderbookDelta(wsData);
      }
      // Handle orderbook snapshot (full book reset)
      else if (msg === "orderbook_snapshot") {
        const yes_bids = wsData.yes || [];
        const no_bids = wsData.no || [];

        this.ui.currentOrderbookData.yes_bids = yes_bids;
        this.ui.currentOrderbookData.no_bids = no_bids;
        this.ui.displayOrderbook(this.ui.currentOrderbookData);
      }
    }
  }

  /**
   * Apply orderbook delta to current orderbook state
   * @param {Object} delta - Orderbook delta message
   */
  applyOrderbookDelta(delta) {
    if (!this.ui.currentOrderbookData) return;

    const side = delta.side?.toLowerCase(); // "yes" or "no"
    const price = delta.price;
    const deltaSize = delta.delta;

    if (!side || price === undefined || deltaSize === undefined) return;

    const bidsKey = side === "yes" ? "yes_bids" : "no_bids";
    let bids = this.ui.currentOrderbookData[bidsKey] || [];

    // Find existing price level
    const index = bids.findIndex(([p]) => p === price);

    if (deltaSize === 0 || (index >= 0 && bids[index][1] + deltaSize <= 0)) {
      // Remove price level if size becomes 0 or negative
      if (index >= 0) {
        bids.splice(index, 1);
      }
    } else if (index >= 0) {
      // Update existing price level
      bids[index][1] += deltaSize;
    } else if (deltaSize > 0) {
      // Add new price level and sort (descending for bids)
      bids.push([price, deltaSize]);
      bids.sort((a, b) => b[0] - a[0]);
    }

    this.ui.currentOrderbookData[bidsKey] = bids;
    this.ui.displayOrderbook(this.ui.currentOrderbookData);
  }

  /**
   * Look up ticker
   * @param {string} ticker - Market ticker to look up
   */
  lookupTicker(ticker) {
    if (!ticker) return;

    // Sanitize ticker: Kalshi tickers are uppercase alphanumeric + hyphens only
    const sanitized = ticker
      .trim()
      .toUpperCase()
      .replace(/[^A-Z0-9-]/g, "");

    if (!sanitized || sanitized.length > 100) {
      this.ui.showToast("Invalid ticker format");
      return;
    }

    this.wsClient.send("lookup_ticker", { ticker: sanitized });
  }

  /**
   * Place quick market order (converted to max aggressive limit order)
   * Note: Kalshi deprecated market orders Sept 2025 - we use max aggressive limit pricing instead
   * @param {string} action - "buy" or "sell"
   * @param {string} side - "yes" or "no"
   * @param {number} count - Number of contracts
   */
  quickMarketOrder(action, side, count) {
    if (!this.ui.selectedTicker) {
      this.ui.showToast("Please select a market first");
      return;
    }

    // Backend handles aggressive pricing (99¢ buy / 1¢ sell)
    this.wsClient.send("quick_order", {
      ticker: this.ui.selectedTicker,
      order_action: action,
      side: side,
      count: count,
    });
  }

  /**
   * Place limit order
   */
  placeLimitOrder() {
    if (!this.ui.selectedTicker) {
      this.ui.showToast("Please select a market first");
      return;
    }

    const action = this.ui.getActiveToggleValue("data-action", "buy");
    const side = this.ui.getActiveToggleValue("data-side", "yes");
    const count = parseInt(document.getElementById("limit-count")?.value);
    const price = parseInt(document.getElementById("limit-price")?.value);

    // Simple request - backend handles price field assignment
    this.wsClient.send("place_order", {
      ticker: this.ui.selectedTicker,
      order_action: action,
      side: side,
      count: count,
      price: price,
    });
  }

  /**
   * Cancel order
   * @param {string} orderId - Order ID to cancel
   */
  cancelOrder(orderId) {
    this.wsClient.send("cancel_order", { order_id: orderId });
  }

  /**
   * Toggle buy/sell action
   * @param {string} action - "buy" or "sell"
   */
  toggleAction(action) {
    this.ui.setActiveToggle("data-action", action);
    this.ui.updateLimitOrderButtonText();
  }

  /**
   * Toggle yes/no side
   * @param {string} side - "yes" or "no"
   */
  toggleSide(side) {
    this.ui.setActiveToggle("data-side", side);
    this.ui.updateLimitPrice();
    this.ui.updateLimitOrderButtonText();
  }

  /**
   * Toggle hotkey bot on/off
   */
  toggleHotkeyBot() {
    const btn = document.getElementById("bot-toggle-btn");
    const isRunning = btn.textContent.trim() === "Deactivate";
    if (isRunning) {
      this.wsClient.send("stop_hotkey_bot");
    } else {
      this.wsClient.send("start_hotkey_bot");
    }
  }

  /**
   * Execute bot hotkey
   * @param {string} keyword - Hotkey keyword to execute
   */
  executeBotHotkey(keyword) {
    this.wsClient.send("bot_execute_hotkey", { keyword });
  }

  /**
   * Take manual portfolio snapshot
   */
  takeSnapshot() {
    this.wsClient.send("take_snapshot");
    this.ui.showToast("Snapshot saved");
  }

  /**
   * Generate hotkeys from series ticker
   */
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
    this.wsClient.send("generate_hotkeys", {
      series_ticker: seriesTicker,
      share_count: shareCount,
    });
  }
}

// Initialize application
const client = new KalshiTradingClient();

// Export global functions for onclick handlers in HTML
window.client = client;
window.switchTab = (tabName, event = null) => {
  document
    .querySelectorAll(".tab-content")
    .forEach((tab) => tab.classList.remove("active"));
  document
    .querySelectorAll(".tab-btn")
    .forEach((btn) => btn.classList.remove("active"));
  document.getElementById(`${tabName}-tab`)?.classList.add("active");

  if (event?.target) {
    event.target.classList.add("active");
  }

  if (tabName === "positions") {
    client.ui.loadPositions();
  } else if (tabName === "analytics") {
    client.ui.loadAnalytics();
  }
};

window.quickMarketOrder = (action, side, count) =>
  client.quickMarketOrder(action, side, count);
window.placeLimitOrder = () => client.placeLimitOrder();
window.toggleAction = (action) => client.toggleAction(action);
window.toggleSide = (side) => client.toggleSide(side);
window.updateLimitOrderButton = () => client.ui.updateLimitOrderButtonText();
window.loadOrders = (status, event) => client.ui.loadOrders(status, event);
window.loadFills = (event) => client.ui.loadFills(event);
window.loadPositions = (event) => client.ui.loadPositions(event);
window.cancelOrder = (orderId) => client.cancelOrder(orderId);
window.executeBotHotkey = (keyword) => client.executeBotHotkey(keyword);
window.handleListItemClick = (event, ticker) => {
  if (event.target.tagName === "BUTTON") return;
  navigator.clipboard
    .writeText(ticker)
    .then(() => client.ui.showToast("Copied to clipboard!"))
    .catch(() => client.ui.showToast("Failed to copy"));
};
