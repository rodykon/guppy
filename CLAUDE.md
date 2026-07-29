# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

Guppy turns GitHub issues into pull requests automatically. A designated
user files an issue in a specific format on a *target* repository; a
GitHub Actions workflow validates it, runs Claude Code headlessly against
the target codebase to implement the fix/feature (always including tests),
and opens a PR against `dev` for human review.

**This repo (`guppy`) is not a repo the agent watches.** It's the tool's
home: the installable workflow/template, the installer script, and the
design docs. Nothing here runs against guppy's own issues — the deliverable
in `template/` is copied into *other* repos via `install.sh`.

## Repo layout and architecture

- `template/.github/workflows/issue-agent.yml` — the actual GitHub Actions
  workflow, meant to be copied into a target repo. This is the source of
  truth for the agent's behavior; `solution-design.md`'s embedded YAML is
  illustrative design reference only and may drift from it.
- `template/.github/ISSUE_TEMPLATE/agent-task.md` — the issue template a
  target repo's users fill in. Required sections: `## Type` (bug|feature),
  `## Description`, `## Acceptance Criteria`. Optional: `## Affected Files`,
  `## Tests`.
- `install.sh` — interactive installer. Copies the two template files into
  a target repo's `.github/`, configures the `ALLOWED_GITHUB_USER` repo
  variable and the `ANTHROPIC_API_KEY`/`AGENT_GITHUB_TOKEN` secrets (via
  `gh` if available, else prints manual steps), and offers to create/push
  a `dev` branch if missing. Idempotent — re-running prompts before
  overwriting existing files.
- `README.md` — user-facing install/usage instructions; keep in sync with
  `install.sh` and the template files when either changes.
- `solution-design.md` — phased design doc. Phase 1 (implemented) is the
  Actions-based prototype described above. Phase 2 (not built) is a
  self-hosted webhook server + job queue for when Phase 1's limits are hit
  (long runs, cost caps, approval gates, retries, concurrency) — includes
  a migration checklist and concrete triggers for when to make that move.
- `github-agent-approaches.md` — the four architecture options considered
  (Actions+API, self-hosted server, Claude Code CLI in Actions, no-code
  platforms) with a comparison table and security considerations. Approach
  3 (Claude Code CLI in Actions) is what got built.

### How the workflow itself works (`issue-agent.yml`)

1. `if: github.event.issue.user.login == vars.ALLOWED_GITHUB_USER` gates the
   whole job at trigger time.
2. A Python step (embedded via heredoc) regex-validates the issue body's
   required sections and extracts `issue_type` and the optional `## Tests`
   section content, writing them to `$GITHUB_OUTPUT` (multiline values use
   a random `GUPPY_<uuid>` delimiter, not a fixed one, to avoid collisions
   with issue body content).
3. Invalid-format issues get a comment and the rest of the job is skipped
   via per-step `if:` conditions — there's no early `exit`, every later step
   checks `steps.validate.outputs.valid`.
4. The prompt sent to Claude Code is built by a second Python step using
   `textwrap.dedent` + `string.Template.safe_substitute` — **not**
   `str.format`/f-string `.format()`, because issue bodies routinely contain
   literal `{}` (code snippets) that would break format-style substitution.
   Keep using `Template` if you touch this.
5. **Test generation is unconditional**: the fixed prompt always instructs
   the agent to write tests (step 3 of its task list), regardless of
   whether the issue's `## Tests` section is filled in. If present, that
   section's content is passed through as additional required coverage; if
   absent, the agent is told to pick scenarios itself. Don't make test
   generation conditional on the section's presence — that's the opposite
   of what was asked when this was built.
6. Claude Code runs via `claude --print "$(cat /tmp/agent-prompt.txt)"` —
   prompt goes through a file, not inline shell interpolation, to sidestep
   quoting issues with arbitrary issue-body content.
7. `SKIP` (literal, on its own line) is the escape hatch the agent uses to
   signal it can't safely implement the issue; the "Check for changes" step
   treats that the same as "no diff produced" (comments instead of opening
   a PR).
8. Branch naming: `agent/<issue-number>-<slugified-title>`. PRs always
   target `dev`, never `main`/`master`.

## Commands

There is no application build/lint/test suite in this repo — it's docs plus
a workflow template and a shell installer, not a package. Relevant checks
when editing:

```bash
# Validate the workflow YAML parses
python3 -c "import yaml; yaml.safe_load(open('template/.github/workflows/issue-agent.yml')); print('OK')"

# Check install.sh syntax
bash -n install.sh

# Exercise the installer against a scratch repo (don't run against a real one)
mkdir -p /tmp/guppy-install-test && cd /tmp/guppy-install-test && git init -q && git commit -q --allow-empty -m init
cd - && ./install.sh /tmp/guppy-install-test
```

When editing the two Python heredocs embedded in `issue-agent.yml` (issue
validation, prompt building), test them standalone first by extracting the
script, setting the same env vars the workflow sets, and running with
`python3` directly — GitHub Actions won't give you a REPL to debug a bad
regex or template in place.
