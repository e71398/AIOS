#!/bin/bash
# Stop AIOS-managed modules. Independent AI tool installations remain intact.
set -euo pipefail
systemctl --user stop \
  aios-feishu-entry.service aios-telegram-entry.service \
  aios-api-proxy.service aios-model-gateway.service \
  aios-runtime.service aios-intel.service aios-web.service \
  aios-result-push.service aios-verification-gate.service \
  aios-executor-opencode.service aios-executor-claude.service aios-executor-codex.service \
  openclaw-gateway.service \
  aios-enforcer-daemon.service aios-event-daemon.service aios-entry-gateway.service \
  aios-core.target
echo "AIOS workload stopped; control center remains on 127.0.0.1:8086"
