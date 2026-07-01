#!/bin/bash
# Launcher for transactable-agentpay-mcp under Claude Desktop (or any MCP host).
# Sources the repo's .env so the wallet private key never has to land in
# claude_desktop_config.json. Point the host's "command" at this script.
#
# Python is resolved from PATH (python3); override with TRANSACTABLE_MCP_PYTHON.

set -e

MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MCP_DIR"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

PYTHON="${TRANSACTABLE_MCP_PYTHON:-$(command -v python3)}"

export PYTHONPATH="$MCP_DIR/src:${PYTHONPATH:-}"
exec "$PYTHON" -m transactable_agentpay
