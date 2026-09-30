# Signing launcher releases

**Status: in force.** Every launcher release is signed with the project's release key, and the
launcher (`signing.go`, `trustedReleaseKeys`), `install.sh` (`release_keys`) and `install.ps1`
(`$ReleaseKeys`) refuse a release that is not — before anything from it is unpacked or run. The key:

```
minisign key id   A18524FA935353DF   (the bytes df535393fa2485a1 in the record)
public key line   RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2
fingerprint       f75fa5a293fdd55b36c794f4787f7af8646e4dac95e19d74e6288e55c357c285
```

The fingerprint is the SHA-256 of the key line, which is what both installers print before they
install anything. It is published outside the installers, where a forged installer cannot change it:
in the repository's README and `desktop/README.md`, and in every release's notes. The private half
is kept by the operator on their own machine, never on a server and never in CI, and releases are
signed there (*Release checklist* below). The tests never use it: they make throwaway keys, and a
fixture build of the launcher puts its test's key in place of the project's with `-ldflags -X
main.linkedFixtureReleaseKey=…`.

## What it protects against, and what not

`SHA256SUMS` proves that a download is the file the release lists. Anyone who can publish a release —
a stolen GitHub token, a compromised workflow, a compromised account — can publish matching sums, and
every installation that upgrades runs that code. A signature over the release's tag and `SHA256SUMS`
(format below) by a key whose public
half is compiled into the launcher moves the trust from "whoever can publish on GitHub" to "whoever
holds the private key". That is only worth something if the private key is **not** reachable from the
same place: a key stored as a GitHub Actions secret protects against a tampered download or mirror,
not against a compromised account or workflow.

It is not OS code signing (Apple Developer ID, Authenticode). Those decide what Gatekeeper and
SmartScreen say; this decides what the launcher agrees to install. They are independent, and OS code
signing is out of scope for the preview.

**What it does not cover: the checkouts.** The code the stack runs — the two checkouts — is not in
the release. `update`, and the last step of every upgrade, fetch the tip of `main` from GitHub's
archive service (`repos.go`) over TLS, and nothing signs that: it is exactly as trustworthy as
GitHub and TLS. The release signature covers the launcher, `ptyd`, `browserd` and the app's build,
and nothing the launcher downloads afterwards.

## Format

OpenBSD signify's, Ed25519 without prehashing — verified by the Go standard library, produced by
`signify -S`, by `minisign -S -l` (legacy mode), or by a few lines of any language:

```
SHA256SUMS.sig   untrusted comment: …
                 base64("Ed" | key id: 8 bytes | Ed25519 signature: 64 bytes)
signed message   "daedalus-release <tag>\n" followed by SHA256SUMS exactly as published
public key       untrusted comment: …
                 base64("Ed" | key id: 8 bytes | Ed25519 public key: 32 bytes)
```

## Release checklist

With the key compiled in, **an unsigned release is refused by every launcher and by both
installers**: a release that is published before it is signed is one nobody can install, and every
launcher that looked at it meanwhile has said so. So the first release carrying the key — the
preview — is signed before it is published, like every release after it.

1. **Tag.** Pushing `desktop-vX.Y.Z` runs the workflow (`desktop.yml`): it builds the archives and
   `SHA256SUMS` into a **draft** release, and signs nothing. It refuses to touch a release that is
   already published: new archives under a signature over the old `SHA256SUMS` would be refused
   everywhere, and replacing both is a new release.
2. **Sign, on the operator's machine** — with `gh` signed in and `minisign` installed:

   ```sh
   desktop/sign-release.sh desktop-vX.Y.Z                    # minisign's default key location
   desktop/sign-release.sh desktop-vX.Y.Z ~/keys/daedalus.key
   ```

   It checks that the release is still a draft, downloads its `SHA256SUMS`, checks that every file
   it names is attached, and prints it — compare it with the workflow run's log. It then builds the
   message (`daedalus-release <tag>` and a newline, then `SHA256SUMS` exactly as published), signs it
   with `minisign -S -l`, verifies the signature against the key line in `install.sh` — the one the
   installers and the launcher carry — and uploads `SHA256SUMS.sig` to the draft. minisign asks for
   the key's password; the secret key is read from where minisign keeps it or from the path given,
   and never leaves the machine.
3. **Publish**: `gh release edit desktop-vX.Y.Z --draft=false`, or `--publish` in step 2.

**After any re-run of the workflow over the draft, sign again.** A re-run rebuilds the archives —
builds are not byte-for-byte reproducible — and replaces them and `SHA256SUMS`; a signature over the
old checksums would make every launcher refuse the release, so the workflow deletes
`SHA256SUMS.sig` when it replaces files and says so in its log. Step 2 then has to be done again.

The macOS and Windows tests (`window` in the workflow) do not hold back the build. While they are
red, publish the Linux archives alone: before step 2, delete the macOS and Windows archives from the
draft and replace its `SHA256SUMS` with only the lines of the files left
(`grep linux SHA256SUMS > SHA256SUMS.linux && mv SHA256SUMS.linux SHA256SUMS`, then
`gh release upload desktop-vX.Y.Z SHA256SUMS --clobber`): step 2 signs what `SHA256SUMS` names, and
refuses a list that names a file the draft does not have.

`-l` matters: minisign's default signature is over a BLAKE2b hash of the message, which neither the
launcher nor OpenSSL checks; the legacy signature is plain Ed25519 over the message itself.

## What checks the signature

- **Launcher.** `FindUpgrade` offers only releases that carry `SHA256SUMS.sig` (a newer one without
  it is named as unsigned, not hidden), and `stage()` verifies it before it reads a checksum or
  unpacks a byte; a missing, malformed, foreign-key or wrong signature refuses the upgrade with
  nothing changed.
- **Bridge.** `install.sh` / `install.ps1` put `SHA256SUMS.sig` beside `SHA256SUMS`, and the bridge
  verifies it the same way. But the bridge *is* the downloaded launcher: what it verifies, it
  verifies with a key it brought itself, so the bridge is only as trustworthy as the installer's own
  check.
- **Installers.** Both fetch `SHA256SUMS.sig`, print the key's fingerprint, and check the signature
  over the tag and `SHA256SUMS` **before anything from the release is unpacked or run**, with OpenSSL
  3 (`openssl pkeyutl -verify -rawin`); a missing, foreign, wrong-tag or broken signature, or no
  OpenSSL 3 on the machine, is a refusal with nothing installed. They read the first line of the
  signature file after its untrusted comment, as the launcher does, so minisign's trusted comment
  and its second signature after it are ignored. `install.sh` is tested on Linux (OpenSSL 3.0);
  `install.ps1` is run by the tests under PowerShell 7 on Linux and, in CI, by Windows PowerShell on
  Windows, where it takes the first OpenSSL 3 it finds on PATH or in Git for Windows (an older one on
  PATH has no `-rawin`). Not verified: macOS, whose `/usr/bin/openssl` is LibreSSL and may not do
  this — then the installer refuses, and a Mac needs OpenSSL 3 (Homebrew's) to install by script.
- **What the installers cannot do: vouch for themselves.** The script arrives by `curl | sh` or
  `irm | iex` over TLS, unsigned; a forged copy can simply leave the check out. So a first install
  and the bridge are authenticated end to end only if the user compares the fingerprint the
  installer prints with the one published in the README and the release notes. After that, the
  launcher checks every upgrade of itself with the key compiled into it — and, as above, never the
  checkouts.

## Rules the code keeps

- **No silent fallback.** With a key compiled in, a missing, malformed, foreign-key or wrong
  signature refuses the upgrade; there is no "unsigned, continue anyway" path and no flag for one.
- **No trust from the release itself.** The keys are only those compiled into the launcher (and
  embedded in the installers). A key shipped *in* a release, or named by it, is never trusted for
  that release.
- **A build always carries a key.** An empty list would authenticate nothing; the only way to change
  the list is to build the launcher, and a fixture build replaces it with its test's key rather than
  adding one.

## Trust for installations that already exist (v0.12.0 and before)

They carry no key and cannot check anything, and their first step to the new mechanism is the
installer's bridge (`UPDATES.md`). That one step is as trustworthy as the installer: fetched from
`main` over TLS, it carries the key and checks `SHA256SUMS.sig`, and from then on the launcher checks
every upgrade of itself. A careful user checks the installer's printed fingerprint against the README
and the release notes, or verifies the release by hand before running it.

The signed message is the tag line followed by `SHA256SUMS`, so it is rebuilt first (`TAG` is the
release, e.g. `desktop-v0.13.0`):

```sh
printf 'daedalus-release %s\n' "$TAG" > message
cat SHA256SUMS >> message
minisign -V -P RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2 -m message -x SHA256SUMS.sig
```

Without minisign, OpenSSL 3 does the same with the raw key and signature — what `install.sh`
runs (`verify_release`); the signature is the second line of the file:

```sh
sed -n 2p SHA256SUMS.sig | openssl base64 -d -A | tail -c 64 > sig.bin
{ printf '\060\052\060\005\006\003\053\145\160\003\041\000'
  printf '%s' RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2 | openssl base64 -d -A | tail -c 32; } > key.der
openssl pkeyutl -verify -pubin -keyform DER -inkey key.der -rawin -in message -sigfile sig.bin
```

Both print a success line only for a signature over exactly this tag and these checksums. Then
check the downloaded archive against `SHA256SUMS` (`sha256sum -c --ignore-missing SHA256SUMS`).

## Rotation and loss

- `trustedReleaseKeys` is a list: a new key ships in a release signed by the old one, and the old one
  is dropped a release later.
- There is one key today and no spare. A lost key means shipping a new key the old way cannot vouch
  for: every installation takes that release by hand, through the installer, after checking the new
  fingerprint out of band. A spare kept offline from the start is what would avoid that.
- A leaked key: remove it from the list in a release signed by another key, and say so publicly —
  which, again, needs a second key to exist.
