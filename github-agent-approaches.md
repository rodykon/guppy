# GitHub Issue-to-PR Agent: Architecture Approaches

## Overview

The goal is an automated agent that:
1. Monitors a GitHub repository for new issues created by a specific user following a defined format
2. Understands the issue (bug fix or new feature request)
3. Creates a new branch, implements the fix/feature using an LLM
4. Opens a pull request targeting the `dev` branch

---

## Common Components (All Approaches)

Regardless of architecture, every approach requires the same logical building blocks:

| Component | Purpose |
|---|---|
| **Event trigger** | Detect new GitHub issues (webhook or polling) |
| **Issue filter** | Check author + format validity |
| **Issue parser** | Extract structured intent from the issue body |
| **Code generation** | LLM that reads the codebase and writes the fix |
| **Code execution sandbox** | Safe environment to run/test generated code |
| **Git operations** | Clone repo, create branch, commit changes |
| **GitHub API client** | Create the PR and post status comments |

### Issue Format Recommendation

Define a machine-readable template for issues so the agent can reliably parse intent:

```markdown
## Type
bug | feature

## Description
<what the problem is or what should be built>

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Affected Files (optional)
- src/foo.ts

## Tests (optional)
- Specific test scenario or edge case you want covered
```

The `Tests` section is optional, but only in the sense that filling it in is
optional — the agent writes tests for every change regardless. If present,
its contents are surfaced to the agent as scenarios it must additionally
cover; if absent, the agent chooses scenarios itself.

---

## Approach 1: GitHub Actions + Claude API

### Architecture

```
GitHub Issue Created
        │
        ▼
GitHub Actions Workflow (triggered by issues event)
        │
        ├─ Filter: correct author + valid format?
        │         └─ No → exit
        │
        ▼
Checkout repo in Actions runner
        │
        ▼
Call Claude API (claude-opus-4 or claude-sonnet-4-6)
  ├─ Pass full repo context (or relevant files)
  ├─ Pass parsed issue
  └─ Receive file diffs / new file content
        │
        ▼
Apply changes, commit, push new branch
        │
        ▼
gh CLI → Create PR to dev
```

### Implementation

- Workflow file: `.github/workflows/issue-agent.yml`
- Trigger: `on: issues: types: [opened]`
- Filter step: check `github.event.issue.user.login` and issue body regex
- Agent step: Python/Node script that calls the Anthropic SDK, feeds the issue + relevant file contents, and parses the response into file writes
- Git step: standard `git checkout -b`, `git commit`, `git push`, `gh pr create`

### Assets Needed

| Asset | Cost |
|---|---|
| GitHub Actions minutes | Free (2,000 min/mo on Free plan); ~$0.008/min beyond that |
| Claude API | ~$3/M input tokens, ~$15/M output tokens (Sonnet 4.6); $15/$75 (Opus 4.8) |
| GitHub repo | Free |
| Secrets storage (API key) | Free (GitHub Secrets) |

**Estimated cost per issue:** $0.05–$0.50 depending on codebase size and model choice.

### Pros

- No infrastructure to manage — fully serverless
- Native GitHub integration, no external services
- Secrets managed by GitHub
- Easy to audit (workflow logs)
- Version-controlled agent logic (lives in the repo)

### Cons

- Actions runner has a 6-hour job limit and limited RAM (7 GB); large repos may struggle
- Passing large codebases to the LLM is expensive (token cost scales with repo size)
- No persistent state between steps — every run starts cold
- LLM output must be deterministically parseable into file changes (fragile without careful prompting)
- Cannot run arbitrary tools (no shell access to a live app to test the fix)

---

## Approach 2: Self-Hosted Webhook Server + Agent Loop

### Architecture

```
GitHub Issue Created
        │  (webhook POST)
        ▼
Webhook Server (FastAPI / Express, always-on)
        │
        ├─ Validate GitHub signature
        ├─ Filter: author + format
        │
        ▼
Job Queue (Redis/Celery or BullMQ)
        │
        ▼
Worker Process
  ├─ Clone repo into temp directory
  ├─ Build agent loop (ReAct / tool-use pattern):
  │     LLM ←→ tools: read_file, write_file, run_tests, search_codebase
  ├─ Commit changes
  ├─ Push branch via GitHub API
  └─ Open PR
        │
        ▼
GitHub PR created; comment posted on issue
```

### Implementation Options

- **Webhook server:** FastAPI (Python) or Express (Node)
- **Agent framework:** LangGraph, smolagents, or Anthropic's native tool-use API directly
- **LLM:** Claude API (Sonnet or Opus)
- **Queue:** Redis + Celery (Python) or BullMQ (Node)
- **Hosting:** VPS (Hetzner, DigitalOcean), container (Railway, Fly.io), or Kubernetes

### Assets Needed

| Asset | Cost |
|---|---|
| VPS / container host | $5–$20/mo (Hetzner CX22, Railway hobby, Fly.io) |
| Redis (queue) | Free tier (Upstash) or ~$5/mo self-hosted |
| Claude API | Same as Approach 1 |
| GitHub App (for webhooks + auth) | Free |
| Domain + TLS (for webhook endpoint) | $10–$15/yr or free via Fly/Railway subdomain |

**Estimated cost per issue:** $0.05–$0.50 (API) + fixed infra ~$10–$25/mo.

### Pros

- Full control over the agent loop — can use ReAct (Reason+Act) with real tool calls
- Can run tests inside the worker (install deps, execute test suite) before committing
- Persistent process means faster cold starts
- Can handle long-running fixes without a job-time cap
- Extensible: add Slack notifications, approval gates, metrics

### Cons

- Infrastructure to provision and maintain
- Requires secrets management beyond GitHub (env vars on server)
- More complex deployment; need to handle failures, retries, concurrency
- Security surface: webhook endpoint is internet-facing

---

## Approach 3: Claude Code CLI as the Agent Engine

### Architecture

```
GitHub Issue Created
        │  (webhook or GitHub Actions trigger)
        ▼
Trigger Script (Actions or webhook server)
        │
        ├─ Filter: author + format
        │
        ▼
Spin up ephemeral VM / container with repo cloned
        │
        ▼
Run: claude --print "Fix this issue: <issue body>" \
     --allowedTools "Edit,Read,Bash,Write"
        │  (Claude Code CLI drives all file edits)
        ▼
Commit & push changes
        │
        ▼
Create PR via gh CLI
```

### How It Works

Claude Code (the CLI tool you are using now) can be run headlessly with `--print` mode. It has native tools for reading files, editing files, running shell commands, and understanding a codebase. By pointing it at the cloned repo and giving it the issue text as the prompt, it acts as a self-directed coding agent.

### Assets Needed

| Asset | Cost |
|---|---|
| Ephemeral runner (GitHub Actions or EC2 spot) | Free–$0.02/run |
| Claude API (consumed by Claude Code CLI) | Same token rates as Approach 1; Claude Code adds no markup |
| GitHub repo + Secrets | Free |

**Estimated cost per issue:** $0.10–$2.00 (Claude Code tends to use more tokens because it reads files iteratively).

### Pros

- Minimal custom code to write — Claude Code handles all file manipulation
- High-quality edits: Claude Code understands the codebase holistically
- Can run `bash` to execute tests and verify the fix before committing
- No agent framework to build or maintain
- Naturally handles both bug fixes and new features

### Cons

- Harder to control exactly what the agent does (less deterministic than structured tool-use)
- Token usage is higher and harder to predict/cap
- Requires a full checkout environment with dependencies installed (for test runs)
- Audit trail is in Claude Code's session output, not structured logs
- Still experimental for fully autonomous workflows — may need guardrails

---

## Approach 4: No-Code Orchestration Platform

### Architecture

```
GitHub Issue Created
        │  (native trigger)
        ▼
Platform (n8n / Make.com / Zapier)
        │
        ├─ Filter node: author + regex match on body
        │
        ▼
HTTP node → Claude API (or OpenAI)
  └─ Prompt: "Given this issue, what files need to change and how?"
        │
        ▼
GitHub API nodes:
  ├─ Create branch
  ├─ Get file contents (Base64 → decode)
  ├─ Update file (Base64 encode patch → PUT /contents)
  └─ Create PR
```

### Platforms

| Platform | Self-hostable | Free Tier | Paid |
|---|---|---|---|
| n8n | Yes | Yes (cloud limited) | $20/mo (cloud) or free (self-host) |
| Make.com | No | 1,000 ops/mo | $9–$16/mo |
| Zapier | No | 100 tasks/mo | $19.99+/mo |

### Pros

- Fastest to set up (hours, not days)
- Visual flow editor, no code required
- Built-in connectors for GitHub, Slack, etc.
- Good for prototyping or low-volume use

### Cons

- The GitHub Contents API can only update one file at a time — multi-file changes require many API calls and complex logic in a visual editor
- The LLM must return perfectly structured output (JSON with file path + content); very fragile
- Cannot run code or tests to verify the fix
- Limited to simple, single-file changes in practice
- Vendor lock-in; migrating later is costly

---

## Comparison Summary

| | Approach 1 (Actions + API) | Approach 2 (Self-hosted server) | Approach 3 (Claude Code CLI) | Approach 4 (No-code) |
|---|---|---|---|---|
| **Setup complexity** | Low | High | Medium | Very Low |
| **Infrastructure cost** | ~$0 fixed | $10–$25/mo fixed | ~$0 fixed | $0–$20/mo fixed |
| **Cost per issue** | $0.05–$0.50 | $0.05–$0.50 | $0.10–$2.00 | $0.05–$0.50 + ops |
| **Multi-file changes** | Possible | Yes | Yes (native) | Very hard |
| **Can run tests** | Limited | Yes | Yes | No |
| **Auditability** | High (logs) | High (logs + queue) | Medium | Medium |
| **Maintenance burden** | Low | High | Low | Low |
| **Customizability** | High | Very High | Medium | Low |
| **Production readiness** | Good | Best | Experimental | Poor for complex issues |

---

## Recommendation

**Start with Approach 3 (Claude Code CLI in GitHub Actions)** for a prototype:
- Minimal code to write
- No infrastructure to provision
- Claude Code natively handles multi-file edits and can run tests
- Easy to iterate on the prompt

**Graduate to Approach 2 (Self-hosted server)** if you need:
- Reliable test execution in a controlled environment
- Approval gates before the PR is created
- Handling of large repos where Actions limits are a problem
- Metrics, retry logic, or Slack notifications

**Avoid Approach 4** for anything beyond trivially simple single-file changes.

---

## Security Considerations (All Approaches)

- **Never** give the agent write access to `main`/`master` — only allow branch creation and PRs
- Use a **dedicated GitHub App** (not a personal token) so permissions are scoped and revocable
- Validate the **GitHub webhook signature** (HMAC-SHA256) before processing any event
- Restrict which users can trigger the agent (allow-list by GitHub username)
- Run code generation in an **isolated sandbox** (container with no network access to prod systems)
- Require a **human reviewer** to approve the PR before merge — the agent should never auto-merge
