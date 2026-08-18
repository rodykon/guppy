# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

Guppy watches configured GitHub repositories for issues filed by whitelisted
users in a specific format, and turns them into pull requests automatically.
It runs as a standalone polling service (Docker locally today, portable to
a cloud job runner later), not a GitHub Actions workflow -- an earlier,
simpler design (a single Claude Code CLI call triggered per-issue by
Actions) was built, then deliberately replaced with this one; its files
were removed from the repo (see `git log` if that history is ever needed,
it's not relevant to current work).

Full architecture and the reasoning behind every major decision live in
[`DESIGN.md`](DESIGN.md) — read it before making structural changes, since
most of the "why" was worked out interactively and isn't rederivable from
the code alone. [`TODO.md`](TODO.md) lists what was deliberately deferred
(retry loops between stages, a status dashboard, worker network
allowlisting, re-trigger-on-edit) — these are cut scope, not oversights;
don't "fix" them without the user asking.

## Architecture, in one screen

```
Scheduler (long-running container, src/guppy/scheduler/)
  poller.py    -> lists open issues per repo, validates format, enqueues
                  jobs in SQLite (an issue is "processed" the moment a job
                  exists for it, valid or not -- see poller.py docstring)
  dispatcher.py -> bounded-concurrency launch of queued jobs, reconciles
                   worker state on startup, fails jobs whose container
                   exited without reporting a result
  launcher.py   -> WorkerLauncher interface + DockerSocketLauncher (DooD).
                   Shared state volume MUST be a named Docker volume, not
                   a host path -- see the module docstring for why

Worker (one per job, src/guppy/worker/, torn down after)
  main.py      -> entrypoint: clone (repo-scoped token), run setup
                  commands, read .guppy/context.md, run the pipeline,
                  push + open PR or comment + SKIP. REPO_DIR (/work/repo)
                  is deliberately ephemeral -- the clone doesn't need to
                  outlive the job. ARTIFACTS_ROOT must NOT follow that
                  pattern: it's under /data (the shared volume), not
                  /work, because the paths written there get recorded in
                  SQLite and read back after the container is gone. Got
                  this wrong once already (artifacts written under /work
                  vanished with the container the moment it was cleaned
                  up, leaving dangling paths in the store) -- if you're
                  adding a new kind of worker output that needs to
                  outlive the job, it goes under /data, not /work.
  pipeline.py  -> the (up to) 4-stage Claude Agent SDK orchestration --
                  which stages actually run is gated by the issue's
                  optional `## Difficulty` (see below)
  prompts.py   -> per-stage prompt construction, shared SKIP contract
  git_ops.py   -> subprocess git only; redacts the embedded token from any
                  raised error before it can reach logs/SQLite

Shared (src/guppy/common/, used by both sides)
  config.py        -> pydantic Settings/RepoConfig/TurnBudgets, load_config()
  store.py         -> SQLite Store (WAL mode), Job/JobStatus
  github_client.py -> issue polling/validation, PR/comment creation
```

Pipeline stages (`planner` -> `plan_reviewer` -> `implementer` ->
`code_reviewer`) run **single-pass, no retries**: any stage can end the
job by making "SKIP: <reason>" its entire final message, checked
uniformly in `pipeline._check_skip`. Tool scoping (`READ_ONLY_TOOLS` for
the first two stages, `FULL_TOOLS` for the last two) is the actual safety
boundary, not the SKIP convention — a planner literally cannot call
`Edit`/`Write`/`Bash` regardless of what a prompt says.

The optional `## Difficulty` issue section (`trivial|easy|medium|difficult`,
default `difficult` = today's unchanged full pipeline) picks a *subset* of
those four stages via `pipeline._DIFFICULTY_STAGE_FLAGS` — implementer
always runs; trivial/easy skip the planner entirely, easy/medium skip
plan_reviewer. No escalation path exists if a reduced pipeline turns out
insufficient (same SKIP escape hatch as always). A present-but-invalid
`## Difficulty` value fails the whole issue's validation, same as a bad
`## Type`.

Turn budgets **do** vary by difficulty: `RepoConfig.difficulty_turn_budgets`
(keyed by difficulty) layers on top of the repo's already-resolved
default+repo-override `TurnBudgets`, resolved once in
`scheduler/main.py::_build_spec` using the job's persisted `difficulty` —
the worker itself stays difficulty-budget-agnostic, same as before. See
`DESIGN.md`'s Issue format section for why this went from deliberately
deferred to implemented within the same day (`rodykon/tempo#6` hit the flat
implementer budget almost immediately).

Test generation is unconditional: the implementer's prompt always requires
tests, whether or not the issue's optional `## Tests` section is filled
in, **at every difficulty**. If present, its content is passed through as
required coverage in addition to whatever else the agent decides to test.

## Claude Agent SDK facts worth not re-deriving

Verified against the installed `claude-agent-sdk` package (confirm current
behavior with `python3 -c "import inspect; from claude_agent_sdk import
ClaudeAgentOptions; print(inspect.signature(ClaudeAgentOptions))"` if this
ever seems stale):

- Entrypoint is the async `query(prompt=..., options=ClaudeAgentOptions(...))`
  generator, not a client class, for the one-shot-per-stage use this
  pipeline needs.
- `ClaudeAgentOptions` has a real `cwd` field — use it, don't `os.chdir()`
  (an earlier draft did; fixed, see `pipeline.run_stage`).
- Tool scoping is `allowed_tools`/`disallowed_tools` (plain lists of tool
  names); non-interactive auto-approval is `permission_mode="acceptEdits"`.
- Turn budget is `max_turns`; on exhaustion the loop yields a final
  `ResultMessage` first (detect it via `ResultMessage.terminal_reason ==
  "max_turns"`, with `subtype == "error_max_turns"` kept as a fallback for
  older CLI versions that don't set `terminal_reason` — don't rely on
  `subtype` alone) -- **but this earlier note that the loop "does not
  raise" was wrong**, confirmed live against a real turn-limit hit
  (`rodykon/tempo#6`, 2026-08-15): the installed CLI exits non-zero on
  purpose whenever it reports any `is_error` result (`error_max_turns`,
  `error_during_execution`, ...), and the SDK's transport then raises a
  trailing `ProcessError` on the *next* read after already delivering that
  ResultMessage (see `claude_agent_sdk/_internal/query.py`). `run_stage`
  now catches `ProcessError` and treats it as expected/harmless once a
  `ResultMessage` has already been parsed — re-raises only if no
  `ResultMessage` was ever seen. Any future stage-level "why didn't this
  error surface" debugging should start here, not from the assumption the
  loop can't raise.
- `ANTHROPIC_API_KEY` is read from the environment automatically.

## Commands

No test suite is checked in yet — verification so far was ad hoc (scratch
scripts run against a venv with `pydantic`, `PyYAML`, `PyGithub`, `docker`,
and `claude-agent-sdk` installed, exercising: config loading against
`config.example.yaml`; the SQLite store's full job lifecycle including a
second connection reopening the same file, simulating scheduler+worker
sharing the volume; issue-format validation edge cases; `git_ops` against
a real local repo including token-redaction-on-failure; and the 4-stage
pipeline's control flow — success, SKIP, and turn-limit paths — via a
monkeypatched `query()`). None of that is committed as pytest, and none of
it exercises live Docker container launching or a real Anthropic API call
end-to-end — only unit-level logic has been verified.

```bash
# Install for local development
pip install -e ".[dev]"

# Syntax/import check any module
python3 -c "import guppy.scheduler.main"

# Validate config.example.yaml against the schema
python3 -c "from guppy.common.config import load_config; load_config('config.example.yaml')"

# Build images (worker is not a compose service -- see docker-compose.yml comment)
docker compose build scheduler
docker build -f Dockerfile.worker -t guppy-worker:latest .

# Run
docker compose up -d scheduler
docker compose logs -f scheduler
```

When touching `pipeline.py`, the fastest way to verify stage-sequencing
changes without spending API calls is monkeypatching
`guppy.worker.pipeline.query` with a fake async generator yielding
`AssistantMessage`/`ResultMessage` objects — that's how the control flow
(SKIP detection, artifact writes, turn-limit handling) was verified during
initial implementation. Real dataclass field names for these were pulled
from the installed package via `inspect`/`dataclasses.fields`, not assumed.
