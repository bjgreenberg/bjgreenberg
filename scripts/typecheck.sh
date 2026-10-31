#!/usr/bin/env bash
# Type-check gate: mypy --strict over every .py file in the repo. The file set
# and the flags live in setup.cfg [mypy] (files = .), never on this command
# line, so a new .py file anywhere is checked the day it lands. Shared by local
# runs and CI. Prefers the dev venv's mypy (.venv or venv); CI installs
# requirements-dev.txt onto PATH.
#
# It also asserts the count: the number of files mypy reports checking must
# equal the number of .py files git sees (tracked plus untracked, minus
# .gitignore'd ones such as the venvs), so a config change that silently drops
# files fails here instead of passing on a smaller set.
set -euo pipefail
cd "$(dirname "$0")/.."
for v in .venv venv; do
  if [[ -x "$v/bin/mypy" ]]; then PATH="$PWD/$v/bin:$PATH"; break; fi
done
mypy --version

expected=$(git ls-files --cached --others --exclude-standard -- '*.py' | wc -l | tr -d ' ')
if [[ "$expected" -eq 0 ]]; then
  echo "typecheck: git sees no .py files; refusing to pass on an empty set" >&2
  exit 1
fi

rc=0
out=$(mypy 2>&1) || rc=$?
printf '%s\n' "$out"
if [[ "$rc" -ne 0 ]]; then
  exit "$rc"
fi

checked=$(printf '%s\n' "$out" | sed -nE 's/^Success: no issues found in ([0-9]+) source files?$/\1/p')
if [[ "$checked" != "$expected" ]]; then
  echo "typecheck: mypy checked ${checked:-an unknown number of} files but git sees $expected .py files" >&2
  exit 1
fi
echo "typecheck: $checked of $expected .py files checked"
