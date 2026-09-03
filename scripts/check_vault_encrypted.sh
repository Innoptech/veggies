#!/usr/bin/env bash
# Fail if any given file is not ansible-vault encrypted.
# With no arguments, checks secrets/*.yml and secrets/*.yaml.
# Files ending in .example are always skipped.
set -euo pipefail

files=("$@")
if [ "${#files[@]}" -eq 0 ]; then
  shopt -s nullglob
  files=(secrets/*.yml secrets/*.yaml)
fi

fail=0
for f in "${files[@]}"; do
  case "$f" in
    *.example) continue ;;
  esac
  if ! head -c 15 "$f" | grep -q '^[$]ANSIBLE_VAULT;'; then
    echo "NOT ENCRYPTED: $f" >&2
    fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "OK: all checked files are vault-encrypted"
fi
exit "$fail"
