"""Polls configured repos for qualifying issues and enqueues jobs.

Per DESIGN.md: an issue is "processed" the instant a job exists for it,
whether or not that job ever runs a worker. Issues from a whitelisted
author that fail format validation still get a job (immediately marked
SKIPPED) so the explanatory comment is posted exactly once, not every poll
cycle. Issues from non-whitelisted authors are never recorded -- they're
just cheap to re-check every cycle.
"""

from __future__ import annotations

import logging

from guppy.common.config import GuppyConfig, RepoConfig
from guppy.common.github_client import GitHubClient
from guppy.common.store import Store

logger = logging.getLogger(__name__)


class Poller:
    def __init__(self, config: GuppyConfig, store: Store, clients: dict[str, GitHubClient]):
        self._config = config
        self._store = store
        self._clients = clients

    def poll_all(self) -> None:
        for repo in self._config.repos:
            try:
                self._poll_repo(repo)
            except Exception:
                logger.exception("poll failed for repo %s", repo.slug)

    def _poll_repo(self, repo: RepoConfig) -> None:
        client = self._clients[repo.slug]
        for issue in client.list_open_issues_from(repo.allowed_users):
            if self._store.is_issue_processed(repo.slug, issue.number):
                continue

            if not issue.validation.valid:
                job_id = self._store.create_job(repo.slug, issue.number, issue.title)
                reason = issue.validation.reason or "issue does not match the required format"
                try:
                    client.comment(issue.number, f"🤖 {reason}")
                except Exception:
                    logger.exception(
                        "failed to comment on %s#%d about invalid format", repo.slug, issue.number
                    )
                self._store.mark_skipped(job_id, reason)
                logger.info("skipped %s#%d: invalid format", repo.slug, issue.number)
                continue

            job_id = self._store.create_job(repo.slug, issue.number, issue.title)
            logger.info("queued job %s for %s#%d", job_id, repo.slug, issue.number)
