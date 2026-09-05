#!/usr/bin/env bash
# 2026-08-17 P1-HARD-001: install AIOS core service hardening drop-ins.
# Idempotent; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${HOME}/.config/systemd/user"
mkdir -p "${DEST}"

for src in "${HERE}"/*.conf; do
    base="$(basename "${src}" .00-hardening.conf)"
    mkdir -p "${DEST}/${base}.service.d"
    cp -f "${src}" "${DEST}/${base}.service.d/00-hardening.conf"
    echo "Installed ${base} hardening drop-in"
done

systemctl --user daemon-reload
echo "systemd user daemon-reloaded"
