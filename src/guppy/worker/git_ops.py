"""Local git operations for the worker's clone -- plain subprocess calls.

Talking to the GitHub API (PR creation, comments) is github_client.py's
job; this module only ever runs `git` against the local checkout.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _redact(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


def _run(args: list[str], cwd: Path, *, redact: str | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        # git error output often echoes the remote URL, which for us embeds
        # the repo-scoped access token -- redact before it hits logs/DB.
        raise GitError(_redact(f"git {' '.join(args)} failed:\n{result.stderr}", redact))
    return result.stdout


def clone(clone_url: str, base_branch: str, dest: Path, *, token: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["clone", "--branch", base_branch, "--single-branch", clone_url, str(dest)],
        cwd=dest.parent,
        redact=token,
    )


def configure_identity(repo_dir: Path) -> None:
    _run(["config", "user.name", "guppy-agent[bot]"], cwd=repo_dir)
    _run(["config", "user.email", "guppy-agent[bot]@users.noreply.github.com"], cwd=repo_dir)


def has_changes(repo_dir: Path) -> bool:
    return bool(_run(["status", "--porcelain"], cwd=repo_dir).strip())


def create_branch(repo_dir: Path, branch: str) -> None:
    _run(["checkout", "-b", branch], cwd=repo_dir)


def commit_all(repo_dir: Path, message: str) -> None:
    _run(["add", "-A"], cwd=repo_dir)
    _run(["commit", "-m", message], cwd=repo_dir)


def push(repo_dir: Path, branch: str, *, token: str | None = None) -> None:
    _run(["push", "origin", branch], cwd=repo_dir, redact=token)


def full_diff(repo_dir: Path, base_branch: str) -> str:
    return _run(["diff", base_branch], cwd=repo_dir)


def slugify_title(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "issue"
