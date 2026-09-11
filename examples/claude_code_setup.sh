#!/usr/bin/env bash
# ============================================================================
# 1-Click Setup for Claude Code with DuraLLM
# ============================================================================
set -euo pipefail

GATEWAY_PORT="${1:-4001}"
GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}/v1"

echo "⚡ Configuring Claude Code to use DuraLLM on ${GATEWAY_URL}..."

# Export Anthropic base URL for Claude Code
export ANTHROPIC_BASE_URL="${GATEWAY_URL}"
export ANTHROPIC_API_KEY="sk-circuit-breaker-token"

# Optional: configure ~/.claude/settings.json
CLAUDE_CONFIG_DIR="${HOME}/.claude"
mkdir -p "${CLAUDE_CONFIG_DIR}"
SETTINGS_FILE="${CLAUDE_CONFIG_DIR}/settings.json"

if [ ! -f "${SETTINGS_FILE}" ]; then
  cat <<EOF > "${SETTINGS_FILE}"
{
  "env": {
    "ANTHROPIC_BASE_URL": "${GATEWAY_URL}",
    "ANTHROPIC_API_KEY": "sk-circuit-breaker-token"
  }
}
EOF
  echo "✔ Created ${SETTINGS_FILE}"
fi

echo "🚀 Starting Claude Code..."
exec claude "$@"
