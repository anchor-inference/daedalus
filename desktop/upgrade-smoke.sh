#!/usr/bin/env bash
# Updates and upgrades, end to end, on this machine and nothing else: real launcher binaries — the
# old one built from the desktop-v0.12.0 tag's own source, the new ones from this tree — install.sh,
# and a release "server" that is python's http.server on a free loopback port, serving fixture
# releases out of a temporary folder. The installations and their data are fixtures in that folder
# too. No request leaves the machine, no container is started, and nothing outside the temporary
# folder is read or written.
#
# The new binaries are built with -tags upgradefixture, so the stack an upgrade or an update starts
# is the fixture in fixture_stack.go: it rewrites state/daedalus.sqlite the way a migration would
# and, when DAEDALUS_UPGRADE_FIXTURE=fail, fails the health check. It is used only for a data folder
# that carries the .upgrade-fixture marker. The v0.12.0 launcher never starts a stack here.
#
#   desktop/upgrade-smoke.sh            # Linux; needs go, git, python3, curl
#
# From v0.12.0, through install.sh (the bridge):
#   A  a fresh install takes the newest release
#   B  v0.12.0 → v0.13.0 works: data migrated, data and old launcher backed up and checked first
#   C  v0.12.0 → v0.13.0 whose health check fails: old launcher and data back byte for byte
#   D  no terminal and no yes: nothing changes
#   E  Docker mode: refused, nothing changes
#   F  a backup that cannot be written: refused before anything is replaced, nothing changes
# From v0.13.0, by the launcher itself:
#   G  check-update finds v0.14.0; upgrade to it
#   H  update: backed up first (the fence is off here); a failed start puts the data back; Docker refused
#   I  install.sh over a launcher that has upgrade hands over and changes nothing by itself
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s)" in Linux) ;; *) echo "this smoke runs the Linux release shape; see UPDATES.md" >&2; exit 2 ;; esac
case "$(uname -m)" in x86_64 | amd64) arch=amd64 ;; arm64 | aarch64) arch=arm64 ;; *) exit 2 ;; esac
asset="daedalus-desktop-linux-$arch.tar.gz"

work="${SMOKE_DIR:-$(mktemp -d)}"
# Every launcher here keeps its runtime and state under the smoke's folder, never in this user's
# real cache and state folders; and the data is protected by the verified backup, which is what
# these checks read back byte for byte (the fenced switch has its own tests, protect_linux_test.go).
export DAEDALUS_LOCAL_ROOT="$work/local" DAEDALUS_DATA_FENCE=off
mkdir -p "$work"
server_pid=""
cleanup() { if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; fi; }
trap cleanup EXIT

# Whatever the shell running this has set must not reach the launchers.
unset DAEDALUS_MODE DAEDALUS_GIT_REMOTE DAEDALUS_CORE_GIT_REMOTE DAEDALUS_UPGRADE_FIXTURE DAEDALUS_UPGRADE_YES || true
export DAEDALUS_UPDATE_CHECK=off

pass=0
ok() { pass=$((pass + 1)); printf 'PASS  %s\n' "$*"; }
die() { printf 'FAIL  %s\n' "$*" >&2; exit 1; }
expect_eq() { [ "$1" = "$2" ] || die "$3: got '$1', want '$2'"; ok "$3"; }
stage_of() { python3 -c 'import json,sys; j=json.load(open(sys.argv[1])); print(j.get("kind","")+":"+j["stage"])' "$(dirname "$1")/.daedalus-update/$(basename "$1")/upgrade.json"; }
# tree <dir>: every file under it with its hash, links with their targets — the whole state.
# The installation lock (data/upgrade/.lock) is the one file a refused attempt may leave: it is
# never deleted, because removing a locked file lets two processes each hold "the" lock.
tree() { (cd "$1" && find . \( -path '*/data/upgrade' -o -path './upgrade' \) -prune -o \( -type f -o -type l \) -print0 | sort -z | xargs -0 -r sha256sum 2>/dev/null; find . \( -path '*/data/upgrade' -o -path './upgrade' \) -prune -o -type l -printf '%p -> %l\n' | sort; find . \( -path '*/data/upgrade' -o -path './upgrade' \) -prune -o -type d -print | sort); }

echo "work folder: $work"

# A release key made for this run, in the project key's place: every release below is signed with it,
# the new launchers are built to trust it and nothing else, and the installer run is install.sh with
# it written where the project's key is. OpenSSL makes and uses the Ed25519 key; python lays out the
# signify records (the "Ed" tag, an 8-byte key id, then the key or the signature).
key="$work/release-key.pem"
mkdir -p "$work"
openssl genpkey -algorithm ed25519 -out "$key" 2>/dev/null || die "this OpenSSL cannot make an Ed25519 key"
record() { python3 -c 'import base64,sys; print(base64.b64encode(b"Ed"+b"smoke001"+sys.stdin.buffer.read()[-32:] if sys.argv[1]=="key" else b"Ed"+b"smoke001"+sys.stdin.buffer.read()).decode())' "$1"; }
public="$(openssl pkey -in "$key" -pubout -outform DER | record key)"
sign() { # sign <tag> <SHA256SUMS> <out>: the signature over the tag line and SHA256SUMS
  printf 'daedalus-release %s\n' "$1" >"$3.message"
  cat "$2" >>"$3.message"
  openssl pkeyutl -sign -inkey "$key" -rawin -in "$3.message" -out "$3.raw"
  { printf 'untrusted comment: smoke\n'; record sig <"$3.raw"; } >"$3"
  rm -f "$3.message" "$3.raw"
}
sed "s|^release_keys=\".*\"\$|release_keys=\"$public\"|" install.sh >"$work/install.sh"
grep -qx "release_keys=\"$public\"" "$work/install.sh" || die "the installer's key line was not replaced"

# The launchers.
mkdir -p "$work/bin" "$work/src-v0.12.0"
git -C .. archive desktop-v0.12.0 desktop | tar -x -C "$work/src-v0.12.0"
(cd "$work/src-v0.12.0/desktop" && CGO_ENABLED=0 go build -tags nowebview -trimpath -ldflags "-X main.version=desktop-v0.12.0" -o "$work/bin/v0.12.0" .)
build() { CGO_ENABLED=0 go build -tags nowebview,upgradefixture -trimpath -ldflags "-X main.version=$1 -X main.linkedFixtureReleaseKey=$public" -o "$2" .; }
build desktop-v0.13.0 "$work/bin/v0.13.0"
build desktop-v0.14.0 "$work/bin/v0.14.0"
"$work/bin/v0.12.0" --help | grep -q '^  upgrade ' && die "the v0.12.0 build knows upgrade; it is not the old launcher"
ok "the old launcher is built from the desktop-v0.12.0 tag and has no upgrade command"

# The release server.
srv="$work/srv"
mkdir -p "$srv/download"
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
[ "$port" != 8840 ] || die "picked port 8840"
python3 -m http.server --bind 127.0.0.1 --directory "$srv" "$port" >"$work/server.log" 2>&1 &
server_pid=$!
for _ in $(seq 50); do curl -fs "http://127.0.0.1:$port/" >/dev/null 2>&1 && break; sleep 0.1; done
export DAEDALUS_RELEASES_API="http://127.0.0.1:$port"
export DAEDALUS_DOWNLOAD_BASE="http://127.0.0.1:$port/download"

published=()
publish() { # publish <tag> <binary>
  local tag="$1" stage="$work/stage-$1"
  mkdir -p "$stage/miniapp-dist" "$srv/download/$tag"
  cp "$2" "$stage/daedalus-desktop"
  printf 'fixture ptyd %s\n' "$tag" >"$stage/ptyd"
  printf 'fixture browserd %s\n' "$tag" >"$stage/browserd"
  printf '<p>%s</p>\n' "$tag" >"$stage/miniapp-dist/index.html"
  chmod +x "$stage/daedalus-desktop" "$stage/ptyd" "$stage/browserd"
  tar -czf "$srv/download/$tag/$asset" -C "$stage" daedalus-desktop ptyd browserd miniapp-dist
  (cd "$srv/download/$tag" && sha256sum "$asset" >SHA256SUMS)
  sign "$tag" "$srv/download/$tag/SHA256SUMS" "$srv/download/$tag/SHA256SUMS.sig"
  published=("$tag" "${published[@]}")
  python3 - "$srv/releases" "http://127.0.0.1:$port" "$asset" "${published[@]}" <<'PY'
import json, sys
out, base, asset, *tags = sys.argv[1:]
json.dump([{"tag_name": t, "draft": False, "prerelease": False, "html_url": f"{base}/notes/{t}",
            "assets": [{"name": n, "browser_download_url": f"{base}/download/{t}/{n}"} for n in (asset, "SHA256SUMS", "SHA256SUMS.sig")]}
           for t in tags], open(out, "w"))
PY
}

seed() { # seed <data folder> [mode]: what an installation holds, and the fixture marker
  mkdir -p "$1/state" "$1/daedalus-secrets" "$1/workspaces/notes" "$1/daedalus/.git"
  touch "$1/.upgrade-fixture"
  printf 'DAEDALUS_PORT=18000\n' >"$1/.env"
  printf '%s\n' "${2:-native}" >"$1/mode"
  printf 'schema v1\n' >"$1/state/daedalus.sqlite"
  printf 'FIXTURE_KEY=not-a-real-key\n' >"$1/daedalus-secrets/keyproxy.env"
  printf 'a note\n' >"$1/workspaces/notes/today.md"
  ln -s today.md "$1/workspaces/notes/latest.md"
  printf 'ref: refs/heads/main\n' >"$1/daedalus/.git/HEAD"
}

# installer <name> [env...]: install.sh against installation <name>, with no terminal to ask on.
installer() {
  local name="$1"
  shift
  env DAEDALUS_DIR="$work/$name/Daedalus" "$@" setsid sh "$work/install.sh" </dev/null >"$work/install-$name.log" 2>&1
}

# A: fresh installs of v0.12.0.
publish desktop-v0.12.0 "$work/bin/v0.12.0"
for name in a b c d e; do
  installer "$name" || die "install.sh ($name): $(cat "$work/install-$name.log")"
done
seed "$work/a/Daedalus/data"
seed "$work/b/Daedalus/data"
seed "$work/c/Daedalus/data"
seed "$work/d/Daedalus/data" docker
seed "$work/e/Daedalus/data"
expect_eq "$("$work/a/Daedalus/daedalus-desktop" --version)" desktop-v0.12.0 "A: install.sh installs the newest release (v0.12.0)"
old_sum="$(sha256sum "$work/bin/v0.12.0" | cut -d' ' -f1)"

publish desktop-v0.13.0 "$work/bin/v0.13.0"

# B: the bridge works.
A="$work/a/Daedalus/daedalus-desktop"; AD="$work/a/Daedalus/data"
installer a DAEDALUS_UPGRADE_YES=1 DAEDALUS_UPGRADE_FIXTURE=ok || die "B: $(cat "$work/install-a.log")"
grep -q "predates upgrade" "$work/install-a.log" || die "B: the installer did not take the bridge"
expect_eq "$("$A" --version)" desktop-v0.13.0 "B: v0.12.0 → v0.13.0 through install.sh"
expect_eq "$(cat "$AD/state/daedalus.sqlite")" "schema migrated by desktop-v0.13.0" "B: the migrated data is kept"
expect_eq "$(stage_of "$AD")" "upgrade:committed" "B: the journal says committed"
backup="$(ls -d "$AD"/backups/*/ | head -n 1)"
python3 - "$backup" "$old_sum" <<'PY' || die "B: the backup does not match its manifest"
import hashlib, json, os, sys, tarfile, tempfile
d, old = sys.argv[1], sys.argv[2]
m = json.load(open(os.path.join(d, "manifest.json")))
def check(archive, digest, entries):
    assert hashlib.sha256(open(os.path.join(d, archive), "rb").read()).hexdigest() == digest, archive
    t = tempfile.mkdtemp()
    with tarfile.open(os.path.join(d, archive)) as tar:
        tar.extractall(t, filter="data")
    for e in entries:
        p = os.path.join(t, e["path"])
        if e["kind"] == "file":
            assert hashlib.sha256(open(p, "rb").read()).hexdigest() == e["sha256"], e["path"]
        elif e["kind"] == "symlink":
            assert os.readlink(p) == e["target"], e["path"]
    return t
data = check("data.tar.gz", m["archive_sha256"], m["entries"])
assert open(os.path.join(data, "state/daedalus.sqlite")).read() == "schema v1\n"
launcher = check("launcher.tar.gz", m["launcher_sha256"], m["launcher_entries"])
assert hashlib.sha256(open(os.path.join(launcher, "daedalus-desktop"), "rb").read()).hexdigest() == old
assert open(os.path.join(launcher, "ptyd")).read() == "fixture ptyd desktop-v0.12.0\n"
PY
ok "B: the backup holds the pre-upgrade data and the v0.12.0 launcher byte for byte, checked by Python"

# C: the bridge fails its health check.
B="$work/b/Daedalus/daedalus-desktop"; BD="$work/b/Daedalus/data"
before="$(tree "$work/b/Daedalus" | grep -v -e '\./data/backups' -e '\./data/upgrade' -e '\./\.daedalus-upgrade' -e '\./\.daedalus-update')"
if installer b DAEDALUS_UPGRADE_YES=1 DAEDALUS_UPGRADE_FIXTURE=fail; then die "C: a failed bridge exited 0"; fi
grep -q 'Data restored and checked against the backup' "$work/install-b.log" || die "C: $(cat "$work/install-b.log")"
expect_eq "$(sha256sum "$B" | cut -d' ' -f1)" "$old_sum" "C: the v0.12.0 launcher is back byte for byte"
after="$(tree "$work/b/Daedalus" | grep -v -e '\./data/backups' -e '\./data/upgrade' -e '\./\.daedalus-upgrade' -e '\./\.daedalus-update')"
expect_eq "$after" "$before" "C: every file, link and folder of the installation is as it was"
expect_eq "$(stage_of "$BD")" "upgrade:rolled-back" "C: the journal says rolled-back"
expect_eq "$("$B" --version)" desktop-v0.12.0 "C: the v0.12.0 launcher runs again"

# D, E, F: refusals that change nothing.
refuses() { # refuses <name> <what> <expected text> [env...]
  local name="$1" what="$2" text="$3"
  shift 3
  local before after
  before="$(tree "$work/$name/Daedalus")"
  if installer "$name" "$@"; then die "$what: install.sh exited 0"; fi
  grep -q "$text" "$work/install-$name.log" || die "$what: $(cat "$work/install-$name.log")"
  after="$(tree "$work/$name/Daedalus")"
  expect_eq "$after" "$before" "$what: refused, and nothing in the installation changed"
}
refuses c "D: no terminal, no yes" "Nothing was changed" DAEDALUS_UPGRADE_FIXTURE=ok
refuses d "E: Docker mode" "not available yet" DAEDALUS_UPGRADE_YES=1 DAEDALUS_UPGRADE_FIXTURE=ok
mkdir -p "$work/e/Daedalus/data/backups" && chmod 0500 "$work/e/Daedalus/data/backups"
refuses e "F: a backup that cannot be written" "nothing was changed" DAEDALUS_UPGRADE_YES=1 DAEDALUS_UPGRADE_FIXTURE=ok
chmod 0700 "$work/e/Daedalus/data/backups"

# G: v0.13.0 upgrades itself.
publish desktop-v0.14.0 "$work/bin/v0.14.0"
out="$("$A" check-update --data "$AD")"
case "$out" in *"desktop-v0.14.0 is available"*) ok "G: check-update finds desktop-v0.14.0" ;; *) die "G: $out" ;; esac
DAEDALUS_UPGRADE_FIXTURE=ok "$A" upgrade --yes --data "$AD" >"$work/g.log" 2>&1 || die "G: $(cat "$work/g.log")"
expect_eq "$("$A" --version)" desktop-v0.14.0 "G: v0.13.0 → v0.14.0 by the launcher's own upgrade"

# H: update keeps the invariant.
printf 'schema v1\n' >"$AD/state/daedalus.sqlite"
before="$(tree "$AD" | grep -v -e '\./backups' -e '\./upgrade')"
if DAEDALUS_UPGRADE_FIXTURE=fail "$A" update --data "$AD" >"$work/h1.log" 2>&1; then die "H: a failed update exited 0"; fi
after="$(tree "$AD" | grep -v -e '\./backups' -e '\./upgrade')"
expect_eq "$after" "$before" "H: a failed update puts every data file back"
expect_eq "$(stage_of "$AD")" "update:rolled-back" "H: the journal says the update was rolled back"
n_before="$(ls "$AD/backups" | wc -l)"
DAEDALUS_UPGRADE_FIXTURE=ok "$A" update --data "$AD" >"$work/h2.log" 2>&1 || die "H: $(cat "$work/h2.log")"
expect_eq "$(cat "$AD/state/daedalus.sqlite")" "schema migrated by desktop-v0.14.0" "H: an update that works keeps its migration"
expect_eq "$(stage_of "$AD")" "update:committed" "H: the journal says the update was committed"
# A check that fails says so: a chain of && without a die would skip it in silence.
if [ "$(ls "$AD/backups" | wc -l)" -ge "$n_before" ] && grep -q "updated; the verified backup is $AD/backups/" "$work/h2.log"; then ok "H: the update took a backup first"; else die "H: the update took a backup first: $(cat "$work/h2.log")"; fi
DD="$work/d/Daedalus/data"
before="$(tree "$DD")"
# The Docker installation is still on v0.12.0 (the bridge refused it), and that launcher's own update
# is out of reach of this change — it is not run here, because it would talk to Docker. The new
# launcher's refusal is checked with a v0.14.0 copy beside it.
cp "$work/bin/v0.14.0" "$work/d/Daedalus/daedalus-desktop-new"
if DAEDALUS_UPGRADE_FIXTURE=ok "$work/d/Daedalus/daedalus-desktop-new" update --data "$DD" >"$work/h4.log" 2>&1; then die "H: Docker update ran"; fi
rm "$work/d/Daedalus/daedalus-desktop-new"
grep -q "not available yet" "$work/h4.log" || die "H: $(cat "$work/h4.log")"
expect_eq "$(tree "$DD")" "$before" "H: Docker update refused with nothing changed"

# I: install.sh over a launcher that has upgrade.
before="$(tree "$work/a/Daedalus")"
if installer a; then die "I: install.sh did not stop"; fi
grep -q "handing over to its launcher's upgrade" "$work/install-a.log" || die "I: $(cat "$work/install-a.log")"
expect_eq "$(tree "$work/a/Daedalus")" "$before" "I: install.sh handed over and changed nothing by itself"

echo
echo "$pass checks passed. Work folder: $work"
