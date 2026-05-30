'''
Runtime config for the MCP server, loaded from environment variables.

All settings have safe defaults except the two required values:
  - TRANSACTABLE_AGENTPAY_BASE_URL
  - TRANSACTABLE_WALLET_PRIVATE_KEY

Missing required values do NOT crash the server at import time. Instead, tool
invocations return a structured error so the host agent surface (Claude Cowork,
Claude Code, etc.) can show a setup hint to the user.
'''
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional


# Default points at the x402 testnet rail so a fresh checkout boots safely on
# testnet rather than accidentally hitting mainnet. Override with the env var
# for production: change the rail segment from `x402-testnet` to `x402-mainnet`
# (or `mpp-stripe`) without touching code.
DEFAULT_BASE_URL = 'https://agent-proxy.alchemy.com/v1/x402-testnet/11488ccf8e53b480'
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class Config:
    base_url: str
    private_key: Optional[str]
    agent_name: Optional[str]
    model_info: Optional[dict[str, Any]]
    timeout_seconds: float
    debug: bool

    @property
    def is_payment_ready(self) -> bool:
        return bool(self.private_key)


def load_config() -> Config:
    '''Read env vars once. Re-call only if env changes (rare).'''
    raw_model_info = os.environ.get('TRANSACTABLE_MODEL_INFO', '').strip()
    model_info: Optional[dict[str, Any]] = None
    if raw_model_info:
        try:
            parsed = json.loads(raw_model_info)
            if isinstance(parsed, dict):
                model_info = parsed
        except json.JSONDecodeError:
            # Tolerate malformed input — log via debug only, don't crash.
            model_info = None

    timeout_raw = os.environ.get('TRANSACTABLE_HTTP_TIMEOUT', '').strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS

    return Config(
        base_url=os.environ.get('TRANSACTABLE_AGENTPAY_BASE_URL', DEFAULT_BASE_URL).rstrip('/'),
        private_key=_normalize_private_key(os.environ.get('TRANSACTABLE_WALLET_PRIVATE_KEY', '')),
        agent_name=os.environ.get('TRANSACTABLE_AGENT_NAME', '').strip() or None,
        model_info=model_info,
        timeout_seconds=timeout,
        debug=os.environ.get('TRANSACTABLE_DEBUG', '').strip() in ('1', 'true', 'yes'),
    )


def _normalize_private_key(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None
    if not value.startswith('0x'):
        value = '0x' + value
    # Basic sanity check: 0x + 64 hex chars.
    if len(value) != 66:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value
