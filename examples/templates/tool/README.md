# Tool Template

Copy this directory, replace the placeholders, and you have a
Tool skeleton that:

- ships with permission `deny` by default,
- has a unit test under `kernel/tools/tests/` shape,
- documents the permission model.

## Files

- `tool.py.template` — copy to `tool.py` and edit.
- `test_tool.py.template` — copy to `test_tool.py` and edit.
- `README.md` — this file.

## Steps

1. Copy `tool.py.template` to `tool.py` and replace the placeholders.
2. Default `permission = "deny"` until you have a justification.
3. Implement `invoke()` per the contract in `docs/TOOL_SECURITY.md`.
4. Copy `test_tool.py.template` to `test_tool.py`.
5. Open a PR.

## License

Apache License 2.0.