#!/usr/bin/env bash
# Stages agent-desktop, the accessibility backend deskd runs, for the packages: the release archives
# desktop/agent-desktop.lock names, checked and unpacked one folder per platform.
#
#   desktop/stage-agent-desktop.sh <out-dir> [platform ...]     # every platform in the lock by default
#
# It is a program of someone else's that runs with Daedalus's Accessibility grant on the operator's
# machine, so nothing reaches a package unless all of this holds, and the script stops at the first
# thing that does not:
#
#   - the archive's sha256 is the lock's for that platform;
#   - its build provenance verifies (`gh attestation verify`): signed through Sigstore by the
#     project's own release workflow on a GitHub-hosted runner, built from the commit the lock pins.
#     The digest proves these are the bytes that were reviewed; the attestation proves those bytes
#     came out of that project's build of that commit and were not uploaded by hand;
#   - the archive holds exactly the files that platform ships, each a regular file, and nothing else:
#     no directory, no link, no path that could land outside the folder.
#
# What it writes:
#
#   <out-dir>/<platform>/agent-desktop[.exe]          and the helper it runs beside it on Linux
#                                                     (agent-desktop-linux-clipboard-owner) and on
#                                                     macOS (agent-desktop-macos-helper)
#   <out-dir>/THIRD_PARTY_NOTICES.txt                 its licence, which every package carries
#   <out-dir>/SHA256SUMS                              of every file above, which the packaging jobs
#                                                     check again before they copy anything
#
# Each file is mode 0755: agent-desktop refuses a helper that a group or other users may write.
#
# DAEDALUS_AGENT_DESKTOP_ARCHIVES=<dir> takes the archives from disk, under their released names, for
# a build without the network; they are held to the same digests. DAEDALUS_AGENT_DESKTOP_ATTESTATION
# =skip leaves out the provenance check, for a machine whose gh is older than `gh attestation`
# (2.49); the release workflow never sets it. GH=<path> names the gh to use.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:?usage: stage-agent-desktop.sh <out-dir> [platform ...]}"
shift
lock="$root/desktop/agent-desktop.lock"
license="$root/desktop/third-party/agent-desktop.LICENSE"
gh="${GH:-gh}"

fail() {
  echo "agent-desktop: $*" >&2
  exit 1
}

# The lock as text with every line trimmed of a carriage return and surrounding blanks: a Windows
# checkout that converts line ends hands it over with CRLF, and "1.0.2\r" is not a version.
locktext="$(tr -d '\r' <"$lock" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
field() { sed -n "s/^$1=//p" <<<"$locktext"; }
version="$(field version)"
commit="$(field commit)"
repo="$(field repo)"
workflow="$(field workflow)"
template="$(field url)"
[ -n "$version" ] && [ -n "$repo" ] && [ -n "$workflow" ] && [ -n "$template" ] ||
  fail "$lock needs version=, repo=, workflow= and url= lines"
grep -Eqx '[0-9a-f]{40}' <<<"$commit" || fail "$lock needs commit= with a full commit id"

if [ "$#" -eq 0 ]; then
  # A loop, not mapfile: the Mac's own bash is 3.2.
  while read -r platform; do
    set -- "$@" "$platform"
  done < <(sed -n 's/^\([a-z]*-[a-z0-9]*\)=.*/\1/p' <<<"$locktext")
fi
[ "$#" -gt 0 ] || fail "$lock names no platform"

# The Mac has shasum and no sha256sum; both read and write the same list format.
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }
else
  sha256() { shasum -a 256 "$@"; }
fi

if [ "${DAEDALUS_AGENT_DESKTOP_ATTESTATION:-}" = skip ]; then
  echo "agent-desktop: DAEDALUS_AGENT_DESKTOP_ATTESTATION=skip: the provenance is NOT checked, only the digests" >&2
elif ! "$gh" attestation verify --help >/dev/null 2>&1; then
  fail "$gh has no 'attestation verify' (gh 2.49 or later has it); install a newer gh, or set DAEDALUS_AGENT_DESKTOP_ATTESTATION=skip for a build that is not a release"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
rm -rf "$out"
mkdir -p "$out"

for platform in "$@"; do
  line="$(field "$platform")"
  [ -n "$line" ] || fail "$lock has no line for $platform"
  target="${line% *}"
  want="${line#* }"
  grep -Eqx '[0-9a-f]{64}' <<<"$want" || fail "$lock: the sha256 for $platform is not 64 hex digits"
  case "$platform" in
    linux-*) files=(agent-desktop agent-desktop-linux-clipboard-owner) ;;
    darwin-*) files=(agent-desktop agent-desktop-macos-helper) ;;
    windows-*) files=(agent-desktop.exe) ;;
    *) fail "$platform is not a platform this script knows the files of" ;;
  esac

  url="$(printf '%s' "$template" | sed -e "s/{version}/$version/g" -e "s/{target}/$target/g")"
  name="${url##*/}"
  archive="$work/$name"
  if [ -n "${DAEDALUS_AGENT_DESKTOP_ARCHIVES:-}" ]; then
    cp "$DAEDALUS_AGENT_DESKTOP_ARCHIVES/$name" "$archive"
  else
    curl -fsSL --retry 3 -o "$archive" "$url"
  fi

  got="$(sha256 "$archive" | cut -d' ' -f1)"
  [ "$got" = "$want" ] || fail "$name has sha256 $got; desktop/agent-desktop.lock pins $want for $platform"

  if [ "${DAEDALUS_AGENT_DESKTOP_ATTESTATION:-}" != skip ]; then
    # Every attestation printed has verified against the signer workflow; one of them must be from
    # the pinned commit. A release re-run from another commit attests the same digest only if it
    # rebuilt identical bytes, and then the pinned commit's attestation is there as well.
    sources="$("$gh" attestation verify "$archive" --repo "$repo" --signer-workflow "$repo/$workflow" \
      --deny-self-hosted-runners --format json --jq '.[].verificationResult.signature.certificate.sourceRepositoryDigest')" ||
      fail "$name: its build provenance does not verify for $repo/$workflow"
    grep -qxF "$commit" <<<"$sources" ||
      fail "$name: its provenance names the commits $(tr '\n' ' ' <<<"$sources")and not the pinned $commit"
  fi

  # The listing, read before anything is unpacked: exactly the expected names, each a regular file
  # ("-" in the long listing), so no entry is a directory, a link or a path with a slash in it.
  # Each listing is read whole into a variable first: grep -q stopping early on a pipe from tar
  # would kill tar, and under pipefail that failure would make the test pass a bad entry.
  expected="$(printf '%s\n' "${files[@]}" | LC_ALL=C sort)"
  listed="$(tar -tzf "$archive" | LC_ALL=C sort)"
  [ "$listed" = "$expected" ] || fail "$name lists $(tr '\n' ' ' <<<"$listed")instead of exactly ${files[*]}"
  long="$(tar -tvzf "$archive")"
  if grep -qv '^-' <<<"$long"; then
    fail "$name holds an entry that is not a regular file"
  fi

  dir="$out/$platform"
  mkdir -p "$dir"
  tar -xzf "$archive" -C "$dir" --no-same-owner "${files[@]}"
  for file in "${files[@]}"; do
    chmod 0755 "$dir/$file"
  done
  echo "agent-desktop $version for $platform: sha256 $want, provenance $([ "${DAEDALUS_AGENT_DESKTOP_ATTESTATION:-}" = skip ] && echo 'NOT checked' || echo "verified at ${commit:0:12}")"
done

{
  echo "Daedalus carries agent-desktop $version, unmodified, from"
  echo "https://github.com/$repo (commit $commit),"
  echo "as the program its desktop daemon runs to read and act on the accessibility tree. It is"
  echo "distributed under the Apache License, Version 2.0, whose text follows."
  echo
  cat "$license"
} >"$out/THIRD_PARTY_NOTICES.txt"

(
  cd "$out"
  find . -type f -print | sed 's|^\./||' | LC_ALL=C sort | while read -r file; do
    sha256 "$file"
  done >"$work/SHA256SUMS"
)
mv "$work/SHA256SUMS" "$out/SHA256SUMS"
echo "staged agent-desktop $version into $out"
