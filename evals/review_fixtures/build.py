"""Rebuild the review fixtures from this repo's own history (needs a clone with the PR commits fetchable,
and `gh` for the PR title/body). Run from the repo root: python -m evals.review_fixtures.build

Each fixture is the diff of one PR at one commit the bot reviewed, against the commit the PR branched from.
`pre` fixtures are commits whose review found a real problem that was then fixed; `clean` ones are the fixed
commits the reviewer approved. Files that are recorded data, not code, are left out of the diff (see EXCLUDE).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
EXCLUDE = [":(exclude)tests/histories/*.json"]  # recorded Temporal histories: ~300 KB of JSON nobody reviews
# name -> (PR number, reviewed commit)
FIXTURES = {
    "pr12_review_flow": (12, "945bab36765f19e1ca902890734779ed05572758"),
    "pr17_resolve_conflicts": (17, "5883d8fe94f7c33ddbfab9b6a641ed54b3ebbd70"),
    "pr22_resolver_timeouts": (22, "a500468048d3854c9dd71f2bc478221f2a4374d8"),
    "pr31_approval_expiry": (31, "aa8098f27258b089b90d9c9c1f292ba786aa1c82"),
    "pr33_search_attributes": (33, "a3379dc878b9ebd3fe650eee5a5f80b521bf0a4b"),
    "pr41_batch_evals": (41, "4e3a0513bf2b09c9b797310a77c009553252212d"),
    "pr31_approval_expiry_fixed": (31, "9beb5fc192e66b1b6e92807cf4cd53055c54213f"),
    "pr33_search_attributes_fixed": (33, "b7302a09300155180684fa1815cf90e56220ac92"),
    "pr41_batch_evals_fixed": (41, "9745fda8a8934a960917a6c4a462f0c8cd6b1580"),
    "pr43_reviewer_upgrades_fixed": (43, "7d2e60ce0041dab3805dfe56d68499ea9fd4de2c"),
}


def git(*a: str) -> str:
    return subprocess.run(["git", *a], check=True, capture_output=True, text=True).stdout


def main() -> None:
    for name, (pr, sha) in FIXTURES.items():
        git("fetch", "-q", "origin", sha)
        sha = git("rev-parse", sha).strip()
        base = git("merge-base", sha, "origin/main").strip()
        info = json.loads(subprocess.run(["gh", "pr", "view", str(pr), "--json", "title,body"], check=True,
                                         capture_output=True, text=True).stdout)
        d = HERE / name
        d.mkdir(exist_ok=True)
        (d / "diff.patch").write_text(git("diff", base, sha, "--", ".", *EXCLUDE))
        (d / "meta.json").write_text(json.dumps({"pr": pr, "commit": sha, "base": base, "title": info["title"],
                                                 "body": info["body"]}, indent=1) + "\n")
        print(name, len((d / "diff.patch").read_text()))


if __name__ == "__main__":
    main()
