# Guppy: GitHub Issue Agent — Solution Design

## Phase 1: Prototype (Approach 3 — Claude Code CLI in GitHub Actions)

### How It Works

A GitHub Actions workflow fires every time an issue is opened in the target repository. The workflow filters for the designated author and validates the issue format, then checks out the repository, runs Claude Code CLI in headless mode against the codebase with the issue as the prompt, commits whatever changes Claude Code makes, and opens a pull request targeting `dev`.

```
Issue opened on GitHub
        │
        ▼
GitHub Actions: "issue-agent" workflow
        │
        ├─ [step 1] Filter: author == ALLOWED_USER && body matches template?
        │           └─ No → exit early (no cost incurred)
        │
        ├─ [step 2] Checkout repo (full history, all branches)
        │
        ├─ [step 3] Install project dependencies (so tests can run)
        │
        ├─ [step 4] Run Claude Code CLI (headless)
        │           claude -p "<issue title + body>"
        │           --allowedTools "Read,Edit,Write,Bash"
        │           --max-turns 30
        │
        ├─ [step 5] Check if Claude Code made any changes
        │           └─ No changes → comment on issue + exit
        │
        ├─ [step 6] git checkout -b agent/<issue-number>-<slug>
        │           git add -A && git commit -m "..."
        │           git push origin HEAD
        │
        └─ [step 7] gh pr create --base dev --title "..." --body "..."
                    + comment on issue with PR link
```

---

## Issue Format Specification

Issues that should trigger the agent must follow this exact template. The agent ignores any issue that does not match.

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
```

**Validation rules:**
- `## Type` section must be present and contain exactly `bug` or `feature`
- `## Description` section must be non-empty
- `## Acceptance Criteria` must have at least one item
- Issue must be opened by the configured `ALLOWED_GITHUB_USER`

---

## Assets Required

| Asset | Purpose | Cost |
|---|---|---|
| GitHub repository | Target repo to watch and modify | Free |
| GitHub Actions | Runs the workflow | Free (2,000 min/mo); $0.008/min after |
| Anthropic API key | Powers Claude Code CLI | ~$3/M input tokens, ~$15/M output tokens (Sonnet 4.6) |
| Fine-grained PAT or GitHub App | Branch creation + PR creation | Free |
| GitHub Secrets | Stores API key + PAT securely | Free |

**Estimated cost per issue:** $0.10–$1.00 depending on codebase size and fix complexity. A 10,000-line codebase where Claude Code reads ~20% of files to make a fix costs roughly $0.30–$0.60.

---

## Setup Steps

### 1. Add Secrets to the Repository

In the target repository, go to **Settings → Secrets and variables → Actions** and add:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `AGENT_GITHUB_TOKEN` | A fine-grained PAT with `contents: write` and `pull-requests: write` scopes on this repo |
| `ALLOWED_GITHUB_USER` | The GitHub username allowed to trigger the agent |

Using a separate `AGENT_GITHUB_TOKEN` (not `GITHUB_TOKEN`) is important because the default `GITHUB_TOKEN` cannot trigger other workflows from a push, and Actions run by the default token show "github-actions[bot]" as the committer, which can be confusing.

### 2. Create the Workflow File

Add `.github/workflows/issue-agent.yml` to the target repository:

```yaml
name: Issue Agent

on:
  issues:
    types: [opened]

jobs:
  agent:
    runs-on: ubuntu-latest
    # Only run if the issue author is the allowed user
    if: github.event.issue.user.login == vars.ALLOWED_GITHUB_USER

    permissions:
      contents: write
      pull-requests: write
      issues: write

    steps:
      - name: Validate issue format
        id: validate
        env:
          ISSUE_BODY: ${{ github.event.issue.body }}
        run: |
          python3 - <<'EOF'
          import os, re, sys

          body = os.environ["ISSUE_BODY"]

          # Check required sections
          has_type = re.search(r'^##\s+Type\s*\n\s*(bug|feature)', body, re.MULTILINE | re.IGNORECASE)
          has_desc = re.search(r'^##\s+Description\s*\n(.+)', body, re.MULTILINE | re.DOTALL)
          has_ac   = re.search(r'^##\s+Acceptance Criteria\s*\n\s*-\s+\[', body, re.MULTILINE)

          if not (has_type and has_desc and has_ac):
              print("Issue does not match expected format — skipping.")
              with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                  f.write("valid=false\n")
              sys.exit(0)

          issue_type = has_type.group(1).lower()
          with open(os.environ["GITHUB_OUTPUT"], "a") as f:
              f.write("valid=true\n")
              f.write(f"issue_type={issue_type}\n")
          EOF

      - name: Comment and exit if invalid format
        if: steps.validate.outputs.valid == 'false'
        uses: actions/github-script@v7
        with:
          github-token: ${{ secrets.AGENT_GITHUB_TOKEN }}
          script: |
            github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: "👋 This issue was not picked up by the agent because it doesn't match the required format. Please use the issue template."
            });

      - name: Checkout repository
        if: steps.validate.outputs.valid == 'true'
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.AGENT_GITHUB_TOKEN }}
          fetch-depth: 0

      - name: Set up Node / Python / etc.
        if: steps.validate.outputs.valid == 'true'
        # Adjust this step for your project's runtime
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'

      - name: Install project dependencies
        if: steps.validate.outputs.valid == 'true'
        run: npm ci   # or: pip install -r requirements.txt, etc.

      - name: Install Claude Code CLI
        if: steps.validate.outputs.valid == 'true'
        run: npm install -g @anthropic-ai/claude-code

      - name: Run Claude Code agent
        if: steps.validate.outputs.valid == 'true'
        id: claude
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          ISSUE_TITLE: ${{ github.event.issue.title }}
          ISSUE_BODY: ${{ github.event.issue.body }}
          ISSUE_TYPE: ${{ steps.validate.outputs.issue_type }}
        run: |
          PROMPT="You are an automated software agent working on a GitHub repository.

          A new ${ISSUE_TYPE} issue has been filed:

          Title: ${ISSUE_TITLE}

          ${ISSUE_BODY}

          Your task:
          1. Read the codebase to understand the relevant code.
          2. Implement the fix or feature described in the issue.
          3. Make sure your changes satisfy the acceptance criteria listed in the issue.
          4. Run the test suite (e.g. npm test) and fix any failures your changes introduce.
          5. Do NOT commit anything — just make the file changes.

          Important constraints:
          - Only modify files that are necessary to address this issue.
          - Do not add comments explaining what you changed.
          - Do not create migration or changelog files.
          - If the issue is ambiguous or impossible to implement safely, output the single word SKIP and do nothing else."

          claude --print "$PROMPT" \
                 --allowedTools "Read,Edit,Write,Bash" \
                 --max-turns 40 \
                 2>&1 | tee /tmp/claude-output.txt

      - name: Check for changes
        if: steps.validate.outputs.valid == 'true'
        id: changes
        run: |
          if grep -q "^SKIP$" /tmp/claude-output.txt; then
            echo "has_changes=false" >> "$GITHUB_OUTPUT"
            echo "skip_reason=agent_skip" >> "$GITHUB_OUTPUT"
          elif git diff --quiet && git diff --cached --quiet; then
            echo "has_changes=false" >> "$GITHUB_OUTPUT"
            echo "skip_reason=no_changes" >> "$GITHUB_OUTPUT"
          else
            echo "has_changes=true" >> "$GITHUB_OUTPUT"
          fi

      - name: Comment if agent skipped
        if: steps.validate.outputs.valid == 'true' && steps.changes.outputs.has_changes == 'false'
        uses: actions/github-script@v7
        with:
          github-token: ${{ secrets.AGENT_GITHUB_TOKEN }}
          script: |
            const reason = "${{ steps.changes.outputs.skip_reason }}";
            const msg = reason === "agent_skip"
              ? "🤖 The agent reviewed this issue but determined it could not implement the change safely. Manual intervention is required."
              : "🤖 The agent ran but produced no file changes. Please review the issue description and try again.";
            github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: msg
            });

      - name: Create branch, commit, and push
        if: steps.validate.outputs.valid == 'true' && steps.changes.outputs.has_changes == 'true'
        id: push
        env:
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          ISSUE_TITLE: ${{ github.event.issue.title }}
        run: |
          SLUG=$(echo "$ISSUE_TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | cut -c1-40)
          BRANCH="agent/${ISSUE_NUMBER}-${SLUG}"

          git config user.name  "guppy-agent[bot]"
          git config user.email "guppy-agent[bot]@users.noreply.github.com"
          git checkout -b "$BRANCH"
          git add -A
          git commit -m "fix(#${ISSUE_NUMBER}): ${ISSUE_TITLE}

          Automated fix generated by guppy-agent.
          Closes #${ISSUE_NUMBER}"
          git push origin "$BRANCH"

          echo "branch=$BRANCH" >> "$GITHUB_OUTPUT"

      - name: Create pull request
        if: steps.validate.outputs.valid == 'true' && steps.changes.outputs.has_changes == 'true'
        id: pr
        env:
          GH_TOKEN: ${{ secrets.AGENT_GITHUB_TOKEN }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          ISSUE_TITLE: ${{ github.event.issue.title }}
          ISSUE_BODY: ${{ github.event.issue.body }}
          BRANCH: ${{ steps.push.outputs.branch }}
        run: |
          PR_URL=$(gh pr create \
            --base dev \
            --head "$BRANCH" \
            --title "fix(#${ISSUE_NUMBER}): ${ISSUE_TITLE}" \
            --body "## Automated fix for #${ISSUE_NUMBER}

          ${ISSUE_BODY}

          ---
          > Generated by guppy-agent. Please review carefully before merging.")

          echo "pr_url=$PR_URL" >> "$GITHUB_OUTPUT"

      - name: Comment on issue with PR link
        if: steps.validate.outputs.valid == 'true' && steps.changes.outputs.has_changes == 'true'
        uses: actions/github-script@v7
        with:
          github-token: ${{ secrets.AGENT_GITHUB_TOKEN }}
          script: |
            github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `🤖 The agent has created a fix: ${{ steps.pr.outputs.pr_url }}\n\nPlease review the changes before merging.`
            });
```

### 3. Add the Repository Variable

In **Settings → Secrets and variables → Actions → Variables**, add:

| Variable name | Value |
|---|---|
| `ALLOWED_GITHUB_USER` | The GitHub username that can trigger the agent |

Variables (not secrets) are used for non-sensitive configuration so they can be visible in workflow logs.

### 4. Ensure a `dev` Branch Exists

```bash
git checkout -b dev
git push origin dev
```

---

## Prompt Engineering Notes

The quality of the agent's output is determined almost entirely by the prompt passed to `claude --print`. Key principles:

- **Give the agent an identity and goal** up front ("You are an automated agent…")
- **Include the full issue body** verbatim so it sees the acceptance criteria
- **Be explicit about what not to do** (don't commit, don't create changelogs, don't over-engineer)
- **Provide an escape hatch** (the `SKIP` keyword) so the agent can signal when a task is unsafe rather than producing broken code
- **Ask it to run tests** — Claude Code can execute `npm test` or `pytest` via its Bash tool, and will fix failures it introduces

Iterate on the prompt based on the PRs the agent produces in early runs.

---

## Observability

| Signal | Where to find it |
|---|---|
| Workflow run logs | GitHub Actions tab → issue-agent runs |
| Claude Code's reasoning | `/tmp/claude-output.txt` (visible in step logs) |
| Agent comments | The original issue thread |
| PR diff | The opened pull request |

To preserve Claude Code's full output for debugging, add an upload step:

```yaml
- name: Upload agent output
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: claude-output-${{ github.event.issue.number }}
    path: /tmp/claude-output.txt
    retention-days: 14
```

---

## Known Limitations of the Prototype

1. **No persistent memory** — each run starts cold; Claude Code re-reads the codebase from scratch every time.
2. **Token cost scales with repo size** — very large repos are expensive; mitigate by passing `--allowedPaths` to restrict Claude Code to relevant directories.
3. **Actions runner timeout** — 6-hour hard limit; complex features may hit this.
4. **No approval gate** — the PR is created immediately; reviewers must catch errors.
5. **Single-threaded** — if two issues are filed simultaneously, both workflows run in parallel and could conflict on git state (rare but possible).
6. **No retry logic** — if the Claude API call fails mid-run, the workflow fails silently.

---

---

## Phase 2: Migration to Approach 2 (Self-Hosted Webhook Server)

Migrate when you observe any of the following in Phase 1:
- Workflow run times consistently exceed 30 minutes
- Token costs are unpredictable or too high (no cap available in Actions)
- You need an approval gate before the PR is created
- You want retry logic, metrics, or Slack/email notifications
- Two concurrent issues are causing race conditions

### Target Architecture

```
GitHub App (webhook)
        │  POST /webhook
        ▼
Webhook Server (FastAPI, always-on container)
  ├─ Validate HMAC-SHA256 signature
  ├─ Filter: author + format
  └─ Enqueue job → Redis
        │
        ▼
Worker Pool (Celery or ARQ)
  ├─ Pull job from queue
  ├─ Clone repo into isolated temp dir
  ├─ Build agent prompt
  ├─ Call Anthropic API directly (tool-use loop):
  │     tools: read_file, write_file, run_shell, search_codebase
  │     max_iterations: 20
  ├─ Validate: run tests in sandbox
  │     └─ If tests fail → retry up to 3 times
  │     └─ If still failing → post "needs human" comment + stop
  ├─ Commit + push branch
  └─ Create PR via GitHub API
        │
  [Optional approval gate]
        │
  Slack/email notification → human reviews → merges
```

### Migration Checklist

#### Infrastructure

- [ ] Provision a VPS or container service (Fly.io, Railway, or Hetzner CX22)
- [ ] Provision a Redis instance (Upstash free tier or self-hosted)
- [ ] Set up a GitHub App (replace PAT) with scopes: `Contents: write`, `Pull requests: write`, `Issues: write`, `Metadata: read`
- [ ] Point the GitHub App webhook at `https://<your-server>/webhook`
- [ ] Store secrets in environment variables or a secrets manager (e.g., Doppler, AWS Secrets Manager)

#### Application Code

- [ ] Write FastAPI webhook handler with HMAC-SHA256 signature validation
- [ ] Implement the same format validation logic from the Actions workflow (port the Python script)
- [ ] Implement the job queue (Celery + Redis)
- [ ] Implement the agent worker:
  - Replace `claude --print` with direct Anthropic SDK calls using the tool-use API
  - Implement tools: `read_file`, `write_file`, `run_shell` (sandboxed), `list_directory`, `search_codebase` (grep wrapper)
  - Implement the ReAct loop: send tool results back to the model until it signals completion
- [ ] Implement test-run validation before committing
- [ ] Implement retry logic (max 3 attempts per issue, with exponential backoff on API errors)
- [ ] Implement PR creation and issue commenting via PyGithub or the GitHub REST API directly

#### Observability

- [ ] Structured logging (JSON logs → log aggregator)
- [ ] Job status dashboard (Flower for Celery, or a simple admin endpoint)
- [ ] Alerting on job failures (Slack webhook or email)
- [ ] Cost tracking: log token counts per job and export to a time-series store

#### Deployment

- [ ] Containerize the server and worker (single Dockerfile, two services)
- [ ] Set up CI/CD to deploy on push to `main`
- [ ] Configure health check endpoint for the load balancer / uptime monitor
- [ ] Run a load test: verify the system handles 10 simultaneous issues without race conditions

#### Cutover

1. Deploy the new server alongside the existing Actions workflow
2. Set `ALLOWED_GITHUB_USER` to a test user and send 5 test issues through the new server
3. Verify PRs are created correctly and tests pass
4. Disable the GitHub Actions workflow (`on: issues` → comment it out)
5. Switch the GitHub App webhook to production

### Recommended Stack for Phase 2

| Layer | Choice | Why |
|---|---|---|
| Web framework | FastAPI (Python) | Async, fast, easy webhook validation |
| Queue | ARQ (async Redis queue) | Simpler than Celery for async Python |
| LLM client | `anthropic` Python SDK | Native tool-use support |
| GitHub client | `PyGithub` | Full GitHub API coverage |
| Container host | Fly.io | Free hobby tier; auto-scaling; simple deploy |
| Redis | Upstash | Serverless Redis, free tier is sufficient |
| Secrets | Doppler or env vars | Simple for small teams |
| Logging | structlog → Grafana Cloud | Free tier, good retention |

### Estimated Phase 2 Costs

| Item | Cost |
|---|---|
| Fly.io (1 shared-CPU VM) | $0–$5/mo |
| Upstash Redis | $0 (free tier) |
| Anthropic API | Same per-issue cost as Phase 1 |
| Doppler (secrets) | $0 (free for 1 project) |
| Grafana Cloud (logging) | $0 (free tier) |
| **Total fixed cost** | **~$0–$10/mo** |

---

## Decision: When to Migrate

| Trigger | Action |
|---|---|
| Any single workflow run > 30 min | Investigate root cause; if systemic, migrate |
| Monthly API cost > $50 with no cap in sight | Migrate (add per-issue token budget in Phase 2) |
| Need to add approval gate before PR creation | Migrate |
| Need to handle > 20 issues/month reliably | Migrate |
| Need retry logic for flaky API calls | Migrate |
