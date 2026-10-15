"""Config/env hygiene: the settle window, and .env.example staying in step with the AOA_* names the code reads."""
from __future__ import annotations

import re
from pathlib import Path

from aoa.config import Settings

ROOT = Path(__file__).resolve().parent.parent

# Names that may be absent from .env.example, each with a reason. Anything new must be added to
# .env.example or here, on purpose.
ALLOWLIST = {
    "AOA_SESSION",  # read outside Settings (slack_bridge, client_cli, scheduling)
    "AOA_GITHUB_USER",  # read outside Settings (scheduling.ensure_github_schedule)
}


def test_review_settle_seconds(monkeypatch, tmp_path):
    monkeypatch.setenv("AOA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AOA_REVIEW_SETTLE_SECONDS", raising=False)
    assert Settings.from_env().review_settle_seconds == 300
    for raw, want in (("0", 0), ("-5", 0), ("120", 120)):
        monkeypatch.setenv("AOA_REVIEW_SETTLE_SECONDS", raw)
        assert Settings.from_env().review_settle_seconds == want


def test_every_env_var_the_code_reads_is_in_env_example():
    # names only, from .env.example: never .env
    documented = set(re.findall(r"AOA_[A-Z][A-Z_]+", (ROOT / ".env.example").read_text()))
    used: set[str] = set()
    for f in (ROOT / "aoa").glob("*.py"):
        used |= {n for n in re.findall(r"AOA_[A-Z][A-Z_]+", f.read_text()) if not n.endswith("_")}
    missing = used - documented - ALLOWLIST
    assert not missing, f"add to .env.example (or the allowlist above): {sorted(missing)}"
