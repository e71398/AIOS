# Provider Adapter Template

Copy this directory, replace the placeholders, and you have the
skeleton of a Provider Adapter that ships with:

- default-deny enabled flag,
- stub HTTP test (no real Provider call),
- third-party notice row stub,
- honest documentation.

## Files

- `provider.py.template` — copy to `provider.py` and edit.
- `test_provider.py.template` — copy to `test_provider.py` and edit.
- `third_party_notice.md` — the row to add to `THIRD_PARTY_NOTICES.md`.
- `README.md` — this file.

## Steps

1. Copy `provider.py.template` to `provider.py`.
2. Replace `<PROVIDER_NAME>` and `<BASE_URL>` placeholders.
3. Implement `invoke()`, `health()`, `shutdown()` per the contract in
   `docs/PROVIDER_ADAPTER.md`.
4. Default `enabled = False`.
5. Copy `test_provider.py.template` to `test_provider.py` and add a
   stub HTTP server. Do **not** call the real Provider from CI.
6. Append the row from `third_party_notice.md` to
   `THIRD_PARTY_NOTICES.md`.
7. Open a PR.

## License

Apache License 2.0.