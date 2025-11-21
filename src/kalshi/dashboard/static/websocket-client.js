/**
 * WebSocket Client - Connection management with reconnection logic
 * Handles WebSocket lifecycle and message routing
 */

const RECONNECT_BASE_DELAY = 1000; // 1 second
const RECONNECT_MAX_DELAY = 30000; // 30 seconds

/**
 * WebSocket client with automatic reconnection
 */
export class WebSocketClient {
  constructor(onMessage, onOpen = null) {
    this.ws = null;
    this.reconnectAttempts = 0;
    this.onMessage = onMessage; // Callback for incoming messages
    this.onOpen = onOpen; // Callback for connection opened
  }

  /**
   * Establish WebSocket connection
   */
  connect() {
    // Close existing connection before creating new one to prevent memory leaks
    if (this.ws) {
      this.ws.onclose = null; // Remove handler to prevent reconnect loop
      this.ws.close();
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    this.ws.onopen = () => this.handleOpen();
    this.ws.onmessage = (event) => this.handleMessage(event);
    this.ws.onclose = () => this.handleClose();
    this.ws.onerror = (error) => this.handleError(error);
  }

  /**
   * Handle connection opened
   */
  handleOpen() {
    console.log("Connected to Kalshi Trading Dashboard");
    this.reconnectAttempts = 0;
    document.getElementById("status").classList.add("connected");

    // Call onOpen callback if provided
    if (this.onOpen) {
      this.onOpen();
    }
  }

  /**
   * Handle incoming message
   */
  handleMessage(event) {
    try {
      const data = JSON.parse(event.data);
      this.onMessage(data);
    } catch (error) {
      console.error("Failed to parse message:", error);
    }
  }

  /**
   * Handle connection closed with exponential backoff
   */
  handleClose() {
    console.log("Disconnected from server");
    document.getElementById("status").classList.remove("connected");

    // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (max)
    const delay = Math.min(
      RECONNECT_MAX_DELAY,
      RECONNECT_BASE_DELAY * Math.pow(2, this.reconnectAttempts),
    );

    this.reconnectAttempts++;
    console.log(
      `Reconnecting in ${delay / 1000}s (attempt ${this.reconnectAttempts})...`,
    );

    setTimeout(() => this.connect(), delay);
  }

  /**
   * Handle connection error
   */
  handleError(error) {
    console.error("WebSocket error:", error);
  }

  /**
   * Send message to server
   * @param {string} action - Action type
   * @param {Object} data - Message data
   */
  send(action, data = {}) {
    const message = { action, ...data };
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }
}
