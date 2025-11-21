/**
 * Formatters - Pure utility functions for data formatting
 * No dependencies, no side effects
 */

/**
 * Format cents as currency string
 * @param {number} cents - Value in cents
 * @returns {string} Formatted currency (e.g., "$12.34" or "-$12.34")
 */
export function formatCurrency(cents) {
  const dollars = cents / 100;
  const isNegative = dollars < 0;
  const absoluteDollars = Math.abs(dollars);

  const formatted = absoluteDollars.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  return isNegative ? `-$${formatted}` : `$${formatted}`;
}

/**
 * Truncate string to max length with ellipsis
 * @param {string} str - String to truncate
 * @param {number} maxLength - Maximum length
 * @returns {string} Truncated string
 */
export function truncate(str, maxLength) {
  if (!str) return "";
  return str.length > maxLength ? str.substring(0, maxLength - 3) + "..." : str;
}

/**
 * Format date as time string
 * @param {Date|string} date - Date to format
 * @returns {string} Formatted time (e.g., "01/15/25, 02:30:45 PM")
 */
export function formatTime(date) {
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
