# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| v0.1.0-alpha.1 | Yes 鈥?actively worked on |
| older | No |

AIOS is in Alpha. Only the latest alpha release receives security fixes.

## Reporting a vulnerability

**Do not open a public GitHub issue for security bugs.**

Email the maintainer team at the address listed in `SUPPORT.md`. Include:

- a clear description of the issue,
- reproduction steps,
- the commit / tag you reproduced against,
- whether you believe the bug is currently exploitable,
- your contact details if you would like an acknowledgement.

We aim to acknowledge new reports within 7 days. We will coordinate
disclosure timing with you before publishing any CVE or advisory.

## Threat model (in scope)

- Process-level isolation between AIOS daemons (entry, planner, executor,
  reviewer, monitor, web).
- Tool sandboxing for executor-driven file writes.
- Provider key isolation 鈥?keys live in `.env` only, never in source.
- Network bind defaults 鈥?only `127.0.0.1` by default; non-loopback
  requires `AUTH_REQUIRED_FOR_NON_LOOPBACK=1` plus a valid token.
- systemd hardening 鈥?every shipped unit uses
  `NoNewPrivileges=true` and `PrivateTmp=true` and runs as the
  unprivileged `aios` service user.

## Out of scope

- Issues caused by enabling a paid Provider that we cannot reproduce
  without that Provider's cooperation.
- Bugs in upstream libraries (pytest, Flask, Redis, etc.) 鈥?please
  report those upstream.
- Configuration mistakes made by operators after enabling non-default
  features.

## Hardening guidance

- Run with `AIOS_OFFLINE=1` until you have a Provider integration plan.
- Keep `EXTERNAL_PROVIDERS_ENABLED=0` and `AIOS_MINIMAX_OFFICIAL_ENABLED=0`
  in `.env` unless you deliberately need them.
- Never commit `.env`. `.gitignore` already excludes it.
- Use the systemd units as shipped 鈥?do not relax hardening directives
  without a security review.