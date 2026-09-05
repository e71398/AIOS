# Current Limitations

This document is the **honest** list of what does not work, what is
partial, and what is intentionally not built yet in AIOS
v0.1.0-alpha.1. It exists so contributors do not have to read the code
to find out.

## Hard blockers for "MVP usable"

1. **Provider Registry inconsistent in systemd Orchestrator.**
   `aios_orchestrator.py --daemon` builds a Provider Registry from
   `config/module_manifest.json` plus the dynamic tool overlay. With the
   current configuration, `minimax-official` is reported as
   `UNVERIFIED`, and the registry has duplicate entries for a small
   number of modules. The normal `POST /task 鈫?Orchestrator` loop is
   therefore not end-to-end passing. **NORMAL_ENTRY_E2E = 0 / 5.**

2. **No first-class read-only AIOS state tool for Executor.** Executors
   must inspect the bus via raw Redis client. This is brittle and
   contributes to the Orchestrator failure domain.

3. **No sandboxed write tool for Executor.** File-system writes from
   executors currently go through ToolBus paths that are not yet
   audited. The sandbox write policy is **draft** in `docs/TOOL_SECURITY.md`.

## Quality gates that are partially failing

4. **Native Canary suite has known failures.** They are documented and
   labelled `KNOWN_FAILURES` in CI. The runner does not silently skip
   them and does not rewrite the pass rate.

5. **Some pytest tests have ordering-dependent failures.** Run them
   with `pytest -p no:randomly` or follow the documented order. CI uses
   the documented order.

6. **Planner / Reviewer structured JSON truncation.** Both roles emit
   JSON inside a token budget. Long plans may be truncated mid-field.

## Things intentionally not built yet

7. **No production-grade authentication.** Bind defaults to
   `127.0.0.1`. Non-loopback binds require `AUTH_REQUIRED_FOR_NON_LOOPBACK=1`
   plus a token, but the token system is alpha-grade.

8. **No multi-tenant workspace isolation.** Single-user assumption for
   v0.1.0-alpha.1.

9. **No high-availability deployment story.** systemd user units are
   the supported local development layout. There is no official
   multi-host or container orchestration story in this Alpha.

10. **No Provider SDK vendoring.** Adapters communicate with Providers
    via HTTP only. If you want a SDK wrapper, contribute one as a
    Provider Adapter and add a row to `THIRD_PARTY_NOTICES.md`.

## Things that are fine and intentionally so

11. **Most file paths are now `${AIOS_HOME}` / `${HOME}` based.** The
    repository no longer contains hard-coded per-operator absolute user-home paths
    in source.

12. **All Provider defaults are OFF.** You must opt in.

13. **Tests can be run offline.** No Provider is contacted from CI.

## Where to look

- `README.md` for project status.
- `ROADMAP.md` for what is planned.
- `docs/ARCHITECTURE.md` for component boundaries.
- `docs/PROVIDER_ADAPTER.md` for the Provider contract.
- `docs/TOOL_SECURITY.md` for the Tool sandbox contract.