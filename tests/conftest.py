"""Session-wide test isolation.

AMY_SAAS_DATA must be set BEFORE anything imports amy.saas.paths / db —
those modules bind the data directory and the SQLAlchemy engine at import
time. Individual fixtures that set the env var inside a test function are
too late whenever another test module already imported amy.saas, and the
suite then silently reads/writes the REAL saas_data/amy_saas.db (this
actually happened: a test signup leaked into the real user DB).

conftest.py is imported by pytest before any test module, so setting the
env here guarantees every suite — regardless of ordering — binds to a
throwaway directory.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="amy_test_saas_")
os.environ["AMY_SAAS_DATA"] = _TEST_DATA_DIR
# strong, deterministic JWT secret for tests (avoids writing .jwt_secret files)
os.environ.setdefault("AMY_JWT_SECRET", "test-secret-" + "x" * 32)
