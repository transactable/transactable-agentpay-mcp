'''
Thin async client over the Alchemy AgentPay proxy with x402 payment handling.

Why this exists separately from server.py: the MCP tools should focus on
input/output shapes; the payment dance is a separate concern with its own
failure modes. Keeping the two apart makes it easy to swap payment providers
(MPP Stripe, future rails) without touching the tool layer.

The x402 handshake:
  1. Send the actual request to the proxy with no payment header.
  2. Proxy returns 402 Payment Required with `accepts: [...]` requirements.
  3. We pick a scheme/network we support, build a signed payment payload, and
     retry the request with `X-PAYMENT: <base64(payload)>`.
  4. Proxy verifies, forwards to cw-backend, returns the upstream JSON.

We delegate the actual signing to Coinbase's `x402` Python client when
available (handles EIP-3009 for USDC etc.). If it's not installed, we surface
a clear error rather than failing silently.
'''
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import httpx

from transactable_agentpay.config import Config

logger = logging.getLogger('transactable_agentpay.x402')


class PaymentNotConfiguredError(RuntimeError):
    '''Raised when a paid call is attempted without wallet config.'''


class PaymentRequiredError(RuntimeError):
    '''Raised when the proxy returns 402 and we cannot satisfy it.'''


class ProxyError(RuntimeError):
    '''Raised for non-payment proxy/upstream errors. Holds upstream JSON if present.'''

    def __init__(self, message: str, status_code: int, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AgentPayClient:
    '''Async client that talks to the AgentPay proxy with x402 retry.'''

    def __init__(self, config: Config):
        self._config = config
        self._signer = self._build_signer(config) if config.is_payment_ready else None

    # Map the rail segment in the base URL to a CAIP-2 network identifier.
    # Alchemy's AgentPay proxy advertises 402 PaymentRequirements with networks
    # like 'eip155:84532' (Base Sepolia) and 'eip155:8453' (Base mainnet), so
    # the scheme we register on the x402 client must match.
    _RAIL_TO_NETWORK = {
        'x402-testnet': 'eip155:84532',  # Base Sepolia
        'x402-mainnet': 'eip155:8453',   # Base mainnet
    }

    @staticmethod
    def _build_signer(config: Config):
        '''Lazy import so the package can be inspected without web3 deps installed.

        Returns an `eth_account.LocalAccount`. AgentPayClient.request() wraps it
        in an x402 SchemeRegistration matching the rail in the base URL.
        '''
        try:
            from eth_account import Account  # type: ignore
        except ImportError as exc:
            logger.error(
                'eth-account not installed. Install with: pip install eth-account',
                exc_info=exc if config.debug else None,
            )
            return None

        return Account.from_key(config.private_key)

    def _resolve_network(self) -> str:
        '''Pick the CAIP-2 network ID from the rail segment of the base URL.'''
        # base_url is like https://agent-proxy.alchemy.com/v1/<rail>/<project_id>
        parts = self._config.base_url.rstrip('/').split('/')
        rail = parts[-2] if len(parts) >= 2 else ''
        return self._RAIL_TO_NETWORK.get(rail, 'eip155:84532')

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        '''Make a single proxy call. Handles 402 -> sign -> retry automatically.'''
        if not self._config.is_payment_ready:
            raise PaymentNotConfiguredError(
                'TRANSACTABLE_WALLET_PRIVATE_KEY is not set. The MCP cannot make paid calls '
                'to AgentPay without a wallet. Set the env var to a hex-encoded EVM private '
                'key that holds USDC on the network AgentPay charges on. See README.md.'
            )

        if self._signer is None:
            raise PaymentNotConfiguredError(
                'x402 signing dependencies are missing. Install with: '
                'pip install "x402>=2.10" eth-account'
            )

        url = f'{self._config.base_url}/{path.lstrip("/")}'

        # x402 2.x: build a config with a SchemeRegistration that maps the rail's
        # CAIP-2 network to an EVM ExactScheme signer. wrapHttpxWithPaymentFromConfig
        # returns an httpx.AsyncClient that handles the 402-and-retry transparently.
        from x402 import SchemeRegistration, x402ClientConfig
        from x402.http.clients.httpx import wrapHttpxWithPaymentFromConfig
        from x402.mechanisms.evm.exact.client import ExactEvmScheme

        network = self._resolve_network()
        config = x402ClientConfig(
            schemes=[
                SchemeRegistration(network=network, client=ExactEvmScheme(self._signer)),
            ],
        )

        async with wrapHttpxWithPaymentFromConfig(
            config, timeout=self._config.timeout_seconds
        ) as client:
            try:
                response = await client.request(
                    method.upper(),
                    url,
                    json=json_body,
                    params=params,
                )
            except httpx.TimeoutException as exc:
                raise ProxyError(
                    f'Request to AgentPay proxy timed out after {self._config.timeout_seconds}s. '
                    'The cw-backend may be cold-starting; try again in a few seconds.',
                    status_code=504,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProxyError(
                    f'Transport error talking to AgentPay proxy: {exc}',
                    status_code=502,
                ) from exc

        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text

        if self._config.debug:
            print(
                f'[transactable-agentpay] {method.upper()} {path} -> {response.status_code}\n{body}',
                file=sys.stderr,
            )

        if response.status_code == 402:
            # The x402 client failed to satisfy payment. Surface the requirements
            # so the caller can diagnose (insufficient USDC, wrong network, etc.).
            raise PaymentRequiredError(
                f'Payment could not be completed. AgentPay returned 402 with: {body}'
            )

        if response.status_code >= 400:
            raise ProxyError(
                f'AgentPay returned {response.status_code}',
                status_code=response.status_code,
                body=body,
            )

        if isinstance(body, dict):
            return body
        return {'_raw': body}

    @property
    def configured_wallet_address(self) -> Optional[str]:
        '''Address derived from the configured private key, or None if not set.'''
        if not self._config.is_payment_ready:
            return None
        try:
            from eth_account import Account  # type: ignore
            return Account.from_key(self._config.private_key).address
        except Exception:  # pragma: no cover — defensive
            return None
