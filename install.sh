#!/usr/bin/env bash
# Guppy installer.
#
# Copies the issue-agent workflow and issue template into a target
# repository and walks through configuring the GitHub variable/secrets it
# needs. Safe to re-run.
#
# Usage: ./install.sh [path-to-target-repo]
#   (defaults to the current directory)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/template"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '\n%s\n' "$1"; }

ask() {
  local prompt="$1" reply
  read -r -p "$prompt" reply
  printf '%s' "$reply"
}

confirm() {
  local prompt="$1" reply
  read -r -p "$prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

bold "Guppy installer"

# --- 1. Locate target repo -------------------------------------------------
TARGET_DIR="${1:-$(pwd)}"
if [[ ! -d "$TARGET_DIR" ]]; then
  echo "Error: $TARGET_DIR does not exist." >&2
  exit 1
fi
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

if [[ ! -d "$TARGET_DIR/.git" ]]; then
  echo "Error: $TARGET_DIR is not a git repository (no .git directory)." >&2
  exit 1
fi

info "Target repository: $TARGET_DIR"

# --- 2. Copy workflow + issue template --------------------------------------
mkdir -p "$TARGET_DIR/.github/workflows" "$TARGET_DIR/.github/ISSUE_TEMPLATE"

WORKFLOW_DEST="$TARGET_DIR/.github/workflows/issue-agent.yml"
ISSUE_TEMPLATE_DEST="$TARGET_DIR/.github/ISSUE_TEMPLATE/agent-task.md"

install_file() {
  local src="$1" dest="$2"
  if [[ -f "$dest" ]] && ! confirm "$dest already exists. Overwrite?"; then
    echo "Skipped $dest"
    return
  fi
  cp "$src" "$dest"
  echo "Installed $dest"
}

install_file "$TEMPLATE_DIR/.github/workflows/issue-agent.yml" "$WORKFLOW_DEST"
install_file "$TEMPLATE_DIR/.github/ISSUE_TEMPLATE/agent-task.md" "$ISSUE_TEMPLATE_DEST"

# --- 3. Detect owner/repo ----------------------------------------------------
HAVE_GH=false
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  HAVE_GH=true
fi

REPO_SLUG=""
if $HAVE_GH; then
  REPO_SLUG="$(cd "$TARGET_DIR" && gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
fi

if [[ -z "$REPO_SLUG" ]]; then
  REPO_SLUG="$(ask "GitHub repo (owner/name), e.g. my-org/my-repo: ")"
fi

info "Configuring $REPO_SLUG"

if ! $HAVE_GH; then
  info "gh CLI not found or not authenticated -- printing manual setup steps instead of configuring GitHub directly."
fi

# --- 4. ALLOWED_GITHUB_USER variable -----------------------------------------
ALLOWED_USER="$(ask "GitHub username allowed to trigger the agent (issue author to watch for): ")"

if $HAVE_GH && confirm "Set repo variable ALLOWED_GITHUB_USER=$ALLOWED_USER on $REPO_SLUG now?"; then
  gh variable set ALLOWED_GITHUB_USER --repo "$REPO_SLUG" --body "$ALLOWED_USER"
  echo "Variable set."
else
  echo "Manual step -- Settings > Secrets and variables > Actions > Variables > New variable:"
  echo "  Name:  ALLOWED_GITHUB_USER"
  echo "  Value: $ALLOWED_USER"
fi

# --- 5. Secrets: ANTHROPIC_API_KEY, AGENT_GITHUB_TOKEN -----------------------
info "Two secrets are required:
  ANTHROPIC_API_KEY  -- your Anthropic API key
  AGENT_GITHUB_TOKEN -- a fine-grained PAT with 'Contents: write' and 'Pull requests: write' on this repo"

if $HAVE_GH && confirm "Set these secrets now via gh (input hidden)?"; then
  read -r -s -p "ANTHROPIC_API_KEY: " ANTHROPIC_KEY; echo
  if [[ -n "$ANTHROPIC_KEY" ]]; then
    gh secret set ANTHROPIC_API_KEY --repo "$REPO_SLUG" --body "$ANTHROPIC_KEY"
    echo "ANTHROPIC_API_KEY set."
  fi
  read -r -s -p "AGENT_GITHUB_TOKEN: " AGENT_TOKEN; echo
  if [[ -n "$AGENT_TOKEN" ]]; then
    gh secret set AGENT_GITHUB_TOKEN --repo "$REPO_SLUG" --body "$AGENT_TOKEN"
    echo "AGENT_GITHUB_TOKEN set."
  fi
else
  echo "Manual step -- Settings > Secrets and variables > Actions > Secrets > New repository secret:"
  echo "  ANTHROPIC_API_KEY"
  echo "  AGENT_GITHUB_TOKEN"
fi

# --- 6. Ensure a dev branch exists --------------------------------------------
cd "$TARGET_DIR"
if git show-ref --verify --quiet refs/heads/dev; then
  info "Local 'dev' branch already exists."
elif git ls-remote --exit-code --heads origin dev >/dev/null 2>&1; then
  info "Remote 'dev' branch already exists."
else
  info "No 'dev' branch found -- the workflow opens PRs against 'dev'."
  if confirm "Create and push 'dev' from the current HEAD now?"; then
    git branch dev
    git push origin dev
    echo "'dev' branch created and pushed."
  else
    echo "Skipped. Create it yourself before the agent's first PR, or PR creation will fail."
  fi
fi

# --- 7. Done -------------------------------------------------------------------
bold "Done."
cat <<EOF

Next steps:
1. The workflow's dependency-install step assumes a Node project. If this
   repo uses a different runtime, edit the "Set up Node / Python / etc." and
   "Install project dependencies" steps in:
     $WORKFLOW_DEST
2. Have $ALLOWED_USER open an issue on $REPO_SLUG using the "Agent Task"
   template (see $ISSUE_TEMPLATE_DEST), filling in Type, Description, and
   Acceptance Criteria. The Tests section is optional -- the agent writes
   tests either way.
3. Watch the Actions tab for the "Issue Agent" workflow run.

See README.md for full details.
EOF
