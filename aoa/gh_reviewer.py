"""A separate GitHub identity for PR reviews (#11), so they can APPROVE / REQUEST_CHANGES.

GitHub refuses approve/request-changes from the PR author's own account, so with only GITHUB_TOKEN aoa posts
comment reviews with a stated recommendation. With a reviewer identity configured, the review is a real one:

- a GitHub App (preferred; shows as `<app>[bot]`): AOA_REVIEWER_APP_ID plus a private key file
  (AOA_REVIEWER_APP_KEY_FILE, default ~/.config/aoa/reviewer-app.pem, mode 600, outside the repo and data
  dir). Each review gets an installation token scoped to that one repo with only `pull_requests: write`,
  valid for an hour and cached until shortly before it expires. Needs the `github-app` extra (PyJWT);
- or a second account's token in AOA_REVIEWER_TOKEN.

Off by default: `reviewer_token` returns None when nothing is configured or anything fails (logged), and the
caller falls back to a comment review, so a misconfigured App never makes a review disappear.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

from .github_monitor import API, check_repo_in_scope

log = logging.getLogger(__name__)
DEFAULT_KEY_FILE = "~/.config/aoa/reviewer-app.pem"
_cache: dict[str, tuple[str, float]] = {}  # repo -> (installation token, expiry epoch)
MARGIN = 300


def forget(repo: str) -> None:
    """Drop a cached token GitHub rejected (e.g. the App was uninstalled), so the next review mints anew."""
    _cache.pop(repo, None)


def configured() -> bool:
    return bool(os.environ.get("AOA_REVIEWER_TOKEN") or os.environ.get("AOA_REVIEWER_APP_ID"))


def app_jwt(app_id: str, key_pem: bytes, now: float | None = None) -> str:
    import jwt  # the github-app extra; imported here so a missing package only disables the App path

    now = time.time() if now is None else now
    # iat backdated for clock drift; GitHub caps exp at 10 minutes
    return jwt.encode({"iat": int(now) - 60, "exp": int(now) + 540, "iss": app_id}, key_pem, algorithm="RS256")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


async def _installation_token(repo: str, app_id: str, key_file: Path, http: httpx.AsyncClient) -> str:
    hit = _cache.get(repo)
    if hit and hit[1] - MARGIN > time.time():
        return hit[0]
    bearer = app_jwt(app_id, key_file.read_bytes())
    r = await http.get(f"{API}/repos/{repo}/installation", headers=_headers(bearer))
    r.raise_for_status()
    r = await http.post(f"{API}/app/installations/{r.json()['id']}/access_tokens", headers=_headers(bearer),
                        json={"repositories": [repo.split("/", 1)[1]], "permissions": {"pull_requests": "write"}})
    r.raise_for_status()
    body = r.json()
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
    _cache[repo] = (body["token"], expires)
    return body["token"]


async def reviewer_token(repo: str, http: httpx.AsyncClient | None = None) -> str | None:
    """A token that posts reviews on `repo` as the reviewer identity, or None (then post a comment review)."""
    repo = check_repo_in_scope(repo)
    if tok := os.environ.get("AOA_REVIEWER_TOKEN"):
        return tok
    app_id = os.environ.get("AOA_REVIEWER_APP_ID")
    if not app_id:
        return None
    key_file = Path(os.environ.get("AOA_REVIEWER_APP_KEY_FILE") or DEFAULT_KEY_FILE).expanduser()
    try:
        if http is not None:
            return await _installation_token(repo, app_id, key_file, http)
        async with httpx.AsyncClient(timeout=20) as client:
            return await _installation_token(repo, app_id, key_file, client)
    except ImportError:
        log.warning("AOA_REVIEWER_APP_ID is set but PyJWT is missing: pip install -e '.[github-app]'")
    except OSError as e:
        log.warning("reviewer App key file %s unreadable (%s)", key_file, type(e).__name__)
    except Exception as e:  # noqa: BLE001 - not installed on this repo, bad key, GitHub down: fall back
        log.warning("reviewer App token for %s failed: %s %s", repo, type(e).__name__, str(e)[:200])
    return None
