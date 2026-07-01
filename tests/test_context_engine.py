import datetime as dt
import os
import tempfile

import pytest

from amy.collab import CollabDB
from amy.engines import ContextEngine


def _db():
    return CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_ctx_"), "collab.db"))


def test_detect_weekend_and_work():
    ce = ContextEngine(_db())
    base = dt.datetime(2026, 6, 1, 10, 0)
    sat = base + dt.timedelta(days=(5 - base.weekday()) % 7)   # next Saturday, 10:00
    assert sat.weekday() == 5
    assert ce.detect(now=sat) == "weekend"
    wed = (sat + dt.timedelta(days=4)).replace(hour=10)         # Wednesday 10:00
    assert ce.detect(now=wed) == "work"


def test_set_mode_overrides_and_profile():
    ce = ContextEngine(_db())
    ce.set_mode("focus")
    assert ce.detect() == "focus"
    prof = ce.profile()
    assert prof["mode"] == "focus"
    assert "priority_domains" in prof and "recommendations" in prof and "top_goals" in prof


def test_invalid_mode_rejected():
    ce = ContextEngine(_db())
    with pytest.raises(ValueError):
        ce.set_mode("party")
