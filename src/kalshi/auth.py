"""Shared authentication utilities for Kalshi API."""

import base64
import time
from pathlib import Path
from typing import Dict

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_private_key(key_path: Path) -> rsa.RSAPrivateKey:
    """Load RSA private key from PEM file."""
    with open(key_path, "r", encoding="utf-8") as f:
        private_key_str = f.read()
    return serialization.load_pem_private_key(
        private_key_str.encode("utf-8"), password=None
    )


def sign_request(
    private_key: rsa.RSAPrivateKey, timestamp: str, method: str, path: str
) -> str:
    """
    Generate RSA-PSS signature for Kalshi API authentication.

    Args:
        private_key: RSA private key
        timestamp: Unix timestamp in milliseconds (as string)
        method: HTTP method (e.g., "GET", "POST")
        path: API path (e.g., "/portfolio/balance")

    Returns:
        Base64-encoded signature
    """
    message = f"{timestamp}{method}{path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def get_auth_headers(
    private_key: rsa.RSAPrivateKey, api_key_id: str, method: str, path: str
) -> Dict[str, str]:
    """
    Generate complete authentication headers for Kalshi API.

    Args:
        private_key: RSA private key
        api_key_id: Kalshi API key ID
        method: HTTP method (e.g., "GET", "POST")
        path: API path (e.g., "/portfolio/balance")

    Returns:
        Dictionary with KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
    """
    timestamp = str(int(time.time() * 1000))
    signature = sign_request(private_key, timestamp, method, path)

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
