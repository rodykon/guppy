# Guppy

Guppy watches a GitHub repository for issues filed by a specific person in a
specific format, and turns them into pull requests automatically. An issue
comes in, Claude Code reads the codebase, implements the fix or feature,
writes tests for it, and opens a PR against `dev` for a human to review.

This repo is Guppy's home: the installable workflow, the issue template, an
installer script, and the design docs behind them. It is **not** a repo that
Guppy watches itself — install it into whichever repo you actually want the
agent working on.

## How it works

```
Issue opened (by the allowed user, matching the template)
        │
        ▼
GitHub Actions workflow "Issue Agent"
        │
        ├─ Validate author + format -> skip silently if either fails
        ├─ Checkout repo, install deps
        ├─ Run Claude Code headlessly against the issue text
        │     -> implements the change
        │     -> always writes tests for it
        │     -> runs the test suite and fixes failures it introduced
        ├─ Commit to a new agent/<issue-number>-<slug> branch
        └─ Open a PR against `dev` + comment on the issue with the link
```

A human still reviews and merges the PR — the agent never touches `main`
directly and never auto-merges.

See [`github-agent-approaches.md`](github-agent-approaches.md) for the
architecture options considered and [`solution-design.md`](solution-design.md)
for the full design rationale, phased rollout plan, and future migration path
to a self-hosted server.

## What's in this repo

| Path | Purpose |
|---|---|
| `template/.github/workflows/issue-agent.yml` | The GitHub Actions workflow. Copy this into a target repo. |
| `template/.github/ISSUE_TEMPLATE/agent-task.md` | The issue template the agent expects. Copy this too. |
| `install.sh` | Interactive installer that copies both files in and configures the target repo. |
| `solution-design.md`, `github-agent-approaches.md` | Design docs. |

## Installing Guppy into a repo

Prerequisites:
- The target repo is a git repo you can push to, hosted on GitHub.
- You have an [Anthropic API key](https://console.anthropic.com/).
- You have a fine-grained GitHub PAT (or will create one) scoped to the
  target repo with **Contents: write** and **Pull requests: write**.
- (Optional but recommended) the [`gh` CLI](https://cli.github.com/),
  authenticated (`gh auth login`) — the installer will use it to set the
  repo variable/secrets for you. Without it, the installer prints the exact
  manual steps instead.

Run:

```bash
./install.sh /path/to/target-repo
```

(Omit the path to target the current directory.) The installer will:

1. Copy `issue-agent.yml` and `agent-task.md` into the target repo's
   `.github/` — asking before overwriting anything that's already there.
2. Ask which GitHub username is allowed to trigger the agent, and set the
   `ALLOWED_GITHUB_USER` repo variable (via `gh`, or print manual
   instructions).
3. Optionally set the `ANTHROPIC_API_KEY` and `AGENT_GITHUB_TOKEN` secrets
   via `gh` (input is hidden), or print manual instructions.
4. Check whether a `dev` branch exists — the workflow opens PRs against it —
   and offer to create and push one if not.

After it finishes, open the copied workflow file and adjust the
"Set up Node / Python / etc." and "Install project dependencies" steps to
match the target project's runtime (they default to Node).

## Using it

Once installed, have the allowed user open an issue on the target repo using
the **Agent Task** template:

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
- Specific scenario or edge case you want covered
```

Only `Type`, `Description`, and `Acceptance Criteria` are required — the
workflow silently ignores (and comments on) issues that don't match, or that
weren't opened by the allowed user.

`Tests` is optional, but it doesn't gate whether tests get written: **the
agent always writes tests for its change**, using the project's existing
test framework. If you fill in the `Tests` section, those specific scenarios
are called out to the agent as required coverage in addition to whatever else
it decides to test. If you leave it out, the agent picks the scenarios
itself based on the description and acceptance criteria.

From there:
1. The "Issue Agent" workflow run appears under the repo's **Actions** tab.
2. If the issue matched the format, Claude Code runs, and its full output is
   uploaded as a build artifact (`claude-output-<issue-number>`) for
   debugging.
3. If it produced changes, a PR opens against `dev` and the issue gets a
   comment with the link. Review the diff — including the generated tests —
   before merging.
4. If it couldn't produce a safe change, or produced no changes, the issue
   gets a comment saying so instead.

## Known limitations (Phase 1 prototype)

- No persistent memory between runs — every issue starts the agent cold.
- Token/cost scales with repo size and how much the agent needs to read.
- GitHub Actions' 6-hour job limit caps how long a single run can take.
- No approval gate before the PR is opened — review happens at the PR, not
  before.
- No retry logic — a failed Claude API call fails the whole run.

`solution-design.md` describes the Phase 2 migration path (a self-hosted
webhook server with a job queue, retries, and an approval gate) and the
concrete triggers for when it's worth making that move.
