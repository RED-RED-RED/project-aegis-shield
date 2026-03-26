"""
tests/conftest.py
=================
Pytest session-level setup for ARGUS node unit tests.

Scapy is only available on the deployed Pi hardware (and in the Pi venv).
Before any test module is collected, register a MagicMock stub in
sys.modules so that `from scapy.all import ...` in scanner/wifi_nan.py
and in the test files themselves succeeds without the real package.

Attribute access on a MagicMock is stable and idempotent — repeated
accesses to `scapy_mock.Dot11` return the same child mock — so the
identity comparisons used in test helpers (`if layer is Dot11`) work
correctly as long as both the scanner module and the test file obtain
`Dot11` from the same sys.modules entry.
"""

import sys
from unittest.mock import MagicMock

# Only install the stub when scapy is genuinely absent.
if "scapy" not in sys.modules:
    _scapy_stub = MagicMock()
    sys.modules["scapy"]           = _scapy_stub
    sys.modules["scapy.all"]       = _scapy_stub
    sys.modules["scapy.layers"]    = _scapy_stub
    sys.modules["scapy.layers.dot11"] = _scapy_stub
