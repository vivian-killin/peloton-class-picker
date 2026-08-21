#!/usr/bin/env bash
# Publish this folder to your GitHub repo, without leaking your identity.
#
#   ./setup-git.sh YOUR_GITHUB_USERNAME [repo-name]
#
# Commits are authored with GitHub's noreply address, so your real email never
# appears in the git history. Your Peloton login lives in .env, which is
# gitignored and never committed.

set -euo pipefail

USERNAME="${1:-}"
REPO="${2:-peloton-class-picker}"

if [ -z "$USERNAME" ]; then
  echo "Usage: ./setup-git.sh YOUR_GITHUB_USERNAME [repo-name]" >&2
  exit 1
fi

cd "$(dirname "$0")"

[ -d .git ] || git init -b main

# Repo-local identity only — your global git config is untouched.
git config user.name "$USERNAME"
git config user.email "$USERNAME@users.noreply.github.com"

git add .

# Refuse to publish if anything secret actually made it into the index. This checks
# the staged file list rather than .gitignore, so it catches a file that was added
# before the ignore rule existed.
for secret in .env taste_seed.json; do
  if git ls-files --cached --error-unmatch "$secret" >/dev/null 2>&1; then
    echo "Refusing to push: $secret is staged for commit. Run:" >&2
    echo "  git rm --cached $secret" >&2
    exit 1
  fi
done

if git diff --cached --quiet; then
  echo "Nothing new to commit."
else
  git commit -m "Peloton class picker: preference-based picker with a local web UI"
fi

git remote remove origin 2>/dev/null || true
# Prefer SSH when the key is already registered with GitHub; fall back to HTTPS.
# `ssh -T git@github.com` exits 1 even on success, so capture first, then test.
SSH_PROBE="$(ssh -o BatchMode=yes -T git@github.com 2>&1 || true)"
if printf '%s' "$SSH_PROBE" | grep -q "successfully authenticated"; then
  git remote add origin "git@github.com:$USERNAME/$REPO.git"
else
  git remote add origin "https://github.com/$USERNAME/$REPO.git"
fi

echo
echo "Files being published:"
git ls-files
echo

if ! git push -u origin main; then
  echo
  echo "Push was rejected — the GitHub repo probably already has a commit (a README"
  echo "or licence). Merging that in and retrying:"
  git pull --rebase origin main
  git push -u origin main
fi

echo
echo "Done: https://github.com/$USERNAME/$REPO"
