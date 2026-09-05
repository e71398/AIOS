#!/bin/bash
# AIOS systemd entry point. Services are never started directly here.
set -euo pipefail

case "${1:-start}" in
  --status|status)
    systemctl --user --no-pager --full status aios-core.target
    ;;
  start)
    systemctl --user start aios-core.target
    systemctl --user --no-pager --failed
    ;;
  restart)
    systemctl --user restart aios-core.target
    systemctl --user --no-pager --failed
    ;;
  *)
    echo "Usage: $0 [start|restart|status]" >&2
    exit 2
    ;;
esac
