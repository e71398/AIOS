# AIOS Service Hardening Drop-ins (P1-HARD-001)

Per-service systemd drop-ins that add `MemoryMax`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `RestrictNamespaces`, `RestrictAddressFamilies` and
`NoNewPrivileges` to the AIOS core daemons.

## Files

* `aios-orchestrator.00-hardening.conf` — orchestrator workflow loop
* `aios-verification-gate.00-hardening.conf` — verification gate
* `aios-executor-{codex,opencode,claude}.00-hardening.conf` — executors
* `aios-{api-proxy,enforcer-daemon,event-daemon,web,result-push}.00-hardening.conf` — leaf daemons

## Install

```bash
cd ${AIOS_HOME}
bash systemd/hardening/install.sh
systemctl --user daemon-reload
```

The installer is idempotent. It deploys each `.conf` into the
matching `${HOME}/.config/systemd/user/<service>.service.d/00-hardening.conf`
location, then `systemctl --user daemon-reload`s.

## ReadWritePaths carve-out

`ProtectHome=read-only` is paired with explicit `ReadWritePaths=` allowances
for the user-owned directories the AIOS subprocesses (openclaw, hermes, codex)
need to write to:

```
${HOME}/.openclaw
${HOME}/.hermes
${HOME}/.nvm
${HOME}/.cache
${HOME}/.local
${AIOS_HOME}/cache
${AIOS_HOME}/sandbox
${AIOS_HOME}/checkpoint
${AIOS_HOME}/logs
${AIOS_HOME}/tmp
```

Without these carve-outs the openclaw reviewer subprocess returns
`NONZERO_EXIT rc=1` (verified during the closure run before the carve-out
was added).

## Verified

* `./aios task "简短问候"` → completed
* `./aios audit <file>` → completed
* `./aios ops <text>` → completed
* All five executors still selectable (`codex`, `opencode`, `claude`,
  `hermes`, `openclaw`); `claude` remains `provider_quota` (HTTP 402)
  excluded by routing, as before — unrelated to this change.

See `docs/AIOS_SERVICE_HARDENING_20260817.md` for the full closure matrix.
