"""GitHub access: issue polling/validation and PR/comment creation.

Used by the scheduler (list + validate open issues, comment on
non-matching ones) and by workers (comment with the PR link or SKIP
reason, open the PR). Each `GitHubClient` is scoped to one repo with one
token -- per DESIGN.md, workers only ever hold credentials for the repo
they're working on.

Git plumbing (clone, branch, commit, push) is not this module's job -- the
worker does that locally via git subprocess against its own clone. This
module only talks to the GitHub API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from github import Auth, Github
from github.GithubException import GithubException
from github.Issue import Issue

_TYPE_RE = re.compile(r"^##\s+Type\s*\n+\s*(bug|feature)\s*$", re.MULTILINE | re.IGNORECASE)
_DESC_RE = re.compile(r"^##\s+Description\s*\n+(.+?)(?=\n##\s|\Z)", re.MULTILINE | re.DOTALL)
_AC_RE = re.compile(r"^##\s+Acceptance Criteria\s*\n+(?:\s*-\s*\[[ xX]?\]\s*.+\n?)+", re.MULTILINE)
_TESTS_RE = re.compile(
    r"^##\s+Tests(?:\s*\(optional\))?\s*\n+(.+?)(?=\n##\s|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)
# Difficulty gates which pipeline stages run (see worker/pipeline.py). It's
# optional and defaults to "difficult" (today's full pipeline) when the
# section is absent entirely -- but if the section header IS present, its
# value is validated as strictly as ## Type, so a typo fails the whole issue
# rather than silently degrading to a different pipeline than the filer
# intended.
_DIFFICULTY_VALUES = ("trivial", "easy", "medium", "difficult")
_DIFFICULTY_RE = re.compile(
    r"^##\s+Difficulty\s*\n+\s*(" + "|".join(_DIFFICULTY_VALUES) + r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_DIFFICULTY_HEADER_RE = re.compile(r"^##\s+Difficulty\s*\n", re.MULTILINE | re.IGNORECASE)
DEFAULT_DIFFICULTY = "difficult"

REQUIRED_FORMAT_HINT = (
    "Required sections: `## Type` (bug|feature), `## Description`, and "
    "`## Acceptance Criteria` (at least one `- [ ]` item). Optional: "
    "`## Affected Files`, `## Tests`, `## Difficulty` "
    "(trivial|easy|medium|difficult, default difficult)."
)


@dataclass
class ValidationResult:
    valid: bool
    issue_type: str | None = None
    tests_section: str | None = None
    difficulty: str | None = None  # only set when valid; see DEFAULT_DIFFICULTY
    reason: str | None = None  # human-readable, only set when invalid


def validate_issue_body(body: str | None) -> ValidationResult:
    body = body or ""

    missing = []
    has_type = _TYPE_RE.search(body)
    has_desc = _DESC_RE.search(body)
    has_ac = _AC_RE.search(body)
    if not has_type:
        missing.append("a `## Type` section containing exactly `bug` or `feature`")
    if not has_desc:
        missing.append("a non-empty `## Description` section")
    if not has_ac:
        missing.append("a `## Acceptance Criteria` section with at least one `- [ ]` item")

    difficulty_match = _DIFFICULTY_RE.search(body)
    if difficulty_match:
        difficulty = difficulty_match.group(1).lower()
    elif _DIFFICULTY_HEADER_RE.search(body):
        # Header present but value isn't one of the four exact words -- fail
        # the whole issue rather than guessing, same treatment as a bad Type.
        missing.append(
            "a `## Difficulty` value of exactly one of trivial|easy|medium|difficult"
        )
        difficulty = None
    else:
        difficulty = DEFAULT_DIFFICULTY

    if missing:
        reason = (
            "This issue doesn't match the required format. Missing: "
            + "; ".join(missing)
            + f". {REQUIRED_FORMAT_HINT}"
        )
        return ValidationResult(valid=False, reason=reason)

    tests_match = _TESTS_RE.search(body)
    tests_section = tests_match.group(1).strip() if tests_match else None
    return ValidationResult(
        valid=True,
        issue_type=has_type.group(1).lower(),
        tests_section=tests_section,
        difficulty=difficulty,
    )


@dataclass
class QualifyingIssue:
    """An open issue from a whitelisted author, regardless of whether its
    format is valid -- the scheduler still needs invalid ones to post an
    explanatory comment and mark them processed."""

    number: int
    title: str
    body: str
    author: str
    validation: ValidationResult


class GitHubClient:
    def __init__(self, token: str, repo_slug: str):
        self._token = token
        # lazy=True: constructing a client (or a whole dict of them, as the
        # scheduler does for every configured repo at startup) must not
        # itself make an API call. A bad/expired token for one repo should
        # surface as a per-repo polling failure (caught in poller.py), not
        # crash the scheduler before the poll loop even starts.
        self._gh = Github(auth=Auth.Token(token), lazy=True)
        self.repo_slug = repo_slug
        self._repo = self._gh.get_repo(repo_slug)

    def list_open_issues_from(self, allowed_users: Iterable[str]) -> list[QualifyingIssue]:
        allowed = set(allowed_users)
        result = []
        for issue in self._repo.get_issues(state="open"):
            if issue.pull_request is not None:
                continue  # the Issues API also returns PRs; skip those
            author = issue.user.login if issue.user else None
            if author not in allowed:
                continue
            result.append(
                QualifyingIssue(
                    number=issue.number,
                    title=issue.title,
                    body=issue.body or "",
                    author=author,
                    validation=validate_issue_body(issue.body),
                )
            )
        return result

    def get_issue(self, issue_number: int) -> QualifyingIssue:
        """Fetches a single issue by number, regardless of state -- used by
        the worker to re-fetch the issue it was dispatched for (the
        scheduler passes only the issue number, not the body, to keep job
        specs small)."""
        issue = self._repo.get_issue(issue_number)
        author = issue.user.login if issue.user else None
        return QualifyingIssue(
            number=issue.number,
            title=issue.title,
            body=issue.body or "",
            author=author,
            validation=validate_issue_body(issue.body),
        )

    def comment(self, issue_number: int, body: str) -> None:
        self._repo.get_issue(issue_number).create_comment(body)

    def open_pull_request(self, *, head_branch: str, base_branch: str, title: str, body: str) -> str:
        pr = self._repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
        return pr.html_url

    def branch_exists(self, branch: str) -> bool:
        try:
            self._repo.get_branch(branch)
            return True
        except GithubException as e:
            if e.status == 404:
                return False
            raise

    def clone_url_with_token(self) -> str:
        """HTTPS clone URL with the repo-scoped token embedded, for the
        worker's git subprocess calls (clone/push) without needing a
        credential helper."""
        return self._repo.clone_url.replace("https://", f"https://x-access-token:{self._token}@")
