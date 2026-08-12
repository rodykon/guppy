"""Scheduler entrypoint: poll configured repos on an interval, dispatch
qualifying issues to ephemeral worker containers, bounded by
max_concurrent_jobs. See DESIGN.md for the full architecture."""

from __future__ import annotations

import logging
import os
import time

from guppy.common.config import GuppyConfig, RepoConfig, load_config, resolve_secret
from guppy.common.github_client import GitHubClient
from guppy.common.store import Job, Store
from guppy.scheduler.dispatcher import Dispatcher
from guppy.scheduler.launcher import DockerSocketLauncher, WorkerJobSpec
from guppy.scheduler.poller import Poller

logger = logging.getLogger("guppy.scheduler")

DEFAULT_CONFIG_PATH = "/config/config.yaml"
DEFAULT_DATA_VOLUME = "guppy-data"


def _build_spec(config: GuppyConfig, repo: RepoConfig, job: Job) -> WorkerJobSpec:
    return WorkerJobSpec(
        job_id=job.id,
        repo_slug=job.repo_slug,
        issue_number=job.issue_number,
        base_branch=repo.base_branch,
        worker_image=repo.effective_worker_image(config.settings.default_worker_image),
        setup_commands=repo.setup_commands,
        turn_budgets=repo.effective_turn_budgets(config.settings.default_turn_budgets),
        github_token=resolve_secret(repo.github_token_env),
        anthropic_api_key=resolve_secret(config.settings.anthropic_api_key_env),
        sqlite_path=config.settings.sqlite_path,
    )


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("GUPPY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = os.environ.get("GUPPY_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    config = load_config(config_path)

    store = Store(config.settings.sqlite_path)
    clients = {
        repo.slug: GitHubClient(resolve_secret(repo.github_token_env), repo.slug)
        for repo in config.repos
    }
    repo_by_slug = {repo.slug: repo for repo in config.repos}

    launcher = DockerSocketLauncher(
        docker_socket_path=config.settings.docker_socket,
        data_volume_name=os.environ.get("GUPPY_DATA_VOLUME", DEFAULT_DATA_VOLUME),
    )
    poller = Poller(config, store, clients)
    dispatcher = Dispatcher(store, launcher, config.settings.max_concurrent_jobs)

    logger.info(
        "guppy scheduler starting: %d repo(s), poll every %ds, max %d concurrent job(s)",
        len(config.repos),
        config.settings.poll_interval_seconds,
        config.settings.max_concurrent_jobs,
    )

    dispatcher.reconcile_on_startup()

    while True:
        try:
            poller.poll_all()
        except Exception:
            logger.exception("poll cycle failed")

        try:
            dispatcher.poll_running()
            dispatcher.launch_ready_jobs(lambda job: _build_spec(config, repo_by_slug[job.repo_slug], job))
        except Exception:
            logger.exception("dispatch cycle failed")

        time.sleep(config.settings.poll_interval_seconds)


if __name__ == "__main__":
    run()
