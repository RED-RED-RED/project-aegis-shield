#!/usr/bin/env bash
# scripts/patch-pyrtlsdr.sh
# ============================================================
# Apply a Python 3.13 compatibility patch to pyrtlsdr.
#
# Problem:
#   pyrtlsdr 0.2.x uses a bare `import pkg_resources` in its
#   __init__.py.  pkg_resources is provided by setuptools which
#   is no longer included in Python 3.13 venvs by default,
#   causing an ImportError at runtime.
#
# Fix:
#   Wrap the import in a try/except that falls back to
#   importlib.metadata (standard library since Python 3.8).
#
# Idempotent — the script checks for the guard clause before
# writing, so running it multiple times is safe.  It skips
# gracefully if pyrtlsdr is not installed in the target venv.
#
# Usage:
#   bash scripts/patch-pyrtlsdr.sh [/path/to/venv]
#
# Default venv path: /opt/argus-node/venv
# ============================================================

set -euo pipefail

VENV_DIR="${1:-/opt/argus-node/venv}"
PYTHON="$VENV_DIR/bin/python3"

if [[ ! -x "$PYTHON" ]]; then
    echo "[patch-pyrtlsdr] Python not found at $PYTHON — skipping"
    exit 0
fi

patched=0
for init_file in "$VENV_DIR"/lib/python3.*/site-packages/rtlsdr/__init__.py; do
    [[ -f "$init_file" ]] || continue

    # Export so the inner Python script can read it without shell quoting issues.
    export PYRTLSDR_INIT="$init_file"

    "$PYTHON" << 'PYEOF'
import os, sys

path = os.environ["PYRTLSDR_INIT"]

with open(path) as fh:
    content = fh.read()

OLD = 'import pkg_resources'
NEW = (
    'try:\n'
    '    import pkg_resources\n'
    'except ImportError:\n'
    '    import importlib.metadata as pkg_resources'
)

if OLD in content and 'except ImportError' not in content:
    content = content.replace(OLD, NEW, 1)
    with open(path, 'w') as fh:
        fh.write(content)
    print(f"[patch-pyrtlsdr] patched: {path}")
else:
    print(f"[patch-pyrtlsdr] already patched or pkg_resources not used: {path}")
PYEOF

    patched=1
done

if [[ $patched -eq 0 ]]; then
    echo "[patch-pyrtlsdr] rtlsdr/__init__.py not found in $VENV_DIR — nothing to patch"
fi
