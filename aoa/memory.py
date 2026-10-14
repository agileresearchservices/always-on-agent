"""File-based memory in three layers, mirroring how the lecture describes it.

* core facts  : profile.md, injected into every prompt (kept small)
* dated logs  : logs/YYYY-MM-DD.md, searched on demand, never injected wholesale
* scratchpad  : ephemeral notes with an expiry, injected only while still live
* skills      : skills/<name>.md, procedures the agent wrote down after learning them

Everything is plain markdown so the agent can also grep it with ordinary shell commands.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

CORE_FACTS_MAX_CHARS = 4000
PROFILE_HEADER = "# Core facts\n"


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / "skills").mkdir(parents=True, exist_ok=True)
        if not self.profile_path.exists():
            self.profile_path.write_text(PROFILE_HEADER)

    @property
    def profile_path(self) -> Path:
        return self.root / "profile.md"

    @property
    def scratch_path(self) -> Path:
        return self.root / "scratchpad.tsv"

    # ---- core facts ------------------------------------------------------
    def core_facts(self) -> str:
        text = self.profile_path.read_text().strip()
        return text[:CORE_FACTS_MAX_CHARS]

    def remember_fact(self, fact: str) -> str:
        fact = fact.strip().replace("\n", " ")
        if not fact:
            return "empty fact ignored"
        existing = self.profile_path.read_text()
        bullet = f"- {fact}"
        if bullet in existing.splitlines():
            return "already known"
        if len(existing) + len(bullet) > CORE_FACTS_MAX_CHARS:
            return (
                "core facts are full. Rewrite memory with kind='facts_rewrite' to merge or drop stale "
                "items, then retry."
            )
        with self.profile_path.open("a") as f:
            f.write(("" if existing.endswith("\n") else "\n") + bullet + "\n")
        return "remembered"

    def rewrite_facts(self, facts: list[str]) -> str:
        body = PROFILE_HEADER + "".join(f"- {f.strip()}\n" for f in facts if f.strip())
        if len(body) > CORE_FACTS_MAX_CHARS:
            return f"too long ({len(body)} > {CORE_FACTS_MAX_CHARS} chars); condense further"
        self.profile_path.write_text(body)
        return "rewritten"

    # ---- dated log ---------------------------------------------------------
    def append_log(self, text: str, now: datetime) -> None:
        path = self.root / "logs" / f"{now:%Y-%m-%d}.md"
        line = f"- {now:%H:%M} {text.strip()}\n"
        with path.open("a") as f:
            f.write(line)

    def recent_log(self, now: datetime, max_lines: int = 12) -> list[str]:
        path = self.root / "logs" / f"{now:%Y-%m-%d}.md"
        if not path.exists():
            return []
        return path.read_text().splitlines()[-max_lines:]

    # ---- scratchpad (ephemeral) -----------------------------------------------
    def add_scratch(self, text: str, ttl_minutes: int, now: datetime) -> None:
        expiry = (now + timedelta(minutes=ttl_minutes)).isoformat()
        with self.scratch_path.open("a") as f:
            f.write(f"{expiry}\t{text.strip().replace(chr(10), ' ')}\n")

    def scratch_active(self, now: datetime) -> list[str]:
        if not self.scratch_path.exists():
            return []
        live: list[str] = []
        keep: list[str] = []
        for line in self.scratch_path.read_text().splitlines():
            try:
                expiry_s, text = line.split("\t", 1)
                if datetime.fromisoformat(expiry_s) > now:
                    live.append(text)
                    keep.append(line)
            except ValueError:
                continue
        self.scratch_path.write_text("".join(k + "\n" for k in keep))
        return live

    # ---- skills --------------------------------------------------------------
    @staticmethod
    def _slug(name: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "skill"

    def save_skill(self, name: str, markdown: str) -> str:
        path = self.root / "skills" / f"{self._slug(name)}.md"
        path.write_text(markdown.strip() + "\n")
        return f"saved skills/{path.name}"

    def list_skills(self) -> list[str]:
        out = []
        for p in sorted((self.root / "skills").glob("*.md")):
            first = (p.read_text().splitlines() or [""])[0].lstrip("# ").strip()
            out.append(f"{p.stem}: {first}")
        return out

    # ---- search / read -----------------------------------------------------------
    def search(self, query: str, max_results: int = 20) -> list[str]:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        hits: list[str] = []
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root)
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:240]}")
                    if len(hits) >= max_results:
                        return hits
        return hits

    def read(self, rel: str, max_chars: int = 8000) -> str:
        target = (self.root / rel).resolve()
        if self.root.resolve() not in target.parents and target != self.root.resolve():
            raise ValueError("path escapes the memory directory")
        if not target.is_file():
            raise FileNotFoundError(f"no such memory file: {rel}")
        return target.read_text()[:max_chars]
