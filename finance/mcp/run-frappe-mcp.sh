#!/bin/sh
# Start the ERPNext MCP server as the agent API user. Claude Code runs this
# as its MCP command (see mcp.example.json). The credentials are read from
# ~/ws_yardr_finance/.erpnext-api at each start and passed on as environment,
# so neither the key nor the secret is written to the repo or to argv.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
KEYS="$HOME/ws_yardr_finance/.erpnext-api"

if [ ! -f "$KEYS" ]; then
  echo "run-frappe-mcp: $KEYS is missing; run finance/scripts/create-api-user.sh" >&2
  exit 1
fi

# The keys file holds the API URL with a trailing /api/; the client adds
# /api/ to its base URL itself, so strip it here.
FRAPPE_URL=
FRAPPE_API_KEY=
FRAPPE_API_SECRET=
while IFS='=' read -r key value; do
  case "$key" in
    url) FRAPPE_URL="$value" ;;
    api_key) FRAPPE_API_KEY="$value" ;;
    api_secret) FRAPPE_API_SECRET="$value" ;;
  esac
done < "$KEYS"
FRAPPE_URL="${FRAPPE_URL%/}"
FRAPPE_URL="${FRAPPE_URL%/api}"
export FRAPPE_URL FRAPPE_API_KEY FRAPPE_API_SECRET

exec "$HERE/venv/bin/frappe-mcp-server"
