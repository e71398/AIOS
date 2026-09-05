# Demo Tool (read-only, no external calls)

This is a **Demo** Tool. It exposes a tiny read-only function and is
shipped as a template for new Tool authors.

> Do not mistake this for a real Tool with real side effects. The
> permission is **read** by default and only operates on the
> `$AIOS_DATA_DIR` tree.

## Files

- `tool.py` — the Demo tool.
- `test_demo_tool.py` — offline unit test.
- `README.md` — this file.

## Permission

| Permission | Default |
| --- | --- |
| `read`   | granted |
| `write`  | denied  |
| `network`| denied  |
| `subprocess` | denied |

## License

Apache License 2.0. See `../../LICENSE`.