"""Pytest configuration — make the project root importable for tests."""
import os
import sys
import tempfile

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# shared.diagnostics.log() is reached from code paths the suite exercises on
# purpose (ffmpeg failures, mpv with no media). Keep those writes out of the
# source tree so a test run never leaves a commcut.log behind.
os.environ.setdefault(
    "COMMCUT_LOG", os.path.join(tempfile.gettempdir(), "commcut-tests.log"))


@pytest.fixture
def unlimited_path_length(monkeypatch):
    """Raise the platform's path limit so a test can exercise a different rule.

    A 250-character stem is a legal *file name* and an illegal *path* on
    Windows, and those are two different rules: the component rule and the
    absolute-path rule.  A test about the first must not be tripped by the
    second, and the second has tests of its own.
    """
    from shared import environment, paths

    key = environment.os_key()
    _limit, unit = paths.ABSOLUTE_PATH_LIMITS[key]
    monkeypatch.setitem(paths.ABSOLUTE_PATH_LIMITS, key, (100000, unit))
