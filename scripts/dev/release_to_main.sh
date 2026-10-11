#!/usr/bin/env bash
# Promote origin/develop to origin/main safely, from a throwaway worktree.
#
#   scripts/dev/release_to_main.sh            # test, then push main
#   scripts/dev/release_to_main.sh --dry-run  # merge and test, do not push
#
# Steps: fetch; worktree off origin/main on a temporary branch; fast-forward
# it to origin/main; merge origin/develop; run the full suite; push the
# temporary branch to main (never forced); remove the worktree and branch.
# It never touches your checkout. Set CW_WORK to change the worktree folder
# (default /private/tmp/cw-work). Needs the repo's .venv for the tests.
set -euo pipefail
dry=0; [ "${1:-}" = "--dry-run" ] && dry=1
repo=$(git rev-parse --show-toplevel)
work=${CW_WORK:-/private/tmp/cw-work}
dir="$work/release-main"
branch="tmp-release-main-$$"
python="$repo/.venv/bin/python"; [ -x "$python" ] || python=python3
mkdir -p "$work"
git -C "$repo" fetch -q origin
[ ! -e "$dir" ] || { echo "refusing: $dir exists (remove it with: git worktree remove --force $dir)"; exit 1; }
git -C "$repo" worktree add -q "$dir" -b "$branch" origin/main
cleanup() { git -C "$repo" worktree remove --force "$dir" 2>/dev/null || true; git -C "$repo" branch -D "$branch" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cd "$dir"
git merge --ff-only -q origin/main
git merge --no-edit -q origin/develop
echo "merged develop into main candidate: $(git log --oneline -1)"
"$python" -m pytest -q
if [ "$dry" = 1 ]; then echo "dry run: tests passed, not pushing"; exit 0; fi
git fetch -q origin
git merge-base --is-ancestor origin/main HEAD || { echo "origin/main moved; rerun"; exit 1; }
git push origin "$branch:main"
echo "pushed to main"
