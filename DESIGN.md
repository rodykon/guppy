# Guppy v2: Solution Design

## Goal

Guppy watches configured GitHub repositories for issues filed by whitelisted
users in a specific format, and turns them into pull requests automatically.
Unlike v1 (a GitHub Actions workflow triggered per-issue, retired — see
`git log` for that history), v2 is a standalone service you run yourself
(locally in Docker today, portable to a cloud job runner later): it polls on
an interval instead of reacting to webhooks, and it runs each issue through
a four-stage Claude agent pipeline (plan → review plan → implement → review
code) instead of a single Claude Code CLI call.

A human still reviews and merges every PR. The agent never pushes to `main`
and never auto-merges.

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│ Scheduler container (long-running)                           │
│                                                                │
│  Poll loop (every N seconds, per repo in config):             │
│    1. List open issues via GitHub API                         │
│    2. Filter: author in repo's whitelist? body matches         │
│       template (## Type, ## Description, ## Acceptance         │
│       Criteria; optional ## Affected Files, ## Tests)?         │
│    3. Already processed (repo + issue number in SQLite)?       │
│       -> skip, each issue is processed at most once, ever      │
│    4. New qualifying issue -> enqueue a job                    │
│                                                                │
│  Dispatcher (bounded concurrency, configurable cap):           │
│    - Pulls queued jobs, launches one worker container per job  │
│      via WorkerLauncher (Docker socket, sibling container)     │
│    - Enforces the concurrency cap; excess jobs stay queued      │
│                                                                │
│  State: SQLite file on a mounted volume                        │
│    - processed_issues(repo, issue_number, job_id, ...)         │
│    - jobs(id, repo, issue_number, status, stage, artifact       │
│      paths, timestamps, ...)                                   │
│    - shared with worker containers via the same mounted volume  │
│      (WAL mode + busy_timeout so concurrent writers are safe)   │
└─────────────────────────────────────────────────────────────┘
                 │ docker.sock (DooD)
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ Worker container (ephemeral, one per job, torn down after)    │
│                                                                │
│  1. Clone the target repo fresh, using a token scoped to only  │
│     this repo (per-repo fine-grained PAT from config)          │
│  2. Read the repo's context file (.guppy/context.md, checked   │
│     into the target repo)                                      │
│  3. Run the pipeline, single pass, each stage turn-budgeted:    │
│       Planner        (read-only tools)                         │
│         -> plan.md                                              │
│       Plan reviewer   (read-only tools)                         │
│         -> critiques + directly fixes the plan -> plan.md       │
│       Implementer     (Read/Edit/Write/Bash)                    │
│         -> code + tests per the final plan                      │
│       Code reviewer   (Read/Edit/Write/Bash)                    │
│         -> runs tests, applies fixes directly                   │
│     Any stage may emit SKIP -> abort, no PR, comment posted      │
│     explaining why human intervention is needed                 │
│  4. On success: branch (agent/<issue>-<slug>), commit, push      │
│     using the same per-repo token, open PR against the repo's    │
│     configured base branch (default `dev`)                       │
│  5. Comment on the issue with the PR link (or the SKIP reason)   │
│  6. Write final status + artifact paths (plan, review notes,     │
│     diff, logs) to the shared SQLite store, keyed by job id      │
└─────────────────────────────────────────────────────────────┘
```

---

## Key decisions and why

These were worked through interactively; recorded here so the reasoning
doesn't have to be reconstructed later.

| Decision | Choice | Why |
|---|---|---|
| Platform | GitHub only | No current need for GitLab; adding it later is a contained abstraction, not a rewrite. |
| Language | Python | Continuity with v1's embedded scripts; mature ecosystem for scheduling, SQLite, Docker SDK. |
| Agent execution | Claude Agent SDK, in-process | Structured messages and per-stage tool scoping instead of scraping CLI stdout; worth the extra engineering for a real service. |
| Isolation | Scheduler + ephemeral per-job worker containers | Contains a compromised/misbehaving worker's blast radius (credentials, disk, runaway processes) to a single disposable container; the natural shape for a future move to Kubernetes Jobs / Cloud Run. |
| Worker launch | Docker socket mount (DooD), behind a `WorkerLauncher` interface | Simplest local mechanism; the interface is what actually buys portability; a cloud port swaps the implementation, not the caller. |
| State store | SQLite on a mounted volume | Single scheduler process, no need for a network DB; one file to back up. |
| Concurrency | Bounded parallel workers, configurable cap | A slow job shouldn't block unrelated issues; isolation work only pays off if jobs actually run concurrently. |
| Worker environment | Per-repo configurable base image + setup commands, generic default fallback | Repos need different runtimes; an explicit override beats heuristic guessing when no one's watching the run live. |
| GitHub credentials | Per-repo token (fine-grained PAT scoped to one repo) | Consistent with the isolation goal — a compromised worker can't use its credentials against a different configured repo. |
| Anthropic credentials | Single global API key | No meaningful per-repo scoping concept; it's a billing key, not an access-control boundary. |
| Context file location | Inside the target repo (`.guppy/context.md`) | Evolves with the code, visible in normal PR review, no separate mount/fetch plumbing. |
| Pipeline shape | Strict single pass per stage, no retry loops | Matches the described flow; simple state machine; predictable cost/time per job. Bounded retry deferred — see `TODO.md`. |
| Failure handling | SKIP escape hatch at any stage | Carried over from v1: abort with an explanatory comment rather than push a bad result. |
| Cost control | Per-stage turn budget, global default overridable per repo | Bounds worst-case spend per job now that jobs run concurrently; large repos can get a higher cap without raising it for everyone. |
| Re-processing | Each qualifying issue processed exactly once, ever | Dedup by (repo, issue number) is a one-line check; a SKIPped issue can always be closed and reopened if reworked. Re-trigger-on-edit deferred — see `TODO.md`. |
| Observability | Structured logs + SQLite/disk artifacts, no dashboard | The store already holds what a dashboard would read; presentation layer deferred — see `TODO.md`. |
| Worker networking | Unrestricted | The threat model here is adversarial content in a repo you already trust with push access, not an arbitrary untrusted third party; egress allowlisting deferred — see `TODO.md`. |

---

## Issue format

Carried over from v1's spec, plus one addition (`## Difficulty`, see below):

```markdown
## Type
bug | feature

## Description
<clear description of the problem or feature>

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Affected Files (optional)
- path/to/file.ts

## Tests (optional)
- Specific test scenario or edge case you want covered

## Difficulty (optional)
trivial | easy | medium | difficult
```

`Type`, `Description`, and `Acceptance Criteria` are required. `Tests` is
optional but doesn't gate whether tests get written — the implementer stage
always writes tests at every difficulty; if the section is filled in, those
scenarios are passed through as required coverage in addition to whatever
else the agent decides to test.

`Difficulty` gates which of the four pipeline stages run, to avoid running
the full plan → review-plan → implement → review-code sequence on trivial
issues:

| Difficulty | Stages |
|---|---|
| `trivial` | implementer only |
| `easy` | implementer, code_reviewer |
| `medium` | planner, implementer, code_reviewer |
| `difficult` | all four (unchanged) |

It's optional and defaults to `difficult` when absent, so pre-existing
issue-filing habits don't silently change behavior. Validated exactly as
strictly as `Type` when present: an unrecognized value (typo, wrong casing
beyond what's normalized, etc.) fails the whole issue rather than silently
falling back to a default, consistent with how a bad `Type` behaves today
— since re-trigger-on-edit is deferred (see `TODO.md`), a rejected issue
needs a fresh issue to retry, not an edit.

When a stage is skipped because there's no plan yet (trivial/easy),
implementer and code_reviewer get distinct prompt wording ("work directly
from the issue") rather than a placeholder plan spliced into the normal
template — see `worker/prompts.py`. When plan_reviewer alone is skipped
(medium), implementer/code_reviewer get the planner's raw, unreviewed
output through the same wording used for `difficult`.

Reduced pipelines don't escalate: if the implementer or code reviewer
decides mid-task that a `trivial`/`easy`/`medium` classification was wrong,
the only available response is the same single-pass SKIP escape hatch used
everywhere else — the job aborts with an explanatory comment rather than
restarting with more stages.

Turn budgets **do** vary by difficulty, via `RepoConfig.difficulty_turn_budgets`
(keyed by difficulty, each a partial `TurnBudgets` override applied on top of
the repo's already-resolved default+repo-override budgets — see
`RepoConfig.effective_turn_budgets`). This was originally deferred ("revisit
if trivial/easy runs actually hit turn limits in practice") but got revisited
almost immediately: the first live `easy` run
([rodykon/tempo#6](https://github.com/rodykon/tempo/issues/6),
2026-08-15) hit the implementer's flat 30-turn budget, because with no
planner having run, the implementer was absorbing exploration work a
planner used to do on top of implementing and testing. Resolution happens
in the scheduler (`scheduler/main.py::_build_spec`), not the worker — the
scheduler already has both full config access and the job's persisted
`difficulty` (see the `jobs.difficulty` column), so the final per-stage
turn counts are computed once and baked into the worker's env vars exactly
as before; the worker itself has no difficulty-aware budget logic.

That same `easy` run also surfaced a second, unrelated bug: when a stage
hits its turn limit, the installed `claude-agent-sdk` raises a trailing
`ProcessError` on top of the informative `ResultMessage` it already
delivered (the CLI exits non-zero on purpose for `is_error` results — see
`claude_agent_sdk/_internal/query.py`). That exception used to escape
`pipeline.run_stage` uncaught, landing in `worker/main.py`'s generic crash
handler — which recorded the failure in SQLite but never commented on the
issue, so the failure was silent on GitHub. `run_stage` now catches
`ProcessError` and treats it as expected once a `ResultMessage` has already
been parsed; the crash handler now also always posts a comment. See
`CLAUDE.md`'s SDK-facts section for the corrected note on this — the
earlier claim that "the loop does not raise" on turn exhaustion was
incomplete.

---

## Config shape (draft — refined during implementation)

Two layers:

- **Global settings** (poll interval, default worker image, default turn
  budget per stage, max concurrent jobs, Anthropic API key, SQLite path).
- **Per-repo entries** (repo slug, GitHub token reference, whitelisted
  users, base branch, worker base image override, setup commands override,
  per-stage turn budget overrides, and per-difficulty turn budget overrides
  layered on top of those).

Secrets (GitHub tokens, Anthropic key) are referenced from config but supplied
via environment variables / a secrets file — never committed.

---

## Repo layout (target)

```
guppy/
  DESIGN.md            <- this file
  TODO.md               <- deferred work, explicitly out of v1 scope
  README.md             <- setup/usage
  CLAUDE.md              <- guidance for future Claude instances
  pyproject.toml
  docker-compose.yml
  Dockerfile.scheduler
  Dockerfile.worker
  config.example.yaml
  src/guppy/
    common/              <- shared config models, SQLite store, GitHub client
    scheduler/            <- poll loop, dispatcher, WorkerLauncher
    worker/                <- job entrypoint, 4-stage pipeline
```

## Superseded

The Phase 1 GitHub Actions prototype (workflow YAML, issue template,
`install.sh`, and the design docs describing it) has been removed from this
repo. That approach is superseded by this design, not extended by it — the
new pipeline runs as a standalone polling service, not as a per-issue
Actions job.
