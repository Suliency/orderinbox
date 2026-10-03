#!/usr/bin/env bash
# Rebuild the Odoo Apps Store branch (default 18.0) from master.
# The store scans a version-named branch and expects module folders at the
# repo root, so this branch holds only addons/orderinbox -> orderinbox/.
set -euo pipefail

BRANCH="${1:-18.0}"
ROOT="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d)"
trap 'git -C "$ROOT" worktree remove --force "$WORK" 2>/dev/null || true; rm -rf "$WORK"' EXIT

cd "$ROOT"
if git show-ref --quiet "refs/heads/$BRANCH"; then
  git worktree add "$WORK" "$BRANCH"
else
  git worktree add --detach "$WORK"
  git -C "$WORK" checkout --orphan "$BRANCH"
fi

git -C "$WORK" rm -rq --ignore-unmatch . 
git -C "$WORK" clean -fdxq
git archive HEAD addons/orderinbox | tar -x -C "$WORK" --strip-components=1
cp README.md "$WORK/README.md"

git -C "$WORK" add -A
if git -C "$WORK" diff --cached --quiet; then
  echo "$BRANCH already up to date"
else
  git -C "$WORK" commit -qm "release: orderinbox module from $(git rev-parse --short HEAD)"
  echo "Built $BRANCH. Push with: git push origin $BRANCH"
fi
