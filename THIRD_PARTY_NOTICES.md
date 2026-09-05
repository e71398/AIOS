# Third-Party Notices

This file lists third-party components included with or referenced by AIOS
v0.1.0-alpha.1, their versions, sources, licenses, purpose and whether they are
distributed with this repository.

Only components shipped inside this repository tree are bundled. Provider SDKs
that the system *talks to* over HTTP but does not bundle are listed for
attribution only.

| Component | Version | Source | License | Purpose | Distributed |
| --- | --- | --- | --- | --- | --- |
| Python standard library | 3.10+ | https://www.python.org | PSF | Runtime | No |
| Redis | 6+ | https://redis.io | BSD-3-Clause | State bus | No (operator-provided) |
| pytest | latest stable | https://pytest.org | MIT | Tests | No (dev only) |
| Flask (if used by `kernel/centers/*/web_server.py`) | upstream | https://flask.palletsprojects.com | BSD-3-Clause | Internal HTTP endpoints for centers | Source only |
| OpenAI Python SDK (referenced, not bundled) | n/a | https://github.com/openai/openai-python | Apache-2.0 | Provider Adapter contract | No |
| Anthropic SDK (referenced, not bundled) | n/a | https://github.com/anthropics/anthropic-sdk-python | MIT | Provider Adapter contract | No |
| MiniMax API (referenced, not bundled) | n/a | https://platform.MiniMax.ai | Commercial | Provider Adapter contract (paid) | No |
| OpenAI-compatible HTTP contract | n/a | https://platform.openai.com | OpenAI Terms | Provider Adapter contract | No |

## Notes on license compatibility

- The repository ships **only** source we wrote or that was clearly released under
  a permissive license (MIT / BSD / Apache-2.0). No GPL, AGPL, or SSPL code is
  bundled in this repository.
- Provider SDKs are *not* vendored. Adapters communicate with them at runtime
  if and only if the user installs the SDK and sets the corresponding API key.
- No binary blobs, model weights, fonts or proprietary assets are shipped in
  this repository.
- No third-party code with unclear provenance is included. Where a file's
  origin was uncertain, it has been excluded from the public candidate.

If you believe a component is missing from this list or is misattributed, open
an issue or see `SECURITY.md`.