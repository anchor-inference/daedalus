# Signing launcher releases (architecture, not yet in force)

**Status.** The launcher (`signing.go`) and `install.sh` can verify a signed release; `install.ps1`
has the same check written but never run. All three are tested only with throwaway keys the tests
make. `trustedReleaseKeys` in the launcher and `release_keys` / `$ReleaseKeys` in the installers are
**empty**: no project key exists, none is in the repository, none is made up here. **An empty key
list means releases are not authenticated at all.** Authentication stays open and publishing to users is a no-go
until the operator decides the points under *Decisions* and a key is compiled in.

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
SmartScreen say; this decides what the launcher agrees to install. They are independent, and this one
costs nothing but key custody.

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

## Flow once a key exists

1. **Release workflow** (`desktop.yml`): builds and writes `SHA256SUMS` as today, into a **draft**
   release (the pinning patch already does this). Nothing signs in CI.
2. **Signing, off CI**: the key holder downloads the draft's `SHA256SUMS`, checks it against the
   workflow run, builds the message (`printf 'daedalus-release %s\n' "$tag" > msg; cat SHA256SUMS >>
   msg`), signs it (`signify -S -s release.sec -m msg -x SHA256SUMS.sig`), uploads
   `SHA256SUMS.sig` to the draft, and publishes the draft. A release is never public unsigned.
3. **Launcher**: with `trustedReleaseKeys` non-empty, `FindUpgrade` offers only releases that carry
   `SHA256SUMS.sig`, and `stage()` verifies it before it reads a checksum or unpacks a byte; a missing,
   malformed, foreign-key or wrong signature refuses the upgrade with nothing changed.
4. **Bridge**: `install.sh` / `install.ps1` put `SHA256SUMS.sig` beside `SHA256SUMS`; the bridge
   verifies it the same way. But the bridge *is* the downloaded launcher: what it verifies, it
   verifies with a key it brought itself. So the bridge and a first install are only as trustworthy
   as the installer's own check (next point).
5. **Installers**: the key is written into the script too. Both always fetch `SHA256SUMS.sig` and
   put it beside `SHA256SUMS` for the bridge. With a key, the signature is checked **before anything
   from the release is unpacked or run** — over the tag and `SHA256SUMS` — with OpenSSL 3
   (`openssl pkeyutl -verify -rawin`); a missing, foreign, wrong-tag or broken signature, or no
   OpenSSL 3 on the machine, is a refusal with nothing installed. Tested for `install.sh` on Linux
   (OpenSSL 3.0.13). Not verified: macOS, whose `/usr/bin/openssl` is LibreSSL and may not do this
   (then the installer refuses — a no-go for macOS installs until checked or a verifier ships);
   Windows, where `install.ps1` looks for OpenSSL on PATH or in Git for Windows and refuses without
   it (never run).
6. **What the installers cannot do: vouch for themselves.** The script arrives by `curl | sh` or
   `irm | iex` over TLS, unsigned; a forged copy can simply leave the check out. So a first install
   and the bridge are authenticated end to end only if the user compares the key fingerprint the
   installer prints with one obtained through a channel GitHub does not control. **Every upgrade
   after that is covered by the key compiled into the launcher.**

## Rules the code keeps

- **No silent fallback.** With a key compiled in, a missing, malformed, foreign-key or wrong
  signature refuses the upgrade; there is no "unsigned, continue anyway" path and no flag for one.
- **No trust from the release itself.** The keys are only those compiled into the launcher (and
  embedded in the installers). A key shipped *in* a release, or named by it, is never trusted for
  that release.
- **An empty key list is no authentication.** It is today's state: it does not authenticate releases and it is
  not a basis for publishing to users.

## Trust for installations that already exist (v0.12.0 and before)

They carry no key and cannot check anything, and their first step to the new mechanism is the
installer's bridge (`UPDATES.md`). That one step can only be as trustworthy as the installer:

1. **TLS + GitHub for the one bridge step** (simplest): the installer fetched from `main` over TLS
   carries the public key and checks `SHA256SUMS.sig` where a verifier exists; from then on the
   launcher checks every upgrade. Publish the key's fingerprint where GitHub cannot change it
   alone (the project site, a signed announcement) so a careful user can compare.
2. **Manual check once**: the release notes ask existing users to verify `SHA256SUMS.sig` by hand
   before running the installer. The signed message is the tag line followed by `SHA256SUMS`, so it
   is rebuilt first (`TAG` is the release, e.g. `desktop-v0.13.0`; `daedalus.pub` is the public key
   obtained outside GitHub):

   ```sh
   printf 'daedalus-release %s\n' "$TAG" > message
   cat SHA256SUMS >> message
   signify -V -p daedalus.pub -x SHA256SUMS.sig -m message
   ```

   Without signify, OpenSSL 3 does the same with the raw key and signature — what `install.sh`
   runs (`verify_release`):

   ```sh
   tail -n 1 SHA256SUMS.sig | openssl base64 -d -A | tail -c 64 > sig.bin
   { printf '\060\052\060\005\006\003\053\145\160\003\041\000'
     tail -n 1 daedalus.pub | openssl base64 -d -A | tail -c 32; } > key.der
   openssl pkeyutl -verify -pubin -keyform DER -inkey key.der -rawin -in message -sigfile sig.bin
   ```

   Both print a success line only for a signature over exactly this tag and these checksums. Then
   check the downloaded archive against `SHA256SUMS` (`sha256sum -c --ignore-missing SHA256SUMS`).
3. **Fresh install only**: existing installations are asked to reinstall from a verified download.

## Rotation and loss

- `trustedReleaseKeys` is a list: a new key ships in a release signed by the old one, and the old one
  is dropped a release later.
- A lost key means shipping a new key the old way cannot vouch for: every installation takes that
  release by hand, through the installer.
- A leaked key: remove it from the list in a release signed by the other key, and say so publicly.
  Having two keys from the start (one offline as a spare) is what makes this possible.

## Decisions for the operator

1. **Custody**, shortest version:
   - **offline key on the operator's machine** (recommended): signed by hand at publish time; a
     compromised GitHub cannot sign;
   - **hardware token** (YubiKey etc. with Ed25519): the same, the key cannot be copied off;
   - **CI secret**: automatic, but whoever controls the workflow controls the key — little over
     GitHub's own trust.
   Rotation in every case: two keys from the start (active + offline spare), a new key ships in a
   release signed by the old, the old one leaves a release later.
2. **Who signs and when**: the manual publish step above, or not.
3. **Two keys** (active and spare) and where the spare lives.
4. **Installers**: accept "first install rests on TLS + GitHub where no verifier exists", or ship a
   verifier.
5. **Existing v0.12.0 installations**: one of the three bootstrap options above.

Nothing here creates, reads or stores a key; that waits for these decisions.
