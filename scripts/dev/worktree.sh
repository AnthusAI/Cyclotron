#!/usr/bin/env bash
# Keep temporary worktrees in ONE folder and clean them up safely.
#
#   scripts/dev/worktree.sh new NAME [BASE]   # worktree CW_WORK/NAME on branch NAME off BASE (default origin/develop)
#   scripts/dev/worktree.sh clean             # remove worktrees under CW_WORK whose branch is merged into origin/develop
#   scripts/dev/worktree.sh list
#
# `clean` skips any worktree with uncommitted or untracked changes and any
# branch not yet merged, so it cannot lose work. Never delete these folders
# with a wildcard `rm -rf`; other tools put files next to them.
set -euo pipefail
repo=$(git rev-parse --show-toplevel)
work=${CW_WORK:-/private/tmp/cw-work}
cmd=${1:-list}
case "$cmd" in
  new)
    name=${2:?name required}; base=${3:-origin/develop}
    mkdir -p "$work"; git -C "$repo" fetch -q origin
    git -C "$repo" worktree add "$work/$name" -b "$name" "$base"
    echo "$work/$name" ;;
  list) git -C "$repo" worktree list ;;
  clean)
    git -C "$repo" fetch -q origin
    git -C "$repo" worktree list --porcelain | awk '/^worktree /{p=$2} /^branch /{print p" "$2}' | while read -r path ref; do
      case "$path" in "$work"/*) ;; *) continue ;; esac
      b=${ref#refs/heads/}
      if [ -n "$(git -C "$path" status --porcelain)" ]; then echo "skip $path (uncommitted changes)"; continue; fi
      if git -C "$repo" merge-base --is-ancestor "$b" origin/develop; then
        git -C "$repo" worktree remove "$path" && git -C "$repo" branch -d "$b" >/dev/null && echo "removed $path"
      else echo "skip $path (branch $b not merged into origin/develop)"; fi
    done
    git -C "$repo" worktree prune ;;
  *) echo "usage: worktree.sh new NAME [BASE] | clean | list"; exit 1 ;;
esac
