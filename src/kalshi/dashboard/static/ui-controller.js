/**
 * UI Controller - All UI rendering, display, and update logic
 * Handles DOM manipulation and user interactions
 */

import { formatCurrency, truncate, formatTime } from "./formatters.js";

const CONFIG = {
  TOAST_DURATION: 2000,
  TOAST_FADEOUT_DURATION: 300,
  MAX_TITLE_LENGTH: 85,
  MAX_POSITION_TITLE_LENGTH: 150,
  MAX_ORDERBOOK_TITLE_LENGTH: 100,
};

/**
 * UI Controller for dashboard rendering and interactions
 */
export class UIController {
  constructor(wsClient) {
    this.wsClient = wsClient;
    this.selectedTicker = null;
    this.currentOrderbookData = null;
    this.currentSubscribedTicker = null;
    this.portfolioChart = null;
  }

  /**
   * Load initial data on connection
   */
  loadInitialData() {
    this.wsClient.send("get_balance");
    this.wsClient.send("get_positions");
    this.wsClient.send("get_orders", { status: "resting" });
    this.wsClient.send("get_fills", { limit: 20 });
  }

  /**
   * Refresh dashboard data
   */
  loadDashboardData() {
    this.wsClient.send("get_balance");
    this.wsClient.send("get_positions");
    this.wsClient.send("get_orders", { status: "resting" });
  }

  /**
   * Load positions
   * @param {Event|null} event - Optional event object
   */
  loadPositions(event = null) {
    const btn = event?.target;
    if (
      btn &&
      btn.onclick &&
      btn.onclick.toString().includes("loadPositions")
    ) {
      btn.disabled = true;
      btn.textContent = "Loading...";
    }
    this.wsClient.send("get_positions");
  }

  /**
   * Load orders
   * @param {string} status - Order status ("resting" or "executed")
   * @param {Event|null} event - Optional event object
   */
  loadOrders(status = "resting", event = null) {
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

    const btn = event?.target;
    if (btn && btn.onclick && btn.onclick.toString().includes("loadOrders")) {
      btn.disabled = true;
      const originalText = btn.textContent;
      btn.textContent = "Loading...";
      btn.dataset.originalText = originalText;
    }

    this.wsClient.send("get_orders", { status });
  }

  /**
   * Load fills
   * @param {Event|null} event - Optional event object
   */
  loadFills(event = null) {
    const btn = event?.target;
    if (btn && btn.onclick && btn.onclick.toString().includes("loadFills")) {
      btn.disabled = true;
      btn.textContent = "Loading...";
    }
    this.wsClient.send("get_fills", { limit: 20 });
  }

  /**
   * Load analytics data
   */
  loadAnalytics() {
    this.wsClient.send("get_analytics");
  }

  /**
   * Display balance (backend sends all calculated values)
   * @param {Object} data - Balance data from backend
   */
  displayBalance(data) {
    const balance = document.getElementById("balance");
    const positionsValue = document.getElementById("positions-value");
    const portfolioValue = document.getElementById("portfolio-value");

    if (balance) balance.textContent = formatCurrency(data.cash_cents);
    if (positionsValue)
      positionsValue.textContent = formatCurrency(data.positions_value_cents);
    if (portfolioValue)
      portfolioValue.textContent = formatCurrency(data.portfolio_value_cents);
  }

  /**
   * Display positions (backend sends total value)
   * @param {Object} data - Positions data from backend
   */
  displayPositions(data) {
    const positions = data.positions;
    const totalValue = data.total_positions_value_cents;
    const container = document.getElementById("positions-list");
    const positionsValue = document.getElementById("positions-value");

    if (!container) return;

    // Re-enable refresh button
    const refreshBtns = document.querySelectorAll('[onclick*="loadPositions"]');
    refreshBtns.forEach((btn) => {
      btn.disabled = false;
      btn.textContent = "Refresh";
    });

    if (positions.length === 0) {
      container.innerHTML =
        '<div class="empty-state">No active positions</div>';
      if (positionsValue) positionsValue.textContent = formatCurrency(0);
      return;
    }

    // Backend calculates everything - frontend just renders
    const positionsHTML = positions
      .map((pos) => this.createPositionHTML(pos))
      .filter((html) => html !== "")
      .join("");

    container.innerHTML = positionsHTML;

    if (positionsValue) positionsValue.textContent = formatCurrency(totalValue);
  }

  /**
   * Create position HTML (backend sends all calculated fields)
   * @param {Object} pos - Position object
   * @returns {string} HTML string
   */
  createPositionHTML(pos) {
    // Backend sends: side, contracts, effective_price, current_value, realized_pnl
    const side = pos.side; // Already uppercase from backend
    const contracts = pos.contracts;
    const effectivePrice = pos.effective_price;
    const currentValue = pos.current_value || 0;
    const realizedPnl = pos.realized_pnl || 0;

    if (effectivePrice === null || effectivePrice === undefined) {
      return "";
    }

    const pnlColor = realizedPnl >= 0 ? "#10b981" : "#ef4444";
    const pnlSign = realizedPnl >= 0 ? "+" : "";
    const sideColor = side === "YES" ? "#408fff" : "#d24dff";
    const subtitle = side === "YES" ? pos.yes_sub_title : pos.no_sub_title;
    const marketTitle = pos.title || pos.ticker;
    const displayTitle = subtitle
      ? `${subtitle} - ${marketTitle}`
      : marketTitle;
    const title = truncate(displayTitle, CONFIG.MAX_POSITION_TITLE_LENGTH);

    return `
      <div class="list-item" onclick="window.handleListItemClick(event, '${pos.ticker}')">
        <div class="list-item-header">
          <div class="list-item-title">${title}</div>
        </div>
        <div class="list-item-details" style="font-size: 15px; font-weight: 600; margin-top: 4px;">
          <span style="color: var(--text-primary);">
            ${contracts} <span style="color: ${sideColor}; font-weight: 700;">${side}</span> @ ${effectivePrice}¢
            <span style="color: var(--text-muted); font-weight: 500; margin-left: 8px;">
              Current Value: ${formatCurrency(currentValue)}
            </span>
          </span>
          <span style="color: ${pnlColor}; font-weight: 700;">
            PnL: ${pnlSign}${formatCurrency(realizedPnl)}
          </span>
        </div>
      </div>
    `;
  }

  /**
   * Display orders
   * @param {Array} orders - Array of order objects
   */
  displayOrders(orders) {
    const container = document.getElementById("orders-list");
    if (!container) return;

    // Re-enable refresh button
    const refreshBtns = document.querySelectorAll('[onclick*="loadOrders"]');
    refreshBtns.forEach((btn) => {
      btn.disabled = false;
      btn.textContent = btn.dataset.originalText || "Refresh";
    });

    if (orders.length === 0) {
      container.innerHTML = '<div class="empty-state">No orders found</div>';
      return;
    }

    const ordersHTML = orders
      .map((order) => this.createOrderHTML(order))
      .join("");
    container.innerHTML = ordersHTML;
  }

  /**
   * Create order HTML
   * @param {Object} order - Order object
   * @returns {string} HTML string
   */
  createOrderHTML(order) {
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
    const title = truncate(displayTitle, CONFIG.MAX_TITLE_LENGTH);

    const cancelButton =
      order.status === "resting"
        ? `<button class="secondary cancel-order-btn" style="padding: 4px 8px; font-size: 11px;" onclick="event.stopPropagation(); window.cancelOrder('${order.order_id}')">Cancel</button>`
        : "";

    return `
      <div class="list-item" onclick="window.handleListItemClick(event, '${order.ticker}')">
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
      </div>
    `;
  }

  /**
   * Display fills
   * @param {Array} fills - Array of fill objects
   */
  displayFills(fills) {
    const container = document.getElementById("fills-list");
    if (!container) return;

    // Re-enable refresh button
    const refreshBtns = document.querySelectorAll('[onclick*="loadFills"]');
    refreshBtns.forEach((btn) => {
      btn.disabled = false;
      btn.textContent = "Refresh";
    });

    if (fills.length === 0) {
      container.innerHTML = '<div class="empty-state">No recent fills</div>';
      return;
    }

    const fillsHTML = fills.map((fill) => this.createFillHTML(fill)).join("");
    container.innerHTML = fillsHTML;
  }

  /**
   * Create fill HTML
   * @param {Object} fill - Fill object
   * @returns {string} HTML string
   */
  createFillHTML(fill) {
    const date = new Date(fill.created_time);
    const timeStr = formatTime(date);
    const actionText = fill.action === "buy" ? "Bought" : "Sold";
    const actionColor = fill.action === "buy" ? "#10b981" : "#ef4444";
    const sideColor = fill.side === "yes" ? "#408fff" : "#d24dff";
    const subtitle =
      fill.side === "yes" ? fill.yes_sub_title : fill.no_sub_title;
    const marketTitle = fill.title || fill.ticker;
    const displayTitle = subtitle
      ? `${subtitle} - ${marketTitle}`
      : marketTitle;
    const title = truncate(displayTitle, CONFIG.MAX_TITLE_LENGTH);

    return `
      <div class="list-item" onclick="window.handleListItemClick(event, '${fill.ticker}')">
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
      </div>
    `;
  }

  /**
   * Display orderbook
   * @param {Object|null} data - Orderbook data or null to clear
   */
  displayOrderbook(data) {
    const container = document.getElementById("orderbook-info");
    const marketOrdersSection = document.getElementById(
      "market-orders-section",
    );
    const limitOrdersSection = document.getElementById("limit-orders-section");

    if (!container) return;

    if (!data) {
      this.currentOrderbookData = null;
      container.innerHTML = this.buildOrderbookEmptyHTML();
      this.setTradingSectionsState(
        marketOrdersSection,
        limitOrdersSection,
        true,
      );
      return;
    }

    this.currentOrderbookData = data;
    container.innerHTML = this.buildOrderbookHTML(data);
    this.setTradingSectionsState(
      marketOrdersSection,
      limitOrdersSection,
      false,
    );
    this.updateLimitPrice();
  }

  /**
   * Build empty orderbook HTML
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
   * Build orderbook HTML with full depth
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
    const title = truncate(data.title, CONFIG.MAX_ORDERBOOK_TITLE_LENGTH);

    // Build orderbook depth display (sorted descending by price)
    const yes_bids = [...(data.yes_bids || [])].sort((a, b) => b[0] - a[0]);
    const no_bids = [...(data.no_bids || [])].sort((a, b) => b[0] - a[0]);

    const showDepth = yes_bids.length > 0 || no_bids.length > 0;

    let depthHTML = "";
    if (showDepth) {
      const yesBidsHTML = yes_bids
        .slice(0, 5) // Show top 5 levels (best prices)
        .map(
          ([price, size]) =>
            `<div class="book-level"><span class="book-price">${price}¢</span><span class="book-size">${size}</span></div>`,
        )
        .join("");

      const noBidsHTML = no_bids
        .slice(0, 5) // Show top 5 levels (best prices)
        .map(
          ([price, size]) =>
            `<div class="book-level"><span class="book-price">${price}¢</span><span class="book-size">${size}</span></div>`,
        )
        .join("");

      depthHTML = `
        <div class="orderbook-depth">
          <div class="book-side">
            <div class="book-header yes">YES BIDS</div>
            ${yesBidsHTML || '<div class="book-empty">No bids</div>'}
          </div>
          <div class="book-side">
            <div class="book-header no">NO BIDS</div>
            ${noBidsHTML || '<div class="book-empty">No bids</div>'}
          </div>
        </div>
      `;
    }

    return `
      <div class="orderbook-display">
        <div class="orderbook-title">${title}</div>
        <div class="orderbook-prices">
          <div class="price-yes">YES ${yesPriceDisplay}</div>
          <div class="price-no">NO ${noPriceDisplay}</div>
        </div>
        ${depthHTML}
      </div>
    `;
  }

  /**
   * Enable/disable trading sections
   * @param {HTMLElement} marketSection - Market orders section element
   * @param {HTMLElement} limitSection - Limit orders section element
   * @param {boolean} disabled - Whether to disable sections
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
   * Update limit price placeholder
   */
  updateLimitPrice() {
    const side = this.getActiveToggleValue("data-side", "yes");
    const priceInput = document.getElementById("limit-price");

    if (this.currentOrderbookData && priceInput) {
      const price =
        side === "yes"
          ? this.currentOrderbookData.yes_price
          : this.currentOrderbookData.no_price;

      if (price !== null && price !== undefined) {
        priceInput.placeholder = `${price} (last traded)`;
      }
    }

    this.updateLimitOrderButtonText();
  }

  /**
   * Update limit order button text
   */
  updateLimitOrderButtonText() {
    const button = document.getElementById("place-limit-order-btn");
    const countInput = document.getElementById("limit-count");
    const priceInput = document.getElementById("limit-price");

    if (!button) return;

    const action = this.getActiveToggleValue("data-action", "buy");
    const side = this.getActiveToggleValue("data-side", "yes");
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
   * Set active toggle button
   * @param {string} attribute - Data attribute name
   * @param {string} value - Value to match
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
   * @param {string} attribute - Data attribute name
   * @param {string} defaultValue - Default value if none active
   * @returns {string} Active value or default
   */
  getActiveToggleValue(attribute, defaultValue) {
    const button = document.querySelector(`.btn-market[${attribute}].active`);
    if (!button) return defaultValue;
    const value = button.getAttribute(attribute);
    return value || defaultValue;
  }

  /**
   * Display bot hotkeys
   * @param {Object} hotkeys - Hotkeys configuration object
   */
  displayBotHotkeys(hotkeys) {
    const container = document.getElementById("bot-hotkeys-grid");
    if (!container) return;

    if (!hotkeys || Object.keys(hotkeys).length === 0) {
      container.innerHTML =
        '<div class="empty-state">No hotkeys configured</div>';
      return;
    }

    const toTitleCase = (str) => {
      return str
        .split(" ")
        .map(
          (word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase(),
        )
        .join(" ");
    };

    const buttons = Object.entries(hotkeys)
      .map(([keyword, config]) => {
        const title = `${config.action.toUpperCase()} ${config.count} ${config.side.toUpperCase()} @ ${config.ticker}`;
        return `<button class="hotkey-button"
                      onclick="window.executeBotHotkey('${keyword}')"
                      title="${title}">
                ${toTitleCase(keyword)}
              </button>`;
      })
      .join("");

    container.innerHTML = buttons;
  }

  /**
   * Update bot status
   * @param {Object} data - Bot status data
   */
  updateBotStatus(data) {
    const statusText = document.getElementById("bot-status-text");
    const marketTitle = document.getElementById("bot-market-title");
    const statsText = document.getElementById("bot-stats");
    const toggleBtn = document.getElementById("bot-toggle-btn");
    const hotkeysGrid = document.getElementById("bot-hotkeys-grid");

    if (!statusText || !toggleBtn) return;

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
      if (statsText) {
        statsText.textContent = `Uptime: ${uptime}`;
      }

      this.wsClient.send("get_hotkeys");
    } else {
      statusText.textContent = "Offline";
      statusText.style.color = "var(--text-muted)";
      toggleBtn.textContent = "Activate";
      toggleBtn.classList.add("secondary");
      toggleBtn.classList.remove("danger");

      if (marketTitle) marketTitle.textContent = "";
      if (statsText) statsText.textContent = "";
      if (hotkeysGrid) {
        hotkeysGrid.innerHTML =
          '<div class="empty-state">Activate bot to use hotkeys</div>';
      }
    }
  }

  /**
   * Display analytics data
   * @param {Object|null} data - Analytics data or null
   */
  displayAnalytics(data) {
    const analyticsOverall = document.getElementById("analytics-overall");

    if (!data) {
      if (analyticsOverall) {
        analyticsOverall.innerHTML =
          '<div class="empty-state">No portfolio history available</div>';
      }
      return;
    }

    const current = data.current || {};
    const stats = data.stats || {};
    const history = data.history || [];

    const currentValue = current.total_value_cents || 0;
    const changeCents = stats.change_cents || 0;
    const changePct = stats.change_percent || 0;
    const highValue = stats.high_cents || 0;
    const lowValue = stats.low_cents || 0;

    const changeColor = changeCents >= 0 ? "var(--green)" : "var(--red)";
    const changeSign = changeCents >= 0 ? "+" : "";

    const overallHtml = `
      <div class="analytics-stats-grid">
        <div class="analytics-stat-card">
          <div class="analytics-stat-label">Current Value</div>
          <div class="analytics-stat-value">${formatCurrency(currentValue)}</div>
        </div>
        <div class="analytics-stat-card">
          <div class="analytics-stat-label">Change (30 days)</div>
          <div class="analytics-stat-value" style="color: ${changeColor}">
            ${changeSign}${formatCurrency(changeCents)} (${changeSign}${changePct.toFixed(2)}%)
          </div>
        </div>
        <div class="analytics-stat-card">
          <div class="analytics-stat-label">High/Low (30 days)</div>
          <div class="analytics-stat-value">${formatCurrency(highValue)}/${formatCurrency(lowValue)}</div>
        </div>
        <div class="analytics-stat-card">
          <div class="analytics-stat-label">Snapshots</div>
          <div class="analytics-stat-value">${stats.snapshots_count || 0}</div>
        </div>
      </div>
    `;
    if (analyticsOverall) analyticsOverall.innerHTML = overallHtml;

    // Portfolio history chart
    this.renderPortfolioChart(history);
  }

  /**
   * Render portfolio value chart
   * @param {Array} history - Array of portfolio snapshots
   */
  renderPortfolioChart(history) {
    const canvas = document.getElementById("portfolio-chart");
    if (!canvas) return;

    const container = canvas.parentElement;
    if (!container) return;

    // Destroy existing chart if it exists
    if (this.portfolioChart) {
      this.portfolioChart.destroy();
      this.portfolioChart = null;
    }

    if (history.length === 0) {
      // Show empty state instead of broken canvas
      container.innerHTML =
        '<div class="empty-state" style="display: flex; align-items: center; justify-content: center; height: 400px;">No history yet - click "Take Snapshot" to start tracking</div>';
      return;
    }

    // Restore canvas if it was replaced
    let chartCanvas = container.querySelector("canvas");
    if (!chartCanvas) {
      container.innerHTML = '<canvas id="portfolio-chart"></canvas>';
      chartCanvas = container.querySelector("canvas");
    }

    // Format data for Chart.js
    const labels = history.map((snapshot) => {
      const date = new Date(snapshot.timestamp);
      return date.toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    });

    const values = history.map((snapshot) => snapshot.value_cents / 100);

    // Create chart
    const ctx = chartCanvas.getContext("2d");
    this.portfolioChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Portfolio Value",
            data: values,
            borderColor: "#8b5cf6",
            backgroundColor: "rgba(139, 92, 246, 0.1)",
            borderWidth: 2,
            fill: true,
            tension: 0.4,
            pointRadius: 3,
            pointHoverRadius: 5,
            pointBackgroundColor: "#8b5cf6",
            pointBorderColor: "#fff",
            pointBorderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          intersect: false,
          mode: "index",
        },
        plugins: {
          legend: {
            display: false,
          },
          tooltip: {
            backgroundColor: "rgba(22, 24, 46, 0.95)",
            titleColor: "#ffffff",
            bodyColor: "#9ca3c8",
            borderColor: "#2d3250",
            borderWidth: 1,
            padding: 12,
            displayColors: false,
            callbacks: {
              label: function (context) {
                return "Value: $" + context.parsed.y.toFixed(2);
              },
            },
          },
        },
        scales: {
          x: {
            grid: {
              color: "rgba(45, 50, 80, 0.3)",
              drawBorder: false,
            },
            ticks: {
              color: "#9ca3c8",
              maxRotation: 45,
              minRotation: 45,
            },
          },
          y: {
            grid: {
              color: "rgba(45, 50, 80, 0.3)",
              drawBorder: false,
            },
            ticks: {
              color: "#9ca3c8",
              callback: function (value) {
                return "$" + value.toFixed(2);
              },
            },
          },
        },
      },
    });
  }

  /**
   * Show toast notification
   * @param {string} message - Message to display
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
}
