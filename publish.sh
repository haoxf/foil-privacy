#!/usr/bin/env bash
set -euo pipefail

EXPECTED_REPOSITORY="haoxf/foil-privacy"
EXPECTED_REMOTE="https://github.com/${EXPECTED_REPOSITORY}.git"
BRANCH="main"

cd "$(dirname "$0")"

if [[ ! -d .git ]]; then
  echo "Error: this directory is not a Git repository." >&2
  exit 1
fi

actual_remote="$(git remote get-url origin 2>/dev/null || true)"
case "$actual_remote" in
  "$EXPECTED_REMOTE"|"https://github.com/${EXPECTED_REPOSITORY}"|"git@github.com:${EXPECTED_REPOSITORY}.git") ;;
  *)
    echo "Error: origin is '$actual_remote', expected '$EXPECTED_REMOTE'." >&2
    exit 1
    ;;
esac

if [[ "$(git branch --show-current)" != "$BRANCH" ]]; then
  echo "Error: publish from the '$BRANCH' branch." >&2
  exit 1
fi

git add -- index.html README.md publish.sh heartime/index.html heartime/support/index.html

if git diff --cached --quiet; then
  echo "Nothing to publish."
  exit 0
fi

commit_message="Update Foil public support pages"
if [[ $# -gt 0 ]]; then
  commit_message="$*"
fi

git commit -m "$commit_message"
git push origin "$BRANCH"

echo "Published: https://haoxf.github.io/foil-privacy/"
