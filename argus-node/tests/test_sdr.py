"""
tests/test_sdr.py
=================
Environment smoke tests for the RTL-SDR Python binding.

These tests verify the runtime environment rather than application logic:

  - pyrtlsdr imports without error (catches the pkg_resources / Python 3.13
    incompatibility that patch-pyrtlsdr.sh is designed to fix)
  - librtlsdr.so is findable by ctypes (catches a missing librtlsdr-dev package)
  - The pkg_resources import in pyrtlsdr __init__.py is wrapped in try/except
    (verifies patch-pyrtlsdr.sh was actually applied)

All tests skip gracefully when the rtlsdr Python package is not installed —
this covers developer machines and CI environments without an RTL-SDR dongle.
Hardware does NOT need to be physically attached for these tests to pass on a
correctly-configured Pi; they only verify library installation.

Run:
    python -m pytest argus-node/tests/test_sdr.py -v
"""

import importlib.util
import subprocess
import sys
from ctypes.util import find_library

import pytest


def _rtlsdr_pkg_installed() -> bool:
    """Return True if the rtlsdr Python package is present in this interpreter."""
    try:
        return importlib.util.find_spec("rtlsdr") is not None
    except (ModuleNotFoundError, ValueError):
        return False


class TestRTLSDREnvironment:

    def test_rtlsdr_package_importable(self):
        """
        `import rtlsdr` must succeed in the current Python interpreter.

        Runs as a subprocess so that an ImportError (e.g. from a missing
        pkg_resources) surfaces as a test failure with the full traceback
        rather than crashing the test runner.
        """
        if not _rtlsdr_pkg_installed():
            pytest.skip("rtlsdr Python package not installed in this environment")

        result = subprocess.run(
            [sys.executable, "-c", "import rtlsdr"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "import rtlsdr failed — the pkg_resources compatibility patch may "
            "not have been applied.\n"
            "Run: bash scripts/patch-pyrtlsdr.sh\n\n"
            f"stderr: {result.stderr.strip()}"
        )

    def test_librtlsdr_findable(self):
        """
        ctypes must be able to locate librtlsdr.so on the system library path.

        If this fails, install: sudo apt-get install -y librtlsdr-dev
        """
        if not _rtlsdr_pkg_installed():
            pytest.skip("rtlsdr Python package not installed — skipping library check")

        lib = find_library("rtlsdr")
        assert lib is not None, (
            "librtlsdr.so not found on the system library path.\n"
            "Install: sudo apt-get install -y librtlsdr-dev"
        )

    def test_pkg_resources_import_is_guarded(self):
        """
        If pyrtlsdr __init__.py imports pkg_resources, that import must be
        wrapped in a try/except — confirming patch-pyrtlsdr.sh was applied.

        pyrtlsdr versions that do not use pkg_resources at all also pass this
        test (the bare `import pkg_resources` string is simply absent).
        """
        if not _rtlsdr_pkg_installed():
            pytest.skip("rtlsdr Python package not installed")

        spec = importlib.util.find_spec("rtlsdr")
        if spec is None or spec.origin is None:
            pytest.skip("Cannot locate rtlsdr/__init__.py to inspect")

        with open(spec.origin) as fh:
            content = fh.read()

        if "import pkg_resources" in content:
            assert "except ImportError" in content, (
                "pyrtlsdr __init__.py has a bare 'import pkg_resources' with no "
                "try/except guard — this will fail on Python 3.13.\n"
                "Run: bash scripts/patch-pyrtlsdr.sh"
            )
