"""WorkerLauncher: starts one ephemeral container per job.

`DockerSocketLauncher` is today's implementation (DooD: talks to the host
Docker daemon over a mounted socket). Per DESIGN.md, the abstraction is
what buys portability -- a future cloud port (Kubernetes Job, ECS task,
Cloud Run job) implements the same `WorkerLauncher` interface without
touching the dispatcher that calls it.

Note on the shared SQLite volume: it must be attached to sibling
containers by *Docker volume name*, not by a host filesystem path. Under
DooD, a bind-mount path given to `containers.run` is resolved by the host
daemon against the *host's* filesystem, not the scheduler container's --
a path that only exists inside the scheduler (e.g. because the scheduler
itself was started with `-v /host/data:/data`) would not resolve for a
sibling container. A named volume sidesteps this: the daemon resolves it
consistently regardless of which container asked.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import docker
from docker.errors import NotFound as DockerNotFound

from guppy.common.config import TurnBudgets

DATA_MOUNT_PATH = "/data"


@dataclass
class WorkerJobSpec:
    job_id: str
    repo_slug: str
    issue_number: int
    base_branch: str
    worker_image: str
    setup_commands: list[str]
    turn_budgets: TurnBudgets
    github_token: str
    anthropic_api_key: str
    sqlite_path: str  # path *inside the worker container*, e.g. /data/guppy.db

    def to_env(self) -> dict[str, str]:
        return {
            "GUPPY_JOB_ID": self.job_id,
            "GUPPY_REPO_SLUG": self.repo_slug,
            "GUPPY_ISSUE_NUMBER": str(self.issue_number),
            "GUPPY_BASE_BRANCH": self.base_branch,
            "GUPPY_SETUP_COMMANDS": json.dumps(self.setup_commands),
            "GUPPY_TURN_BUDGET_PLANNER": str(self.turn_budgets.planner),
            "GUPPY_TURN_BUDGET_PLAN_REVIEWER": str(self.turn_budgets.plan_reviewer),
            "GUPPY_TURN_BUDGET_IMPLEMENTER": str(self.turn_budgets.implementer),
            "GUPPY_TURN_BUDGET_CODE_REVIEWER": str(self.turn_budgets.code_reviewer),
            "GITHUB_TOKEN": self.github_token,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "GUPPY_SQLITE_PATH": self.sqlite_path,
        }


class WorkerPhase(str, Enum):
    RUNNING = "running"
    EXITED_OK = "exited_ok"
    EXITED_ERROR = "exited_error"


@dataclass
class WorkerRunState:
    phase: WorkerPhase
    exit_code: int | None = None
    logs_tail: str | None = None


class WorkerLauncher(ABC):
    @abstractmethod
    def launch(self, spec: WorkerJobSpec) -> str:
        """Starts a worker for this job. Returns an opaque handle used by
        poll()/cleanup()."""

    @abstractmethod
    def poll(self, handle: str) -> WorkerRunState:
        """Current state of a previously-launched worker."""

    @abstractmethod
    def cleanup(self, handle: str) -> None:
        """Removes a finished worker's container/resources. Safe to call
        more than once."""

    @abstractmethod
    def discover_active(self) -> dict[str, str]:
        """Maps job_id -> handle for any workers this launcher started that
        are still around (running or exited-but-not-yet-cleaned-up). Used
        to reattach after a scheduler restart instead of losing track of
        in-flight jobs."""


class DockerSocketLauncher(WorkerLauncher):
    def __init__(self, docker_socket_path: str, data_volume_name: str):
        self._client = docker.DockerClient(base_url=f"unix://{docker_socket_path}")
        self._data_volume_name = data_volume_name

    def launch(self, spec: WorkerJobSpec) -> str:
        container = self._client.containers.run(
            spec.worker_image,
            environment=spec.to_env(),
            volumes={self._data_volume_name: {"bind": DATA_MOUNT_PATH, "mode": "rw"}},
            detach=True,
            name=f"guppy-job-{spec.job_id}",
            labels={
                "guppy.job_id": spec.job_id,
                "guppy.repo_slug": spec.repo_slug,
                "guppy.issue_number": str(spec.issue_number),
            },
        )
        return container.id

    def poll(self, handle: str) -> WorkerRunState:
        try:
            container = self._client.containers.get(handle)
        except DockerNotFound:
            # Container vanished (e.g. manually removed) -- treat as an
            # error state so the dispatcher doesn't wait on it forever.
            return WorkerRunState(phase=WorkerPhase.EXITED_ERROR, logs_tail="container not found")

        container.reload()
        if container.status in ("running", "created", "restarting"):
            return WorkerRunState(phase=WorkerPhase.RUNNING)

        exit_code = container.attrs.get("State", {}).get("ExitCode", -1)
        logs_tail = container.logs(tail=200).decode("utf-8", errors="replace")
        phase = WorkerPhase.EXITED_OK if exit_code == 0 else WorkerPhase.EXITED_ERROR
        return WorkerRunState(phase=phase, exit_code=exit_code, logs_tail=logs_tail)

    def cleanup(self, handle: str) -> None:
        try:
            self._client.containers.get(handle).remove(force=True)
        except DockerNotFound:
            pass

    def discover_active(self) -> dict[str, str]:
        containers = self._client.containers.list(all=True, filters={"label": "guppy.job_id"})
        return {c.labels["guppy.job_id"]: c.id for c in containers}
