"""The docs list things the code defines (env vars, tools, tables). Fail when they drift apart."""
from __future__ import annotations

import re
from pathlib import Path

from aoa.store import SCHEMA
from aoa.tools import REGISTRY

DOCS = Path(__file__).resolve().parent.parent / "docs"
ROOT = DOCS.parent


def test_configuration_page_lists_every_env_var():
    page = (DOCS / "reference" / "configuration.md").read_text()
    wanted = set(re.findall(r"AOA_[A-Z][A-Z_]+", (ROOT / ".env.example").read_text()))
    missing = sorted(n for n in wanted if n not in page)
    assert not missing, f"add to docs/reference/configuration.md: {missing}"


def test_tools_page_lists_every_registered_tool():
    page = (DOCS / "reference" / "tools.md").read_text()
    missing = sorted(n for n in REGISTRY if f"`{n}`" not in page)
    assert not missing, f"add to docs/reference/tools.md: {missing}"


def test_store_page_lists_every_table():
    page = (DOCS / "reference" / "store.md").read_text()
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
    missing = [t for t in tables if f"`{t}`" not in page]
    assert not missing, f"add to docs/reference/store.md: {missing}"


def test_every_relative_doc_link_resolves():
    bad = []
    for md in list(DOCS.rglob("*.md")) + [ROOT / "README.md"]:
        for target in re.findall(r"\]\((?!https?:|#|mailto:)([^)#\s]+)", md.read_text()):
            if not (md.parent / target).resolve().exists():
                bad.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not bad, f"broken links: {bad}"


def test_every_reference_page_is_linked_from_the_architecture_page():
    arch = (DOCS / "architecture.md").read_text()
    unlinked = [p.name for p in (DOCS / "reference").glob("*.md") if f"reference/{p.name}" not in arch]
    assert not unlinked, f"link from docs/architecture.md: {unlinked}"
