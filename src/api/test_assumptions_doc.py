"""
docs/assumptions.md is generated from ASSUMPTIONS (risk_calendar.py) and
API_ASSUMPTIONS (app.py). These tests fail if the doc is stale: re-run
`python scripts/build_assumptions_doc.py` and commit the result.

Run from the repo root: python -m pytest src -q
"""

import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as api  # noqa: E402
import risk_calendar as rc  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "build_assumptions_doc.py")
DOC = os.path.join(ROOT, "docs", "assumptions.md")

spec = importlib.util.spec_from_file_location("build_assumptions_doc", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_doc_is_up_to_date():
    with open(DOC, encoding="utf-8", newline="") as f:
        assert f.read() == builder.build(), \
            "docs/assumptions.md is stale: run python scripts/build_assumptions_doc.py"


def test_every_assumption_is_in_the_doc_with_a_reason():
    text = builder.build()
    for table in (rc.ASSUMPTIONS, api.API_ASSUMPTIONS):
        for name, entry in table.items():
            assert f"`{name}`" in text
            assert entry.get("why"), f"{name} has no 'why'"


def test_sensitivity_values_are_shown():
    text = builder.build()
    assert "re-run with `1`, `5`" in text      # hot_days
    assert "re-run with `15`" in text          # flowering_window_days
