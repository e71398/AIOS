# Minimal Workflow (Demo only, no external calls)

This is a *minimal* Workflow that uses the Demo Provider and Demo
Tool to produce a deterministic output. It exists as a template
for new Workflow authors and as a working example for the offline
test suite.

## What it does

1. Calls the Demo Provider with a deterministic prompt.
2. Calls the Demo Tool to read `README.md` (or any file under
   `$AIOS_DATA_DIR`).
3. Returns a JSON object combining the two results.

## What it does NOT do

- It does not contact any real Provider.
- It does not write to disk.
- It does not require any API keys.

## Files

- `workflow.py` — the workflow implementation.
- `test_workflow.py` — offline unit test.
- `README.md` — this file.

## License

Apache License 2.0. See `../../LICENSE`.