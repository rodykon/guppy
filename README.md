# Guppy

Guppy watches configured GitHub repositories for issues filed by whitelisted
users in a specific format, and turns them into pull requests automatically.
It runs as a standalone service (locally in Docker today; portable to a
cloud job runner later) that polls on an interval and runs each qualifying
issue through a four-stage Claude agent pipeline: plan, review the plan,
implement, review the code. A human still reviews and merges every PR --
the agent never touches `main` and never auto-merges.

See [`DESIGN.md`](DESIGN.md) for the full architecture and the reasoning
behind each decision, and [`TODO.md`](TODO.md) for what was deliberately
deferred.

## How it works, briefly

```
Scheduler (long-running container)
  -> polls each configured repo every N seconds
  -> filters: author whitelisted? issue body matches the template?
  -> new qualifying issue -> job, dispatched (bounded concurrency) to a
     fresh ephemeral worker container via the Docker socket

Worker (one per job, torn down after)
  -> clones the repo with a token scoped to only that repo
  -> planner -> plan reviewer -> implementer -> code reviewer
     (single pass each; any stage can SKIP and abort with an explanatory
     comment instead of pushing something bad)
  -> on success: branch, commit, push, open a PR against the repo's base
     branch, comment on the issue with the link
```

## Prerequisites

- Docker and Docker Compose.
- An [Anthropic API key](https://console.anthropic.com/).
- For each repo you want to watch: a fine-grained GitHub PAT scoped to
  *only that repo*, with `Contents: write`, `Pull requests: write`, and
  `Issues: write`.
- Each target repo should have a `.guppy/context.md` file describing its
  architecture for the agents (authoring these is not yet tooled -- write
  them by hand for now).

## Setup

1. **Configure.**
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```
   Edit `config.yaml`: list the repos to watch, each with its
   `github_token_env` (the *name* of an env var, not the token itself),
   whitelisted `allowed_users`, base branch, and any setup commands the
   worker needs to run before the pipeline (e.g. `npm ci`). Edit `.env`
   with the actual secret values -- `ANTHROPIC_API_KEY` and one token per
   repo, matching the env var names you used in `config.yaml`. Never
   commit `.env` or `config.yaml` (both are gitignored).

2. **Build the images.**
   ```bash
   docker compose build scheduler
   docker build -f Dockerfile.worker -t guppy-worker:latest .
   ```
   The worker image isn't part of `docker-compose.yml` -- it's not a
   long-running service, the scheduler launches disposable instances of it
   per job. If a repo needs a different runtime than the generic image
   provides (Node + Python by default), build a repo-specific image and
   point that repo's `worker_image` at it in `config.yaml`.

3. **Add the context file** to each target repo: `.guppy/context.md`,
   committed like any other file, describing the codebase for the agents.

4. **Run.**
   ```bash
   docker compose up -d scheduler
   docker compose logs -f scheduler
   ```

## Filing an issue Guppy will pick up

Only issues from a repo's `allowed_users`, matching this format, are
picked up:

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
```

`Type`, `Description`, and `Acceptance Criteria` are required; issues from
a whitelisted author that don't match get a comment explaining why, once.
`Tests` is optional, but doesn't gate whether tests get written -- the
implementer always writes tests; if you fill this section in, those
specific scenarios are passed through as required coverage on top of
whatever else the agent decides to test.

Each qualifying issue is processed **at most once, ever**. A SKIPped issue
(the agent judged it unsafe to act on) won't be retried automatically --
close it and open a fresh one if you rework it.

## Checking on a job

There's no dashboard yet (see `TODO.md`) -- query the shared SQLite store
directly:

```bash
docker compose exec scheduler python3 -c "
import sqlite3
conn = sqlite3.connect('/data/guppy.db')
conn.row_factory = sqlite3.Row
for row in conn.execute('SELECT id, repo_slug, issue_number, status, stage, pr_url FROM jobs ORDER BY created_at DESC LIMIT 20'):
    print(dict(row))
"
```

Each job's artifacts (plan, reviewed plan, diff, full pipeline log) are
under `/data` on the `guppy-data` volume, at paths recorded in that job's
row (`plan_artifact_path`, `plan_review_artifact_path`,
`diff_artifact_path`, `log_path`).

## Known limitations (see TODO.md for the full list and reasoning)

- No retry loop between pipeline stages -- a stage that can't produce
  something usable SKIPs the whole job.
- No status dashboard -- query SQLite directly.
- Worker containers have unrestricted outbound network access.
- No re-trigger if a SKIPped issue is later edited.
