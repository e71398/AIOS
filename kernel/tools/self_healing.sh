#!/bin/bash
# AIOS health reconciler. systemd owns processes; this script only requests
# recovery through systemd and never starts duplicate nohup/wrapper processes.

set -u
AIOS_HOME="${AIOS_HOME:-${AIOS_HOME}}"
TOOLS="$AIOS_HOME/kernel/tools"

log() { echo "[$(date -Iseconds)] $1"; }

# An inactive core target means the owner intentionally stopped the workload
# from the control center. Do not fight that decision.
if ! systemctl --user is-active --quiet aios-core.target; then
    log "AIOS core target intentionally inactive; reconciliation skipped"
    exit 0
fi

if ! redis-cli ping >/dev/null 2>&1; then
    log "ERROR Redis is unavailable; system service intervention required"
fi

services=(
    aios-entry-gateway.service
    aios-web.service
    aios-monitor.service
    aios-event-daemon.service
    aios-enforcer-daemon.service
    aios-executor-opencode.service
    aios-executor-claude.service
    aios-executor-codex.service
    aios-verification-gate.service
    aios-result-push.service
    aios-runtime.service
    aios-intel.service
    aios-model-gateway.service
    aios-api-proxy.service
)

for service in "${services[@]}"; do
    if ! systemctl --user is-active --quiet "$service"; then
        log "WARN $service inactive; requesting systemd restart"
        systemctl --user restart "$service"
    fi
done

python3 "$TOOLS/aios_deadlock_detector.py" >/dev/null 2>&1 || \
    log "WARN deadlock detector returned non-zero"

log "health reconciliation complete"
