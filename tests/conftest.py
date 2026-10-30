"""Shared pytest configuration.

Two responsibilities:

1. Force headless Qt so widget tests can run without a display.
2. Redirect the application database to a throwaway temporary directory so a
   test run can never modify the user's real ``screen_monitor.db``.

``SCREEN_MONITOR_DB`` must be set *before* :mod:`src.database.init_db` is
imported, because that module resolves and caches ``DB_PATH`` at import time.
pytest imports ``conftest.py`` ahead of every test module, which makes this the
correct place for it.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Hard isolation: never fall through to the production database.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="screen_monitor_tests_")
os.environ["SCREEN_MONITOR_DB"] = str(
    Path(_TEST_DB_DIR) / "screen_monitor_test.db")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
