#!/usr/bin/env bash
# Registers nl-image-gen-mcp with Claude Code on this machine (user scope, so it's
# available in every project, not just this repo). Run on any LAN machine that has
# Claude Code installed; the MCP server itself must already be running (see
# nl-image-gen/docker-compose.yml), typically on the Unraid box.
#
# Usage: ./register.sh [host] [port]
#   host  defaults to 192.168.50.46 (the Unraid box)
#   port  defaults to 8000 (MCP_PORT's default)
set -euo pipefail

HOST="${1:-192.168.50.46}"
PORT="${2:-8000}"

claude mcp add --transport http nl-image-gen "http://${HOST}:${PORT}/mcp" --scope user

echo "Registered nl-image-gen at http://${HOST}:${PORT}/mcp (user scope)."
echo "Run 'claude mcp list' to confirm, or 'claude mcp remove nl-image-gen' to undo."
