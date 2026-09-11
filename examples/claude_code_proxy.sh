#!/usr/bin/env bash
# Quickstart script to start DuraLLM Proxy and launch Claude Code

echo "🚀 Starting DuraLLM Local Proxy on http://127.0.0.1:8000..."
python3 -m durallm.proxy --port 8000 &
PROXY_PID=$!

sleep 2

echo "🤖 Launching Claude Code with resilient failover bridge..."
export ANTHROPIC_BASE_URL="http://127.0.0.1:8000/v1"
claude

# Cleanup on exit
kill $PROXY_PID
