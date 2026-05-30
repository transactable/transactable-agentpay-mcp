# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **MCP server** that lets any MCP-aware AI agent (Claude Code, Claude Cowork, Cursor, Goose, OpenAI Agents) register provenance for a creative work on Transactable. It is **not** a Cloud Run service like the other five — it's a Python package distributed via pip / `uvx` that runs on the *agent's* host over stdio, and it *calls* the Django backend's `/agentpay/*` endpoints rather than being called by it.

The twist: every call is **paid**. Requests go through the Alchemy AgentPay proxy (`agent-proxy.alchemy.com`), which returns HTTP 402, the MCP signs an EIP-3009 USDC transfer (x402), retries with an `X-PAYMENT` header, and only then does the proxy forward to the backend. See `README.md` for the user-facing setup/usage guide; this file is the working guide for changing the code.

## Commands

```bash
pip install -e ".[dev]"                                  # install with dev deps (pytest, pytest-asyncio, ruff)
python -m transactable_agentpay                          # run the server over stdio (manual testing)
npx @modelcontextprotocol/inspector python -m transactable_agentpay   # run under MCP Inspector
pytest                                                   # unit tests
ruff check .                                             # lint (line-length 100, target py310)
```

There is no build/deploy step. To publish, build the wheel (`hatchling`) and push to PyPI as `transactable-agentpay-mcp`; agents then run it with `uvx transactable-agentpay-mcp`.

## Layout

```
src/transactable_agentpay/
├── server.py        # FastMCP app + the four @mcp.tool definitions, file-transport resolver, entrypoint main()
├── x402_proxy.py    # AgentPayClient: the 402-sign-retry payment dance, isolated from the tool layer
├── config.py        # Config dataclass loaded from env (no crash on missing required values)
├── models.py        # Pydantic input models (RegisterInput, LookupInput, VerifyHashInput, UploadUrlInput)
└── __main__.py      # `python -m transactable_agentpay` → server.main()
```

The split between `server.py` (I/O shapes) and `x402_proxy.py` (payment) is deliberate: it keeps payment-provider swaps (MPP Stripe, future rails) out of the tool layer.

## The four tools

Each mirrors one backend `/agentpay/*` endpoint and is prefixed `transactable_` to avoid collisions in a shared host:

| Tool | Backend endpoint | Notes |
|---|---|---|
| `transactable_register_copyright` | `POST /register/` | Primary tool. Auto-decides inline vs. signed-upload by file size. |
| `transactable_lookup_registration` | `GET /lookup/{id}` | Can `poll=true` until a terminal status (`completed`, `completed_no_nft`, `failed`). |
| `transactable_verify_hash` | `POST /verifyhash/` | Pre-flight dedupe check by SHA-256 (still a paid call). |
| `transactable_get_upload_url` | `POST /uploadurl/` | Signed GCS URL for self-managed uploads of large files. |

## Config (env vars)

Loaded once by `config.py:load_config()`. Local dev uses a `.env` (copy `.env.example`); on an agent host, vars are set in the MCP server registration block.

- **`TRANSACTABLE_AGENTPAY_BASE_URL`** — proxy URL, pattern `https://agent-proxy.alchemy.com/v1/<rail>/<project_id>`. Defaults to the **testnet** rail.
- **`TRANSACTABLE_WALLET_PRIVATE_KEY`** — hex EVM key holding USDC on the chosen network. Required for any paid call.
- Optional: `TRANSACTABLE_AGENT_NAME`, `TRANSACTABLE_MODEL_INFO` (JSON), `TRANSACTABLE_HTTP_TIMEOUT` (default 60s), `TRANSACTABLE_DEBUG` (`1` logs raw responses to **stderr**).
- Transport overrides: `TRANSACTABLE_MCP_TRANSPORT` (`stdio` default, or `streamable_http`), `TRANSACTABLE_MCP_PORT` (default 8765 for http).

## Gotchas

- **Never log to stdout.** Over stdio, stdout *is* the MCP wire. Logging is configured to stderr (`logging.basicConfig(stream=sys.stderr)`); `print()` debug output must also go to `file=sys.stderr`. Writing to stdout corrupts the protocol.
- **Every call costs USDC**, including `lookup` and `verify_hash`. `poll=true` repeats lookups on an interval — it can rack up paid calls for slow registrations. Default poll interval is 10s.
- **Testnet by default, on purpose.** A fresh checkout points at the `x402-testnet` rail (Base Sepolia, `eip155:84532`) so it can't accidentally spend mainnet USDC. Switching to mainnet (`x402-mainnet` → `eip155:8453`) is a URL change, not a code change. The rail→network mapping lives in `x402_proxy.py:_RAIL_TO_NETWORK`.
- **Missing config doesn't crash at import.** Tools return a structured error envelope (`payment_not_configured`, etc.) so the host can surface a setup hint instead of the server failing to boot.
- **8 MB inline limit.** Files under 8 MB are base64-inlined; at/over the limit the MCP requests a signed GCS upload URL and streams the file itself, then registers with the resulting `upload_token`. Don't raise the inline cap without matching the backend's `/agentpay/register` limit.
- **Coupled to the backend's `/agentpay/*` contract.** Payloads in `server.py` mirror the backend's `agentpay` serializers (e.g. `RegisterRequestSerializer`). If you change those serializers in `backend/`, update the payload builders here in lockstep.
- **x402 signing deps are lazy-imported** (`x402`, `eth-account`) so the package can be inspected without them; their absence surfaces as `payment_not_configured`, not an import crash.

## License

Apache-2.0.
