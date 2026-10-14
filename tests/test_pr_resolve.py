"""Conflict resolution (PR flow stage 3) against a real local git remote: clone, merge, resolve (scripted
model), commit, test, push. No network, no GitHub, git itself is not mocked."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aoa.pr_resolve import COMMIT_TAG, Resolver, git_env, repo_test_command, safe_env
from aoa.testing import FakeLLM, text, tool

REPO = "agileresearchservices/demo"
ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
BASE_FILE = "a = 0\nx = 1\nz = 9\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """origin.git with main and feat that both changed line 2 of app.py. Returns (origin, feat sha)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text(BASE_FILE)
    (seed / "notes.md").write_text("Title\n=======\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "checkout", "-q", "-b", "feat")
    (seed / "app.py").write_text("a = 0\nx = 2  # feat\nz = 9\n")
    git(seed, "commit", "-qam", "feat change")
    git(seed, "checkout", "-q", "main")
    (seed / "app.py").write_text("a = 0\nx = 3  # main\nz = 9\n")
    git(seed, "commit", "-qam", "main change")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main", "feat")
    return origin, git(seed, "rev-parse", "feat"), seed


def resolver(tmp_path, remote, llm, sha=None, marks=None):
    origin, feat_sha, _ = remote
    return Resolver(repo=REPO, sha=sha or feat_sha, head_ref="feat", base_ref="main", workdir=tmp_path / "work",
                    remote_url=str(origin), token=None, llm=llm, model="m", max_tokens=1000,
                    on_commit=lambda sha, files: (marks if marks is not None else []).append((sha, files)))


def resolution(content="a = 0\nx = 2  # feat\nx3 = 3  # main\nz = 9\n"):
    return [tool("submit_resolution", content=content, summary="kept both")]


@pytest.fixture(autouse=True)
def tests_cmd(monkeypatch):
    def set_cmd(cmd):
        monkeypatch.setenv("AOA_TEST_COMMANDS", json.dumps({REPO: cmd}))
    set_cmd("true")
    return set_cmd


async def test_resolves_commits_tests_and_pushes_on_top_of_the_reviewed_commit(tmp_path, remote, tests_cmd, monkeypatch):
    origin, sha, _ = remote
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    # the test run must see no token or key from the worker's environment
    tests_cmd("test -f app.py && ! env | grep -i -E 'token|key'")
    llm, marks = FakeLLM([resolution()]), []
    out = await resolver(tmp_path, remote, llm, marks=marks).run()
    assert out["status"] == "resolved", out
    assert marks == [(out["new_sha"], ["app.py"])]  # marked for the reviewer (before the push)
    assert out["files"] == ["app.py"] and out["tests"].startswith("passed")
    assert git(origin, "rev-parse", "feat") == out["new_sha"]
    assert git(origin, "rev-parse", "feat^1") == sha  # pinned: the merge sits on the reviewed commit
    assert git(origin, "rev-parse", "feat^2") == git(origin, "rev-parse", "main")
    assert git(origin, "show", "feat:app.py") == "a = 0\nx = 2  # feat\nx3 = 3  # main\nz = 9"
    assert COMMIT_TAG in git(origin, "log", "-1", "--format=%B", "feat")
    assert git(origin, "rev-parse", "main") != out["new_sha"]  # nothing reaches main
    prompt = llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "<ours>" in prompt and "x = 2  # feat" in prompt and "x = 3  # main" in prompt
    assert not (tmp_path / "work").exists()  # work dir cleaned up


async def test_leftover_markers_mean_no_push(tmp_path, remote):
    origin, sha, _ = remote
    out = await resolver(tmp_path, remote, FakeLLM([resolution("<<<<<<< HEAD\nx = 2\n=======\nx = 3\n>>>>>>> main\n")])).run()
    assert out["status"] == "failed" and "conflict markers" in out["reason"]
    assert git(origin, "rev-parse", "feat") == sha


async def test_failing_tests_mean_no_push(tmp_path, remote, tests_cmd):
    origin, sha, _ = remote
    tests_cmd("echo boom; exit 1")
    out = await resolver(tmp_path, remote, FakeLLM([resolution()])).run()
    assert out["status"] == "failed" and "tests failed" in out["reason"] and "boom" in out["reason"]
    assert git(origin, "rev-parse", "feat") == sha


async def test_cut_off_or_missing_answer_aborts(tmp_path, remote):
    cut = {"content": resolution()[0:1], "stop_reason": "max_tokens"}
    out = await resolver(tmp_path, remote, FakeLLM([cut])).run()
    assert out["status"] == "failed" and "cut off" in out["reason"]
    out = await resolver(tmp_path, remote, FakeLLM([text("here you go")])).run()
    assert out["status"] == "failed" and "did not submit" in out["reason"]


async def test_modify_delete_conflict_needs_a_human(tmp_path, remote):
    origin, sha, seed = remote
    git(seed, "rm", "-q", "app.py")
    git(seed, "commit", "-qm", "drop app")
    git(seed, "push", "-q", "origin", "main")
    llm = FakeLLM([])
    out = await resolver(tmp_path, remote, llm).run()
    assert out["action"] == "needs_human" and "needs a human" in out["reason"] and not llm.calls


async def test_moved_branch_is_stale_and_a_retry_after_push_is_recognised(tmp_path, remote):
    origin, sha, seed = remote
    first = await resolver(tmp_path, remote, FakeLLM([resolution()])).run()
    assert first["status"] == "resolved"
    again = await resolver(tmp_path, remote, FakeLLM([])).run()  # e.g. the worker died after pushing
    assert again["status"] == "resolved" and again["already"] and again["new_sha"] == first["new_sha"]
    assert again["files"] == ["app.py"]  # recovered from the commit message, so the reviewer is still told
    old = git(origin, "rev-parse", f"{sha}^")
    stale = await resolver(tmp_path, remote, FakeLLM([]), sha=old).run()
    assert stale["status"] == "failed" and "moved" in stale["reason"] and stale["action"] == "stale"


def test_token_only_in_git_config_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    env = git_env("ghp_secret")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_secret" not in json.dumps(env)  # only its base64 form, inside the header value
    assert set(safe_env()) <= {"PATH", "HOME", "LANG", "TMPDIR"}


def test_test_command_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("AOA_TEST_COMMANDS", raising=False)
    assert repo_test_command(REPO, tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "tests").mkdir()
    assert "pytest" in repo_test_command(REPO, tmp_path)
    (tmp_path / ".aoa").mkdir()
    (tmp_path / ".aoa" / "test-command").write_text("true\n")  # in the merged tree: ignored
    assert "pytest" in repo_test_command(REPO, tmp_path)
    assert repo_test_command(REPO, tmp_path, base_file="make test\n") == "make test"
    monkeypatch.setenv("AOA_TEST_COMMANDS", json.dumps({REPO: ""}))
    assert repo_test_command(REPO, tmp_path, base_file="make test") is None


async def test_a_pr_cannot_choose_its_own_test_command(tmp_path, remote, monkeypatch):
    origin, _, seed = remote
    monkeypatch.delenv("AOA_TEST_COMMANDS")
    git(seed, "checkout", "-q", "main")
    (seed / ".aoa").mkdir()
    (seed / ".aoa" / "test-command").write_text("echo base-gate; exit 1\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "gate")
    git(seed, "push", "-q", "origin", "main")
    out = await resolver(tmp_path, remote, FakeLLM([resolution()])).run()
    assert out["status"] == "failed" and "base-gate" in out["reason"]  # the base branch's gate applies


async def test_symlink_and_non_utf8_conflicts_need_a_human(tmp_path, remote):
    origin, _, seed = remote
    for name, base, ours, theirs, why in (("link", None, "a", "b", "symlink"),
                                          ("latin.txt", b"x\n", b"caf\xe9 1\n", b"caf\xe9 2\n", "not UTF-8")):
        git(seed, "checkout", "-q", "main")
        git(seed, "checkout", "-q", "-B", f"t-{name}")
        p = seed / name
        if base is None:
            p.symlink_to("base-target")
        else:
            p.write_bytes(base)
        git(seed, "add", ".")
        git(seed, "commit", "-qm", "base")
        git(seed, "checkout", "-q", "-B", f"h-{name}")
        p.unlink()
        p.symlink_to(ours) if base is None else p.write_bytes(ours)
        git(seed, "commit", "-qam", "ours")
        git(seed, "checkout", "-q", f"t-{name}")
        p.unlink()
        p.symlink_to(theirs) if base is None else p.write_bytes(theirs)
        git(seed, "commit", "-qam", "theirs")
        git(seed, "push", "-q", "origin", f"t-{name}", f"h-{name}")
        r = Resolver(repo=REPO, sha=git(seed, "rev-parse", f"h-{name}"), head_ref=f"h-{name}", base_ref=f"t-{name}",
                     workdir=tmp_path / f"w-{name}", remote_url=str(origin), token=None, llm=FakeLLM([]),
                     model="m", max_tokens=1000)
        out = await r.run()
        assert out["status"] == "failed" and why in out["reason"], (name, out)


async def test_never_pushes_onto_a_protected_head_and_rejects_gutted_resolutions(tmp_path, remote):
    origin, sha, _ = remote
    r = resolver(tmp_path, remote, FakeLLM([]))
    r.protected_refs.add("feat")
    out = await r.run()
    assert out["action"] == "needs_human" and "base or default branch" in out["reason"]
    out = await resolver(tmp_path, remote, FakeLLM([resolution("x\n")])).run()
    assert out["status"] == "failed" and "much shorter" in out["reason"]
    assert git(origin, "rev-parse", "feat") == sha
