#!/usr/bin/env bash
# Validate the host mount before Compose sees its bind path; the container guard repeats the check at every start.
set -euo pipefail

usage() { echo "usage: encrypted-state.sh init|check DIRECTORY /dev/mapper/NAME [IDENTITY]" >&2; exit 2; }
[[ $# -ge 3 ]] || usage
action=$1
directory=$2
source=$3
[[ "$source" == /dev/mapper/* ]] || { echo "expected a mapped device" >&2; exit 1; }
[[ -d "$directory" && ! -L "$directory" ]] || { echo "state directory is absent or a symlink" >&2; exit 1; }
mounted=$(findmnt -rn --mountpoint "$directory" -o SOURCE)
[[ "$mounted" == "$source" ]] || { echo "encrypted state mount is absent or has the wrong source" >&2; exit 1; }
[[ $(lsblk -dn -o TYPE "$source") == crypt ]] || { echo "state source is not a dm-crypt mapping" >&2; exit 1; }

marker="$directory/.encrypted-state-id"
case "$action" in
  init)
    [[ $# -eq 3 ]] || usage
    [[ ! -e "$marker" && ! -L "$marker" ]] || { echo "state identity already exists" >&2; exit 1; }
    identity=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
    (set -C; umask 022; printf '%s\n' "$identity" > "$marker")
    echo "$identity"
    ;;
  check)
    [[ $# -eq 4 && "$4" =~ ^[0-9a-f]{32}$ ]] || usage
    [[ -f "$marker" && ! -L "$marker" ]] || { echo "state identity is absent" >&2; exit 1; }
    [[ $(cat "$marker") == "$4" ]] || { echo "state identity does not match" >&2; exit 1; }
    ;;
  *) usage ;;
esac
