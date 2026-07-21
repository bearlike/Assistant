#!/usr/bin/env bash
# Grove workspace init — seeds the local agent instruction layer.
#
# Grove runs this with cwd set to the NEW worktree, once on create and again on
# resume (`init_script.run_on_resume`). Wired up in .grove/config.json.
#
# Why this exists: CLAUDE.local.md is gitignored, so it is absent from every
# fresh worktree — including the ones Grove builds for a workspace. Without this
# copy, an agent in a Grove workspace starts with none of that context. `pause`
# deletes the worktree and `resume` rebuilds it, so the copy must run on both.
#
# Never fails a workspace: a missing source file is normal (a machine that has
# no local guide) and must not block workspace creation, so this always exits 0.

set -uo pipefail

# Resolve the MAIN repo root, which is where the authoritative copy lives.
#
# Prefer $GROVE_REPO, but do NOT rely on it: Grove passes extra_env on create
# and NOT on resume, so on the resume path it is unset. `git rev-parse
# --git-common-dir` works from inside a worktree in both cases — it points at
# the main repo's .git, whose parent is the main working tree.
main_root="${GROVE_REPO:-}"
if [ -z "$main_root" ]; then
  common_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || {
    echo "grove-init: not a git repository; skipping"
    exit 0
  }
  # --git-common-dir may return a relative path; anchor it to cwd before use.
  case "$common_dir" in
    /*) ;;
    *) common_dir="$PWD/$common_dir" ;;
  esac
  main_root="$(cd "$common_dir/.." && pwd)" || exit 0
fi

src="$main_root/CLAUDE.local.md"
dst="./CLAUDE.local.md"

if [ ! -f "$src" ]; then
  echo "grove-init: no CLAUDE.local.md at $main_root; skipping"
  exit 0
fi

# Guard the degenerate case: a "workspace" rooted at the main repo would copy
# the file onto itself and truncate it.
if [ "$(cd "$(dirname "$dst")" && pwd)" = "$main_root" ]; then
  echo "grove-init: workspace IS the main repo; nothing to copy"
  exit 0
fi

if cp "$src" "$dst"; then
  echo "grove-init: seeded CLAUDE.local.md from $main_root"
else
  echo "grove-init: WARNING could not copy CLAUDE.local.md; continuing"
fi

exit 0
