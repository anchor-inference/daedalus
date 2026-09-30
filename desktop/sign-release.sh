#!/bin/sh
# Signs a draft launcher release, on the operator's own machine:
#
#   desktop/sign-release.sh desktop-v0.13.0                 # the key minisign keeps by default
#   desktop/sign-release.sh desktop-v0.13.0 ~/keys/daedalus.key
#   desktop/sign-release.sh desktop-v0.13.0 --publish        # and publish the draft once it is signed
#
# The release workflow builds the archives and SHA256SUMS into a draft and signs nothing: the
# project's release key never leaves the operator's machine, so no workflow, runner or server can
# sign with it. This downloads the draft's SHA256SUMS with gh, builds the exact message the launcher
# and the installers verify -
#
#   "daedalus-release <tag>\n" followed by SHA256SUMS exactly as published
#
# - signs it with `minisign -S -l` (legacy: plain Ed25519 over the message, which is what the
# launcher's verifier and OpenSSL check; minisign's default prehashed signature is not), checks the
# signature against the key the installers carry, and uploads SHA256SUMS.sig beside SHA256SUMS.
# A launcher refuses a release without it, so the draft is published only after this.
#
# Needs gh (signed in to an account that may edit the repository's releases) and minisign.
# DAEDALUS_REPO names another repository.
set -eu

usage() {
  echo "usage: $0 <desktop-vX.Y.Z tag> [secret key file] [--publish]" >&2
  exit 2
}

fail() {
  echo "$*" >&2
  exit 1
}

tag=""
key=""
publish=""
for arg in "$@"; do
  case "$arg" in
    --publish) publish=1 ;;
    -*) usage ;;
    *)
      if [ -z "$tag" ]; then tag="$arg"; elif [ -z "$key" ]; then key="$arg"; else usage; fi
      ;;
  esac
done
[ -n "$tag" ] || usage
# The same tags the launcher calls versions (release.go): no leading zeros, nothing after.
printf '%s\n' "$tag" | grep -Eq '^desktop-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ||
  fail "$tag is not a desktop-vX.Y.Z tag."
repo="${DAEDALUS_REPO:-anchor-inference/daedalus}"
here="$(cd "$(dirname "$0")" && pwd)"

command -v gh >/dev/null 2>&1 || fail "gh is not installed: https://cli.github.com"
command -v minisign >/dev/null 2>&1 || fail "minisign is not installed: https://jedisct1.github.io/minisign"

# The public key the installers carry, which is the one compiled into the launcher: the signature is
# checked against what will check it.
public="$(sed -n 's/^release_keys="\(.*\)"$/\1/p' "$here/install.sh")"
[ -n "$public" ] || fail "no release key in $here/install.sh"

[ "$(gh release view "$tag" --repo "$repo" --json isDraft --jq .isDraft)" = "true" ] ||
  fail "$tag is not a draft release of $repo. A published release is not signed afterwards: every launcher that looked at it meanwhile refused it."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM
gh release download "$tag" --repo "$repo" --pattern SHA256SUMS --dir "$work"
[ -s "$work/SHA256SUMS" ] || fail "$tag has no SHA256SUMS."

# Every file SHA256SUMS names must be attached to the release: a signature over a list that does
# not match the release signs something nobody can download.
assets="$(gh release view "$tag" --repo "$repo" --json assets --jq '.assets[].name')"
while read -r sum name; do
  [ -n "$name" ] || continue
  name="${name#\*}"
  printf '%s\n' "$assets" | grep -qxF "$name" || fail "SHA256SUMS names $name, which is not attached to $tag."
done <"$work/SHA256SUMS"

echo "SHA256SUMS of $tag, as the workflow published it - compare it with the run's log before signing:"
cat "$work/SHA256SUMS"

printf 'daedalus-release %s\n' "$tag" >"$work/message"
cat "$work/SHA256SUMS" >>"$work/message"

# minisign asks for the key's password on the terminal.
if [ -n "$key" ]; then
  minisign -S -l -s "$key" -m "$work/message" -x "$work/SHA256SUMS.sig" -t "daedalus-release $tag"
else
  minisign -S -l -m "$work/message" -x "$work/SHA256SUMS.sig" -t "daedalus-release $tag"
fi
minisign -V -P "$public" -m "$work/message" -x "$work/SHA256SUMS.sig" >/dev/null ||
  fail "The signature does not verify with the release key the installers carry; nothing was uploaded. Is this the project's key?"

gh release upload "$tag" "$work/SHA256SUMS.sig" --repo "$repo" --clobber
echo "Signed: SHA256SUMS.sig is attached to $tag."
if [ -n "$publish" ]; then
  gh release edit "$tag" --repo "$repo" --draft=false
  echo "Published $tag."
else
  echo "Publish it when ready:  gh release edit $tag --repo $repo --draft=false"
fi
