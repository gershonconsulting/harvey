"""Gershon add-on: leads kept out of H01 on purpose are not "failed to send" (2026-11-25)."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gershon_reports.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("gershon_reports_kept_out", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_kept_out_is_not_a_failure():
    mod = load_mod()
    assert not mod.is_real_failure("Gershon: kept out of H01 (US / already in US / junk) - closed 2026-11-25")
    assert not mod.is_real_failure("Risky address, held back")
    assert not mod.is_real_failure("not deliverable")


def test_real_errors_still_alert():
    mod = load_mod()
    assert mod.is_real_failure("Gmail API 403 forbidden")
    assert mod.is_real_failure("daily quota exceeded")
    assert mod.is_real_failure("")
