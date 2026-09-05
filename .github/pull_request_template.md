# Pull Request

Thanks for contributing to AIOS. Please fill out the checklist below.

## Summary

<!-- What does this PR do and why? -->

## Related issue

<!-- Link the issue this closes, if any. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / chore
- [ ] Tests only

## Checklist

- [ ] CI is green (offline mode).
- [ ] No new model calls added to CI.
- [ ] No secrets, no private paths, no provider defaults flipped to on.
- [ ] Tests added or updated.
- [ ] `docs/` updated if behaviour or contract changed.
- [ ] `CHANGELOG.md` updated under the next unreleased version.
- [ ] No silent test skips; failing tests labelled `KNOWN_FAILURES`.

## Security considerations

- [ ] No auth or default-bind changes.
- [ ] No Provider enabled by default.
- [ ] No key material in source, logs, fixtures, screenshots, or output.

## Reproduction / verification

<!-- Commands run, observed output, screenshots. -->