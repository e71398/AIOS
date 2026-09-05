# Provider Adapter Contract

A Provider Adapter is the only thing in AIOS that talks to an external
language model API. Everything else (Planner, Executor, Reviewer) talks
to the Adapter. This keeps the rest of the system Provider-agnostic and
keeps API-key handling in one place.

## Goals

- Provider can be turned off without code changes.
- Provider can be swapped without changing Planner / Executor / Reviewer.
- Provider cost can be capped per task.
- Provider misuse cannot leak keys into logs.

## Contract

A Provider Adapter exposes a stable interface:

```python
class ProviderAdapter:
    name: str                       # e.g. "openai_compat"
    enabled: bool                   # default False

    def invoke(self, request: AdapterRequest) -> AdapterResponse: ...
    def health(self) -> AdapterHealth: ...
    def shutdown(self) -> None: ...
```

`AdapterRequest` and `AdapterResponse` are defined in
`kernel/tools/providers/base.py`.

## Default state

All shipped adapters have `enabled = False`. To turn an adapter on you
must:

1. Set `EXTERNAL_PROVIDERS_ENABLED=1` in `.env`.
2. Provide the corresponding API key in `.env`.
3. Flip the feature flag in `config/features.toml`:
   `ai_provider_<name> = on`.
4. Restart the Orchestrator.

## MiniMax

MiniMax is shipped as a paid Provider. `AIOS_MINIMAX_OFFICIAL_ENABLED`
defaults to `0`. The adapter (`kernel/tools/providers/minimax_official.py`)
honours a per-task budget read from `config/features.toml`.

> **Do not** flip the MiniMax flag in CI. CI is offline.

## Cost guard

Adapters must implement `invoke()` such that:

- Every call is logged with a per-task token count.
- The Orchestrator can refuse a call if it would exceed the per-task
  budget.
- Adapter logs must not contain raw request or response bodies — only
  metadata.

## Failure modes

- Adapter returns `AdapterResponse(status="rate_limited")` → Orchestrator
  marks the call as retryable with backoff.
- Adapter returns `AdapterResponse(status="budget_exceeded")` →
  Orchestrator marks the task as failed; Reviewer is invoked.
- Adapter raises → wrapped into `AdapterResponse(status="error")` with
  a generic message. The full exception is logged to a private debug
  file in `$AIOS_DATA_DIR/logs/` and never to the bus.

## Adding a Provider

See `docs/DEVELOPMENT.md`. Always default new adapters to **off** and
add a stub-HTTP test.