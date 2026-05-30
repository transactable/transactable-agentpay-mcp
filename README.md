# transactable-agentpay-mcp

An MCP server that lets any MCP-aware AI agent — Claude Cowork, Claude Code, Cursor, Goose, OpenAI Agents — register provenance for a creative work on [Transactable](https://transactable.com) via [Alchemy AgentPay](https://www.alchemy.com/agentpay).

The agent pays per call with USDC (x402); cw-backend handles hashing, AI examination, certificate generation, and optional NFT mint.

## What you get

Four tools, mirroring the four `/agentpay/*` endpoints on cw-backend:

| Tool | Purpose | Paid? |
|---|---|---|
| `transactable_register_copyright` | Create a new registration. File can be a local path or base64. | Yes |
| `transactable_lookup_registration` | Get status / result. Can poll until terminal. | Yes per call |
| `transactable_verify_hash` | Check if a SHA-256 has been registered before. | Yes |
| `transactable_get_upload_url` | Get a signed GCS URL for files you want to upload yourself. | Yes |

For typical use you only need the first two — `transactable_register_copyright` handles the upload/inline decision automatically based on file size.

## Install

```bash
# pypi (once published)
pip install transactable-agentpay-mcp

# or run without installing
uvx transactable-agentpay-mcp

# or from source
git clone https://github.com/transactable/transactable-agentpay-mcp
cd transactable-agentpay-mcp
pip install -e .
```

## Configure

Copy `.env.example` to `.env` and fill in:

```bash
# Testnet (Base Sepolia, free testnet USDC):
TRANSACTABLE_AGENTPAY_BASE_URL=https://agent-proxy.alchemy.com/v1/x402-testnet/11488ccf8e53b480

# Mainnet (real USDC charged per call):
# TRANSACTABLE_AGENTPAY_BASE_URL=https://agent-proxy.alchemy.com/v1/x402-mainnet/11488ccf8e53b480

TRANSACTABLE_WALLET_PRIVATE_KEY=0x...   # EVM key with USDC on the chosen rail
```

The URL pattern is `https://agent-proxy.alchemy.com/v1/<rail>/<project_id>`. The rail (`x402-testnet`, `x402-mainnet`, or `mpp-stripe`) determines which network and instrument the proxy uses. Switch rails by switching the URL — no code change needed.

For testnet development, generate a throwaway key (`cast wallet new` from foundry works) and fund it from a faucet.

For production, use a wallet you control. Anthropic-managed CDP wallets are the cleanest path — the MCP will accept any hex-encoded EVM private key.

## Register the server with your host

### Claude Code / Claude Cowork (`~/.claude/mcp.json` or equivalent)

```json
{
  "mcpServers": {
    "transactable-agentpay": {
      "command": "uvx",
      "args": ["transactable-agentpay-mcp"],
      "env": {
        "TRANSACTABLE_AGENTPAY_BASE_URL": "https://agent-proxy.alchemy.com/v1/x402-testnet/11488ccf8e53b480",
        "TRANSACTABLE_WALLET_PRIVATE_KEY": "0x..."
      }
    }
  }
}
```

### Cursor

Settings → MCP → Add new server. Use the same command/args/env as above.

### Goose

```yaml
extensions:
  transactable-agentpay:
    type: stdio
    cmd: uvx
    args: ["transactable-agentpay-mcp"]
    envs:
      TRANSACTABLE_AGENTPAY_BASE_URL: https://agent-proxy.alchemy.com/v1/x402-testnet/11488ccf8e53b480
      TRANSACTABLE_WALLET_PRIVATE_KEY: 0x...
```

## Quickstart for the agent

A skill / system prompt that teaches the agent how to use it:

```
To register a creative work on Transactable:

1. Call `transactable_verify_hash` first with the file_path. If the work is already
   registered, surface the existing registration_number instead of paying again.

2. If not registered, call `transactable_register_copyright` with:
     - title: short title for the work
     - authors: [{name, country}] — for AI-generated work, the operator is the author
     - file_path: absolute local path (MCP handles inline vs upload automatically)

3. Call `transactable_lookup_registration` with poll=true and the returned
   registration_number to wait for the certificate URL and (optionally) NFT mint.

4. Show the user the certificate_url and nft_transaction_hash.
```

## How payment works

Every tool call goes through `agent-proxy.alchemy.com`. On the first request, the proxy returns HTTP 402 with payment requirements. The MCP signs an EIP-3009 USDC transfer authorization for the requested amount, retries the call with an `X-PAYMENT` header, and the proxy forwards to `cw-backend` only after payment is verified.

You can see the price per endpoint in your AgentPay dashboard. The wallet you configured needs enough USDC on the configured network (mainnet, base, base-sepolia depending on AgentPay project setup) to cover the call.

## What about MPP Stripe?

The Transactable AgentPay project supports both x402 (crypto) and MPP Stripe (card). This MCP implements the x402 path because that's what most agent runtimes can negotiate today. A Stripe-backed variant is a single class swap in `x402_proxy.py` — open an issue if you need it.

## Development

```bash
pip install -e ".[dev]"

# Run the server locally over stdio (for manual testing with MCP Inspector)
python -m transactable_agentpay

# Or test with MCP Inspector
npx @modelcontextprotocol/inspector python -m transactable_agentpay
```

## Troubleshooting

**`payment_not_configured`**: Set `TRANSACTABLE_WALLET_PRIVATE_KEY`.

**`payment_failed`**: Wallet has insufficient USDC on the network AgentPay charges on, or the x402 negotiation failed. Check the wallet balance and the AgentPay dashboard for the expected network.

**`proxy_error` with status 503**: cw-backend dependency is unavailable (GCS, file-prep service). Retry in a few seconds; the call already paid so this can be costly — file an issue if it persists.

**`file_too_large`**: The inline limit is 8 MB. Pass `file_path` instead of `file_base64` and the MCP will switch to the signed-upload flow automatically.

## License

Apache-2.0.
