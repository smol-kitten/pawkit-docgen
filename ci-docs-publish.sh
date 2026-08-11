#!/usr/bin/env bash
# ci-docs-publish.sh — Phase 2 of the autowiki rework: regenerate + commit the durable
# .claude/** discovery copy in CI, post-merge on the default branch, WITHOUT retriggering CI.
#
# Runs SYNCHRONOUSLY (unlike bg-refresh.sh, which detaches and returns immediately — wrong for
# CI). Reuses the exact on-box tooling by materializing it at its hardcoded home
# (/home/claude/.claude-memory), so none of the ~12 hardcoded-path refs need refactoring.
#
# TOOLING SOURCE = polo-nyan/pawkit (scripts at repo ROOT; PRIVATE — the workflow supplies a
# read PAT). The copy-to-home below handles both root layout (pawkit) and a claude-memory/
# subdir layout, so it works regardless of which mirror is checked out to $TOOLING_SRC.
#
# Contract: publish-only, deterministic (no model in CI → AI passes self-skip), commit with
# [skip ci] so it cannot loop. Fail-open: any generator error still lets the commit step run
# over whatever regenerated cleanly.
#
# Usage (from the workflow, repo checked out at $PWD, tooling checked out at $TOOLING_SRC):
#   TOOLING_SRC=.tooling ci-docs-publish.sh
set -uo pipefail

REPO="${GITHUB_WORKSPACE:-$PWD}"
TOOLING_SRC="${TOOLING_SRC:-.tooling}"           # checkout of polo-nyan/claude
HOME_DIR=/home/claude/.claude-memory             # the hardcoded tooling home

# 1) materialize the tooling at its expected home (idempotent).
mkdir -p /home/claude
if [ ! -e "$HOME_DIR" ]; then
  # the repo lays the scripts under claude-memory/ (see polo-nyan/claude)
  if [ -d "$TOOLING_SRC/claude-memory" ]; then
    cp -a "$TOOLING_SRC/claude-memory" "$HOME_DIR"
  else
    cp -a "$TOOLING_SRC" "$HOME_DIR"
  fi
fi

export SYMBOLS_PUBLISH=1                          # symbols.py → in-repo committed copy + wiki maps
export AUTOWIKI_PUBLISH=1                          # parity flag for anything that reads it
export TEL_AUTO=1
PY=python3

echo "::group::regenerate discovery copy"
# Deterministic generators only (no model in CI; the AI autodoc/FAQ pass self-skips on
# llm_util.power_level()==0). Order: symbol index first (wiki maps derive from it), then
# repo-map + wiki upkeep, then AGENTS.md.
"$PY" "$HOME_DIR/symbols.py"  gen --publish --cwd "$REPO" || echo "warn: symbols gen failed"
"$PY" "$HOME_DIR/repomap.py"  gen           --cwd "$REPO" || echo "warn: repomap gen failed"
"$PY" "$HOME_DIR/wiki.py"     crosslink     --cwd "$REPO" || true
"$PY" "$HOME_DIR/wiki.py"     synopsis      --cwd "$REPO" || true
"$PY" "$HOME_DIR/wiki.py"     patchnotes    --cwd "$REPO" || true
"$PY" "$HOME_DIR/agentsmd.py" check         --cwd "$REPO" >/dev/null 2>&1 || \
  "$PY" "$HOME_DIR/agentsmd.py" gen         --cwd "$REPO" || true
echo "::endgroup::"

# 2) commit only the discovery paths, with [skip ci] so no workflow re-triggers.
cd "$REPO" || exit 0
git add -- .claude AGENTS.md 2>/dev/null || true
if git diff --cached --quiet; then
  echo "docs-refresh: nothing to publish — clean."
  exit 0
fi
git config user.name  "polo-nyan[bot]"
git config user.email "polo-nyan-bot@users.noreply.github.com"
git commit -m "chore(docs): refresh indexes + wiki [skip ci]" \
  --trailer "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push origin "HEAD:${GITHUB_REF_NAME:-main}"
echo "docs-refresh: published."
