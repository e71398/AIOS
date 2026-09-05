#!/usr/bin/env bash
# Full regression runner for AIOS.
#
# This script is intentionally offline. It does NOT call any Provider.
# It runs the test suite in a documented order, and it does NOT
# silently skip failing tests. Tests with `KNOWN_FAILURES` are
# flagged honestly in the output.
#
# Exit codes:
#   0 - all tests passed (this is rare in Alpha; many failures are known)
#   1 - one or more tests failed
#   2 - environment / setup error

set -u

cd "$(dirname "$0")/.."

export AIOS_OFFLINE=1
export EXTERNAL_PROVIDERS_ENABLED=0
export AIOS_MINIMAX_OFFICIAL_ENABLED=0

echo "[aios] full regression starting"
echo "[aios] AIOS_OFFLINE=$AIOS_OFFLINE"
echo "[aios] EXTERNAL_PROVIDERS_ENABLED=$EXTERNAL_PROVIDERS_ENABLED"
echo "[aios] AIOS_MINIMAX_OFFICIAL_ENABLED=$AIOS_MINIMAX_OFFICIAL_ENABLED"

PYTEST="$(command -v pytest || true)"
if [ -z "$PYTEST" ]; then
  echo "[aios] pytest not installed; pip install pytest" >&2
  exit 2
fi

set +e

# Stable contract tests — these are expected to pass in Alpha.
echo "[aios] running stable contract tests"
pytest -q kernel/tools/tests/test_stable_v1_cli_contract.py \
       kernel/tools/tests/test_registry_path_boundary.py 2>&1
STABLE_RC=$?

# Full pytest — known failures will be reported as FAILED, not skipped.
echo "[aios] running full pytest (known failures reported honestly)"
pytest -q --tb=line --no-header -p no:cacheprovider --override-ini="addopts=" \
       kernel/tools/tests 2>&1 | tee /tmp/aios_full_regression.log
FULL_RC=${PIPESTATUS[0]}

echo ""
echo "[aios] full regression summary"
echo "[aios] stable contract tests exit: $STABLE_RC"
echo "[aios] full pytest exit:             $FULL_RC"

if [ "$STABLE_RC" -ne 0 ]; then
  echo "[aios] STABLE_RC != 0 — fix the contract tests first"
  exit 1
fi

# Alpha: full suite may fail. Surface the failure honestly.
if [ "$FULL_RC" -ne 0 ]; then
  echo "[aios] full pytest reported failures; see /tmp/aios_full_regression.log"
  echo "[aios] these are documented in docs/CURRENT_LIMITATIONS.md"
  echo "[aios] exit code is 1 (KNOWN_FAILURES), not rewritten as PASS"
  exit 1
fi

echo "[aios] full regression OK"
exit 0