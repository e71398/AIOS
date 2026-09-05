#!/usr/bin/env bash
set -euo pipefail

CODEX_BIN="${HOME}/.n/bin/codex"
WORKDIR="${AIOS_CODEX_WORKDIR:-${AIOS_HOME}/sandbox/coding}"
MODEL="${AIOS_CODEX_MODEL:-MiniMax-M3}"
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-aios-loopback-only}"

if [[ "${1:-}" == "--version" ]]; then
  codex_version="$("${CODEX_BIN}" --version)"
  relay_version="$(${HOME}/.local/bin/codex-relay --version)"
  printf '%s; %s; model %s\n' "${codex_version}" "${relay_version}" "${MODEL}"
  exit 0
fi

mode="${1:-}"
prompt="${2:-}"
if [[ -z "${prompt}" ]]; then
  echo "CodexAdapterError: empty task" >&2
  exit 2
fi

case "${mode}" in
  --probe) sandbox="read-only" ;;
  --task) sandbox="workspace-write" ;;
  *)
    echo "CodexAdapterError: expected --probe or --task" >&2
    exit 2
    ;;
esac

exec "${CODEX_BIN}" exec \
  -m "${MODEL}" \
  --disable apps \
  --disable plugins \
  --disable browser_use \
  --disable computer_use \
  --ephemeral \
  --skip-git-repo-check \
  -C "${WORKDIR}" \
  -s "${sandbox}" \
  "${prompt}" </dev/null
