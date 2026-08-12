"""Config schema for guppy.

Two layers: global Settings (poll interval, concurrency cap, defaults) and a
list of RepoConfig entries (one per watched repo). Secrets are never stored
in config directly -- each secret-bearing field names an environment
variable, resolved at the point of use via `resolve_secret`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_WORKER_IMAGE = "guppy-worker:latest"

PIPELINE_STAGES = ("planner", "plan_reviewer", "implementer", "code_reviewer")


class TurnBudgets(BaseModel):
    """Max agent turns per pipeline stage. See DESIGN.md: per-stage budgets,
    global default overridable per repo."""

    planner: int = 15
    plan_reviewer: int = 10
    implementer: int = 30
    code_reviewer: int = 20

    def merged_with_override(self, override: "TurnBudgets | None") -> "TurnBudgets":
        if override is None:
            return self
        data = self.model_dump()
        data.update(override.model_dump(exclude_unset=True))
        return TurnBudgets(**data)


class RepoConfig(BaseModel):
    """One watched repository.

    `slug` is `owner/name`. `github_token_env` names the environment
    variable holding a fine-grained PAT scoped to *only this repo* --
    workers only ever receive credentials for the repo they're working on.
    """

    slug: str
    github_token_env: str
    allowed_users: list[str] = Field(min_length=1)
    base_branch: str = "dev"
    worker_image: str | None = None
    setup_commands: list[str] = Field(default_factory=list)
    turn_budgets: TurnBudgets | None = None

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if v.count("/") != 1 or not all(v.split("/")):
            raise ValueError(f"repo slug must be 'owner/name', got {v!r}")
        return v

    def effective_worker_image(self, default_image: str) -> str:
        return self.worker_image or default_image

    def effective_turn_budgets(self, default_budgets: TurnBudgets) -> TurnBudgets:
        return default_budgets.merged_with_override(self.turn_budgets)


class Settings(BaseModel):
    """Global scheduler settings."""

    poll_interval_seconds: int = 300
    max_concurrent_jobs: int = 2
    default_worker_image: str = DEFAULT_WORKER_IMAGE
    default_turn_budgets: TurnBudgets = Field(default_factory=TurnBudgets)
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    sqlite_path: str = "/data/guppy.db"
    docker_socket: str = "/var/run/docker.sock"


class GuppyConfig(BaseModel):
    settings: Settings = Field(default_factory=Settings)
    repos: list[RepoConfig] = Field(min_length=1)

    @field_validator("repos")
    @classmethod
    def _validate_unique_slugs(cls, v: list[RepoConfig]) -> list[RepoConfig]:
        seen = set()
        for repo in v:
            if repo.slug in seen:
                raise ValueError(f"duplicate repo slug in config: {repo.slug}")
            seen.add(repo.slug)
        return v

    def get_repo(self, slug: str) -> RepoConfig | None:
        return next((r for r in self.repos if r.slug == slug), None)


def load_config(path: str | Path) -> GuppyConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return GuppyConfig.model_validate(raw or {})


class MissingSecretError(RuntimeError):
    def __init__(self, env_var: str):
        super().__init__(
            f"required environment variable {env_var!r} is not set"
        )
        self.env_var = env_var


def resolve_secret(env_var: str) -> str:
    value = os.environ.get(env_var)
    if not value:
        raise MissingSecretError(env_var)
    return value
