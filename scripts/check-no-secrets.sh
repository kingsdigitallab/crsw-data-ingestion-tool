#!/usr/bin/env sh
# Fail if anything that looks like a storage credential is tracked by git.
# Run from the repo root; intended as a CI step and a pre-push habit.
set -eu

# .env itself must never be tracked, whatever it contains.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "FAIL: .env is tracked by git" >&2
  exit 1
fi

# AWS/Ceph-style access key ids and 40-char secrets, and literal
# assignments of the service variables with a non-empty value.
pattern='AKIA[0-9A-Z]{16}|(aws_)?secret(_access)?_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+]{20,}|CRSW_S3_(ACCESS|SECRET)_KEY=[^[:space:]]+'
if git ls-files -z | xargs -0 grep -nE "$pattern" -- 2>/dev/null; then
  echo "FAIL: possible credential in a tracked file (see above)" >&2
  exit 1
fi

echo "ok: no credentials in tracked files"
