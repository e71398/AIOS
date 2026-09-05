# Demo Provider (no external calls)

This directory contains a **Demo** Provider Adapter that returns
deterministic strings without contacting any external model API. It is
shipped as a template and a working example.

> **Do not** confuse the Demo with a real Provider. It is clearly
> labelled `Demo` and is intended for:

- new contributor onboarding,
- running the AIOS test suite fully offline,
- demoing the Provider Adapter contract without spending budget.

## Files

- `provider.py` — the Demo adapter.
- `test_demo_provider.py` — offline unit test using a stub HTTP server.
- `README.md` — this file.

## Contract

The Demo Provider implements `kernel/tools/providers/base.py` and exposes:

- `name = "demo_provider"`
- `enabled = False` by default; you must explicitly flip it.
- `invoke(request) -> AdapterResponse` returns a deterministic echo
  of the request plus a small fake usage block.
- `health()` returns `AdapterHealth(status="ok")`.
- `shutdown()` is a no-op.

## Enabling

Set in `.env`:

```
EXTERNAL_PROVIDERS_ENABLED=1
AIOS_DEMO_PROVIDER_ENABLED=1
```

Set in `config/features.toml`:

```
[providers.demo_provider]
enabled = true
```

Restart the Orchestrator.

## License

Apache License 2.0. See `../../LICENSE`.