"""Reviewer identity (#11): App installation tokens, scoped to one repo, cached; off when unconfigured."""
from __future__ import annotations

import json

import httpx
import pytest

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from aoa import gh_reviewer  # noqa: E402
from aoa.github_monitor import GithubError  # noqa: E402

REPO = "agileresearchservices/demo"


@pytest.fixture
def app(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "app.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    monkeypatch.setenv("AOA_REVIEWER_APP_ID", "12345")
    monkeypatch.setenv("AOA_REVIEWER_APP_KEY_FILE", str(pem))
    monkeypatch.setattr(gh_reviewer, "_cache", {})
    return key.public_key()


def github(calls, expires="2099-01-01T00:00:00Z"):
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77})
        if req.url.path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json={"token": "ghs_inst", "expires_at": expires})
        return httpx.Response(404, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_off_by_default(monkeypatch):
    monkeypatch.setenv("AOA_GITHUB_USER", "agileresearchservices")
    assert not gh_reviewer.configured() and await gh_reviewer.reviewer_token(REPO) is None
    monkeypatch.setenv("AOA_REVIEWER_TOKEN", "ghp_second")
    assert await gh_reviewer.reviewer_token(REPO) == "ghp_second"


async def test_app_token_is_scoped_to_one_repo_and_cached(app):
    calls = []
    async with github(calls) as http:
        assert await gh_reviewer.reviewer_token(REPO, http) == "ghs_inst"
        assert await gh_reviewer.reviewer_token(REPO, http) == "ghs_inst"
    assert len(calls) == 2  # the second review reused the cached token
    bearer = calls[0].headers["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(bearer, app, algorithms=["RS256"])
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
    assert json.loads(calls[1].content) == {"repositories": ["demo"], "permissions": {"pull_requests": "write"}}


async def test_expiring_token_is_renewed_and_failures_mean_none(app, monkeypatch, tmp_path):
    calls = []
    async with github(calls, expires="2000-01-01T00:00:00Z") as http:
        await gh_reviewer.reviewer_token(REPO, http)
        await gh_reviewer.reviewer_token(REPO, http)
    assert len(calls) == 4
    async with github([]) as http:
        assert await gh_reviewer.reviewer_token("agileresearchservices/other", http) is None  # not installed: 404
    monkeypatch.setenv("AOA_REVIEWER_APP_KEY_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(gh_reviewer, "_cache", {})
    assert await gh_reviewer.reviewer_token(REPO) is None


async def test_out_of_scope_repo_is_refused(app):
    with pytest.raises(GithubError):
        await gh_reviewer.reviewer_token("nasuni/hyrule")
