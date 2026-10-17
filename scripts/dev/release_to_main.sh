#!/usr/bin/env bash
# Promote origin/develop to origin/main safely, from a throwaway worktree.
#
#   scripts/dev/release_to_main.sh            # run make check, then push main
#   scripts/dev/release_to_main.sh --dry-run  # merge and test, do not push
#
# Steps: fetch; worktree off origin/main on a temporary branch; fast-forward
# it to origin/main; merge origin/develop; run the full gate (make check); push the
# temporary branch to main (never forced); wait for the release commit and merge
# main back into develop so develop reports the released version; remove the
# worktree and branch.
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
make check PYTHON="$python"
if [ "$dry" = 1 ]; then echo "dry run: make check passed, not pushing"; exit 0; fi
git fetch -q origin
git merge-base --is-ancestor origin/main HEAD || { echo "origin/main moved; rerun"; exit 1; }
git push origin "$branch:main"
echo "pushed to main"

# The release workflow runs semantic-release on main, which adds the version
# bump commit. Wait for it, then merge main back into develop so develop
# reports the released version. RELEASE_WAIT_SECONDS changes the wait (default 900).
pushed_main=$(git rev-parse HEAD)
deadline=$((SECONDS + ${RELEASE_WAIT_SECONDS:-900}))
bumped=0
while [ "$SECONDS" -lt "$deadline" ]; do
  git fetch -q origin
  if [ "$(git rev-parse origin/main)" != "$pushed_main" ] && git merge-base --is-ancestor "$pushed_main" origin/main; then bumped=1; break; fi
  sleep 20
done
if [ "$bumped" != 1 ]; then
  echo "no release commit on main yet; later run: git fetch origin && git switch develop && git merge origin/main && git push origin develop"
  exit 0
fi
git switch -q -c "$branch-develop" origin/develop
git merge --no-edit -q origin/main
git push origin "$branch-develop:develop"
git switch -q "$branch"
git branch -D "$branch-develop" >/dev/null
echo "merged main back into develop: $(git log origin/main --oneline -1)"
