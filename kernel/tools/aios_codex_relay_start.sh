#!/usr/bin/env bash
set -euo pipefail

: "${MINIMAX_API_KEY:?MINIMAX_API_KEY is required}"
upstream="${MINIMAX_API_BASE:-https://api.minimax.chat/v1/chat/completions}"
upstream="${upstream%/chat/completions}"
export CODEX_RELAY_API_KEY="${MINIMAX_API_KEY}"

exec ${HOME}/.local/bin/codex-relay \
  --port 4444 \
  --bind 127.0.0.1 \
  --upstream "${upstream}"
