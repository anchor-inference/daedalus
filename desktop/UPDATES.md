# Updating a desktop installation

How a desktop installation learns about a new launcher release and moves to it, and how it moves
the code its stack runs. One spec, two thin installers (`install.sh` for macOS and Linux,
`install.ps1` for Windows) and one mechanism that lives in the launcher itself and is the same code
on all three systems.

**The invariant.** Nothing that changes what the stack runs — a new launcher, new checkouts, the
app's migrations on the start after them — happens before the data folder is protected: on Linux
with ext4 it is switched to a fenced copy while the kernel keeps every writer out, and the folder
as it was is kept whole ([the writer fence](#the-writer-fence-linux-ext4)); everywhere else it is
backed up and the backup read back against its manifest. A new launcher's own files are kept
aside as well. A failure after that point puts the data and the launcher back the same way. Where
that cannot be promised, the command refuses and changes nothing. That holds for `upgrade`, for
the installers' bridge from launchers that predate `upgrade`, and for `update`. Docker mode cannot
promise it yet and is refused by all three.

## Two versions, not one

| What | Version | Moved by |
|---|---|---|
| the launcher (`daedalus-desktop`, `ptyd`, `browserd`, the Mini App build) | the `desktop-vX.Y.Z` release it came from | `install.sh` / `install.ps1` on a fresh folder; `daedalus-desktop upgrade` (or the installers' bridge) afterwards |
| the code the stack runs (the two checkouts, and in Docker mode the images) | the tip of `main` and `:latest` when fetched (`repos.go`, `docker.go`) | the first start; `daedalus-desktop update`; the last step of an upgrade |

A release does **not** pin what the stack runs: every installation moves to the newest `main`
whenever `update` runs, and an upgrade ends by doing the same. Pinning a release to its own commit
and image would be a separate change; nothing below depends on it, and if it lands an upgrade simply
moves the checkouts to what the new launcher pins. The release signature (SIGNING.md) covers the
launcher's own files and never these checkouts, which are fetched over TLS and signed by nobody.

The app's database migrations run on the start that follows a change of the checkouts. They are
treated as irreversible: the only way back is the data as it was before.

## Finding out

- A running launcher checks the repository's releases 30 s after it starts and every 12 h after
  that, anonymously, at `https://api.github.com/repos/<owner>/<repo>/releases`. Only a plain
  `desktop-vX.Y.Z` tag counts, newer than the running one, not a draft, not a prerelease, and with
  this machine's archive **and** `SHA256SUMS` attached.
- What the operator sees — every one of them with the **exact command for this machine**, the
  launcher's own path and data folder quoted for its shell (`'…/daedalus-desktop' upgrade --data
  '…/data'`, on Windows `& '…\daedalus-desktop.exe' upgrade --data '…\data'`):
  - an **entry in the app's own notification centre**, once per release
    (`daedalus/extensions/launcher_updates.py`): the app reads the launcher's `/api/status` through
    the launcher bridge every 30 minutes and posts it; which release was announced is kept in the
    state folder, so a restart does not repeat it. Tested at the service level (the entry is
    posted with its text); the notification centre's own rendering is the app's existing UI and was
    not re-rendered for this;
  - a **desktop notification**, once per release — recorded as shown only once it was shown, so a
    machine whose notifications failed is asked again at the next check;
  - a **card on the launcher's status page**, rendered and read back in a headless Chromium
    (`DAEDALUS_BROWSER_TEST=1`);
  - a line in the launcher's log and `upgrade` (with `command`) in `/api/status`.
- There is still no **button**: the upgrade replaces the launcher and restarts the app, so it runs
  in a terminal with the launcher closed. From Finder or the Start menu with no terminal there is no
  path yet.
- It is never installed by the check. `daedalus-desktop check-update` does the same check once,
  in a terminal. A dev build never checks; `DAEDALUS_UPDATE_CHECK=off` turns it off.
- `DAEDALUS_RELEASES_API` points it at another repository's API (or a fixture). Addresses must be
  `https`, or `http` to loopback.

## One at a time: the installation lock

Everything that runs the stack or changes the installation holds `<data>/upgrade/.lock` — an
operating-system lock (flock; on Windows a file opened with no sharing), which goes away with the
process that held it, however it ended:

- the **launcher**, from its start until it exits;
- an **upgrade**, from its first step to its commit or rollback. It also takes a second lock,
  `<data>/upgrade/.finish-lock`, at the same first step, and hands it to the new launcher's `--finish`
  as an inherited descriptor — the same open lock, held by both processes (on Windows an inherited
  handle to the file opened without sharing). There is no moment of hand-over when nobody holds it,
  and if the old launcher dies (kill -9, OOM) `--finish` still holds it: a `--rollback`, `start`,
  `stop` or `update` started then is refused until `--finish` has committed or rolled back. Whoever
  takes the installation lock also requires the finish lock to be free. `--finish` refuses to run
  without the handed-over lock, and before it writes `committed` it checks that the journal is still
  its own (its token, stage `finishing`). Both launchers ignore SIGHUP during an upgrade, so closing
  the terminal window does not stop it half-way;
- an **update** from a terminal, and a **rollback**.

The lock is never waited for. While an upgrade, update or rollback holds it, every other command
(`start`, `stop`, `update`, `setup`, a second `upgrade`, `--rollback`) refuses at once with who holds
it; while a launcher holds it, `update` from a terminal refuses and points at the launcher's own
Update button. The lock file stays after a release (empty): deleting a locked file would let two
processes each hold "the" lock.

## Only its own stack: health and orphans

- **Health.** The launcher gives each start of the supervisor a random `DAEDALUS_BOOT_ID`; the bot
  inherits it and the app echoes it as `X-Daedalus-Boot` on its answers to `/app` and `/app/` — the
  path the health check asks, and no other. The start that decides an
  upgrade's or an update's commit accepts only that answer. An app older than the header (a fork, an
  old checkout) is accepted without it only by an **ordinary** start, and only if the port was free
  when that start began and the supervisor it started is alive — nothing is committed on that. A
  health check with nothing expected on the port refuses outright.
- **Port.** A native start refuses when something it did not start already answers on the app's
  port.
- **A rollback never restores under a running stack.** Its stop is the full one: this launcher's
  own processes, then every process the records confirm (the stack a `--finish` that died had
  brought up included), then the app's port must be quiet. If something still answers there that
  no record confirms, the rollback stops with `rollback-failed` and the data untouched, and says to
  stop the Daedalus processes by hand and run `--rollback` again. `--finish` keeps the finish lock
  to itself: the descriptor it inherited is close-on-exec, so nothing it starts — git, uv, the
  stack — holds the lock after it is gone.
- **Orphans.** Each native child is recorded under `pids/` in the launcher's local state folder with its pid, its program and the
  system's start time for that pid. A launcher that died leaves those records; the next start,
  `stop`, upgrade or update stops every child whose pid, program *and* start time still match, whole
  process group, and waits until they are gone. A pid now used by another process is left alone and
  its record dropped; one that cannot be confirmed is left alone and the command refuses. Not seen:
  a bot whose supervisor died first (it is in a session of its own); the port check catches it.

## Upgrading: `daedalus-desktop upgrade`

With the launcher closed, `daedalus-desktop upgrade [--data DIR] [--yes]`:

1. **Refuses** — changing nothing — in Docker mode (before anything is written), when the lock is
   held, when a launcher is running on the installation, or when a previous upgrade or update is
   unresolved.
2. **Finds** the release and says what will happen. **Waits for `yes`** on the terminal; `--yes`
   answers for a script. No terminal and no `--yes` is a refusal.
3. **Prepares**, changing nothing the installation runs:
   - the archive is checked against `SHA256SUMS` and unpacked into
     `<install>/.daedalus-upgrade/<time>/new/`, refusing any entry that is absolute, climbs out with
     `..`, would be written through a symlink, or is a symlink (except inside the macOS bundle,
     pointing inside it), and refusing an archive that would replace `data` or has no launcher;
   - the stack is stopped, orphans included, and the app's port must be quiet;
   - on Linux, any ServiceStart process still recorded as running is identified by its PID,
     start time, session and working directory, then stopped. A scan of accessible processes'
     cwd and open files refuses the backup if a data writer remains. The same gate runs before
     a rollback restores data. Processes in another protected cgroup cannot always be inspected;
     use a disposable machine for preview and check for foreign data writers first;
   - the next version's checkouts are fetched and their Python environment is built beside the one
     in use (only the third-party packages; the project itself is linked in at the start that
     follows), so a slow download happens before anything stops and a rollback finds the old
     environment untouched;
   - a data folder that still holds `runtime/` (a v0.12 installation) has it moved out, once the
     stack is stopped ([the runtime](#the-runtime-lives-outside-the-data-folder)); the move refuses
     while anything answers on the app's port or any process has its working directory or an open
     file inside the old folder;
   - on Linux with ext4 the data folder is **switched to a fenced copy** and the folder as it was
     is kept whole under `.daedalus-update/<data folder's name>/retained/pre-<op>/` beside it
     ([the writer fence](#the-writer-fence-linux-ext4)); the copy is refused up front when the disk
     has no room for it and a margin, and a refusal of the fence — a writer, a file of another user
     — is final and nothing was changed;
   - elsewhere (Windows, macOS, other filesystems, or `DAEDALUS_DATA_FENCE=off`) the disk must have
     room for about twice the data, plus a margin, and the **backup** is written into `<data>/backups/<time>-<version>/`: `data.tar.gz` (the data
     folder without `runtime/`, `backups/`, `upgrade/` and `launcher.json`) and `launcher.tar.gz`
     (the launcher's files the release replaces), with `manifest.json` naming every entry's kind,
     mode, size and SHA-256. It is **read back** against the manifest. The folder is 0700 and the
     files 0600: it holds the keys.

   If any of this fails, what it made — the staging folder, the journal, a backup that did not
   verify — is removed and the installation is as it was.
4. **Swaps** each of the release's top-level items into the installation folder by rename, the old
   one into `old/`. Rename is what Windows allows for a running executable. The journal names each
   item before it moves; a rollback treats an item found in `old/` as moved, whatever the journal
   managed to record.
5. **Hands over** to the new launcher: `upgrade --finish` checks it is the version expected,
   updates the checkouts, starts the stack and waits for the app to answer.
6. **Commits** (older backups pruned to the newest 3; with the fence, older kept copies removed,
   each under a fresh fence) — or, on any failure after the data was protected,
   **rolls back** (a Ctrl+C included: the terminal's SIGINT reaches both launchers; the old one keeps
   waiting instead of killing the new one, and the rollback runs to its end whatever arrives while it
   does): stops the stack and puts the data from before back — with the fence, by the same
   exchange under the same fence, keeping what the new version left as
   `.daedalus-update/<data folder's name>/retained/failed-<op>/` for the operator; with a backup,
   it moves the current data aside into a folder of its own,
   `<data>/upgrade/replaced-<time>-<random>/`, verifies and restores the backup, syncs every
   restored file and folder to the disk and checks the restored tree against the manifest, and
   puts the previous launcher's files back — from `old/`, or from `launcher.tar.gz` when those are
   gone. An upgrade that moved a v0.12 runtime out of the data folder puts it back for the v0.12
   launcher it returns to, logins and terminal state as that launcher left them. A new launcher that does not run at all is rolled back
   by the old one. When the new version fails while the launcher that began the upgrade is alive,
   `--finish` leaves the rollback to it (it holds the installation lock a fenced restore needs);
   when that launcher is gone, `--finish` takes the lock and rolls back itself.

Every step is written to `.daedalus-update/<data folder's name>/upgrade.json` beside the data
folder (the file and its folder fsynced) before the next begins. It is never inside the data
folder: the fence exchanges the whole folder, and a restore would make the journal from before the
update live again — one that says nothing is unfinished. While it says an upgrade or update is
unresolved, or a switch of the data folder did not finish, every command except `upgrade`,
`update status`, `update resolve`, `status`, `logs` and `check-update` refuses and names the
command that settles it. `upgrade --rollback` that finishes exits 0; a rollback cut off after its
restore recognises the data from before at the data folder by its inode and does not restore it
twice. A **rollback that fails** stops where it is, records `rollback-failed`, and prints where the
data from before is, the previous launcher's path and what to do by hand; it never goes on to
start anything.

## From a launcher that predates `upgrade` (v0.12.0 and before): the bridge

A v0.12.0 launcher knows no `upgrade`, no journal and no backup, and it cannot be taught. So the
first step to the new mechanism is taken by the **new** launcher, run by the installer:

1. `install.sh` / `install.ps1` finds an installation with `data/` whose launcher has no `upgrade`.
   It downloads the new release, checks it against `SHA256SUMS`, unpacks it into a temporary
   folder — and replaces **nothing** itself.
2. It runs the new launcher from there: `daedalus-desktop upgrade --bridge --root <install>
   --data <install>/data --archive <download> --sums <SHA256SUMS>`, asking on `/dev/tty` (or with
   `--yes` when `DAEDALUS_UPGRADE_YES=1`). No terminal and no yes: the script stops, nothing changed.
3. The bridge is `upgrade` from step 1, with three extra checks before it prepares anything: the
   installed launcher answers `--version` with a release older than the bridge; the archive is this
   machine's; and the bridge's own executable is byte-for-byte the launcher inside the archive it is
   about to install. It then backs up the data **and the v0.12.0 launcher's files**, verifies both,
   swaps, hands over to `--finish`, and commits or rolls back exactly as above.

Where the bridge cannot go ahead — Docker mode, a running launcher, a version it does not expect,
a backup it cannot write or verify — it refuses and the installation is left exactly as it was.
An interrupted bridge is safe with either launcher in place: the data changes only in `--finish`,
which the new launcher runs, and while the journal says it is unresolved the new launcher refuses
to start. If it is cut off mid-swap with the v0.12.0 launcher back in place (which ignores the
journal), the data has not been touched yet.

A folder with `data/` and no launcher is refused. A launcher with no `data/` beside it has never
run; its files are replaced (moved aside, not deleted) as on a fresh install.

## Updating the checkouts: `daedalus-desktop update`

`update` (the command and the Update button on the launcher's page) moves the checkouts to what is
published and restarts. It keeps the same invariant: the next version's checkouts are fetched and
its environment prepared, the stack is stopped and the data folder checked for writers exactly as
an upgrade does, the data folder is protected (fenced switch or verified backup), the journal
records an update in progress, and only then are the checkouts moved and the stack started. The page's button runs the switch inside the launcher that holds the
installation lock; the launcher hands its lock to the switch and takes the new live tree's lock
back. A start that fails puts the data from before back; a crash
half-way is found on the next start and undone with `upgrade --rollback`. The command or the button
is the yes; there is no second question. The launcher's files are not touched.

The other path that restarts the stack onto new code — **Apply**, onto the agent's own committed
change — is unchanged: the supervisor checks the commit on a detached copy first and keeps the
running code when the checks fail. It takes no data backup.

## Docker mode: refused, on purpose

In Docker mode the database lives in volumes, and the next start pulls `:latest` again whatever a
rollback puts back. The code that archives and restores the project's volumes through a throwaway
container, and points `:latest`/`:browser` back at the previous image ids, is in
`docker_backup.go` and unit-tested with a fake `docker`. It has **not** been run against real
volumes. So `upgrade`, the bridge and `update` all refuse Docker mode with nothing changed, and
there is no flag to go around that.

The manual path for a Docker installation, until that changes, is a backup the operator takes and
checks, and then the move done by hand:

```sh
cd <install>                     # the folder with data/ in it
./daedalus-desktop stop
mkdir -p data/backups/manual && chmod 700 data/backups/manual
for v in $(docker volume ls -q --filter label=com.docker.compose.project=daedalus); do
  docker run --rm --network none -v "$v":/from:ro -v "$PWD/data/backups/manual":/to \
    --entrypoint sh ghcr.io/anchor-inference/daedalus:latest -c 'tar -czf "/to/$1.tar.gz" -C /from .' sh "$v"
done
tar -czf data/backups/manual/data.tar.gz --exclude=./backups --exclude=./runtime -C data .
docker image inspect --format '{{.Id}}' ghcr.io/anchor-inference/daedalus:latest ghcr.io/anchor-inference/daedalus:browser \
  > data/backups/manual/images.txt
for f in data/backups/manual/*.tar.gz; do tar -tzf "$f" >/dev/null || echo "BROKEN: $f"; done
```

Then the images: `docker compose -f data/daedalus/deploy/compose.yaml -f data/compose.desktop.yaml
--env-file data/.env --project-name daedalus pull` (add `--profile telegram` when Telegram is set
up) and `./daedalus-desktop start`. Moving the **checkouts** by hand is what `update` did (fetch the
tarball of `main`, replace the tracked files, commit); there is no shorter manual equivalent, so
until Docker mode is covered a Docker installation either stays on its checkouts or its operator
runs a v0.12.0 launcher's `update` — which has no backup of its own, which is why the one above
comes first. To put it all back: stop, restore each volume from its archive with the same kind of
container, `docker tag` the ids in `images.txt` back to their references, restore `data/` from
`data.tar.gz`, and start.

Lifting the refusal needs this proven on disposable volumes and images, and a start that does not
pull over a rolled-back image (pinned image tags do that).

## Installers

`install.sh` (macOS, Linux) and `install.ps1` (Windows) do the same thing:

- pick the release the launcher would: the highest plain `desktop-vX.Y.Z` that is neither a draft
  nor a prerelease (`install.sh` reads the listing without a JSON parser, whatever the order of its
  keys; tested with GitHub's order, sorted keys, and three awks);
- download the archive and `SHA256SUMS` and compare;
- **fresh folder**: unpack into `./Daedalus` (`DAEDALUS_DIR`);
- **installation with data, launcher has `upgrade`**: hand over to it and change nothing;
- **installation with data, launcher predates `upgrade`**: the bridge, above;
- they never replace a file of an installation with data themselves, and never kill a running
  launcher (they refuse instead).

`install.ps1` never calls `exit` in the user's session: under `irm … | iex` that would close the
window with the message in it. Failures are thrown, caught once, printed and left in
`$LASTEXITCODE`; the launcher runs through `Start-Process -Wait` so its question and output go
straight to the console. Not yet run anywhere (no PowerShell here).

The one-line bootstrap is transparent, not self-updating: the script is fetched from `main` each
time and does nothing a reader cannot see.

## The writer fence (Linux, ext4)

A backup taken from a folder is a copy of what was in it when it was read; a process that writes
through a shared mapping a moment later is in neither the backup nor the check. The fence closes
that gap with the kernel's own means, without privileges:

- a **read lease** on every file of the data folder: the kernel refuses it while anyone holds the
  file writable (a descriptor or a shared mapping, even one whose descriptor is closed), and a new
  writer blocks in `open()` until the switch lets go — it answers at once, so the writer waits
  milliseconds; the lease belongs to the inode, so a second name elsewhere changes nothing;
- **inotify** on every directory, watched before it is listed, for what leases do not cover;
- every **sweep** also compares each file's size, link count, metadata, inode flags and extended
  attributes, which catches a truncation, a chmod, a chattr or a setxattr through any name.

The data folder is copied through the held descriptors, the copy fenced and compared, and the two
exchanged by one `renameat2(RENAME_EXCHANGE)`; the processes of this user are scanned (every
thread) for a foothold in the old tree, and a last sweep precedes the commit. Anything found before
the exchange refuses (`REFUSED`, exit 3); after it, the exchange is undone (`ROLLED_BACK`, 4). A
state the fence cannot trust — root, a container, CAP_LEASE, file leases off, another filesystem,
a file of another user, a special file, inode flags, a mount inside the folder, a folder that still
holds `runtime/` — refuses before anything changes (`FAIL_CLOSED`, 5). A third party who exchanges
or renames the trees stops the switch without anything being filed as ours. Every attempt writes a
report to `.daedalus-update/<data folder's name>/reports/` (fsynced) before it returns. Each data
folder has a control folder of its own under `.daedalus-update/`: two data folders in one parent
never see each other's copies or switches.

**A committed switch survives a power cut.** The copy is written to the disk (`syncfs`) before the
exchange, and the exchange (both folders it changed) before anything is decided on it; a kept tree
is filed under a record written before the exchange, and the rename that files it is synced too. A
sync that fails before the exchange refuses with nothing switched; after it, the exchange is
undone. Without the first of these, a power cut in the half minute after a switch — before the
kernel writes the copy's pages back on its own — left every file of the live data empty under a
journal that said committed, with the one whole copy filed for removal.

**Space** is checked before the copy: every file written out whole plus a margin of 512 MiB or a
tenth of the copy, whichever is more; too little refuses with nothing written. A copy the disk
filled up half-way is removed at once, before anything else, without a record having to fit first.

The tree from before is kept under `.daedalus-update/<data folder's name>/retained/`, beside the
data folder, and is removed only under a fresh fence and after a full comparison with the manifest
recorded when it was kept: any difference keeps it (`RETAINED`); a doubt after unlinks have begun
is reported as `LOST_POSSIBLE`. Age is a reason to try, never a reason to delete. A removal that
was cut off leaves the rest of the tree in the trash, with its record saying so; the next one goes
on from there, and what is left must still match the record. After an update commits, the copy from
before the update before it goes, and of the copies failed updates left (`failed-*`) only the
newest stays — the older ones were superseded by the data carried forward since; a copy a late
write may have reached never goes on its own. The launcher's page shows each kept copy with its
size and removes one on request, under the same fence, except the copy an unfinished update still
needs.

- `daedalus-desktop update status [-v]` lists every kept copy with its reason, anything in the
  control folder without a record, in the trash or left beside the data folder, a possible late
  write or loss, a switch or an update that did not finish with the command that settles it, and
  exits non-zero when something needs the operator. The processes the last switch could not
  inspect are counted; `-v` lists them. The launcher's page shows the same in a card of its own.
- `daedalus-desktop update resolve [--apply]` settles a switch that stopped half way (a crash, a
  power cut): it says which tree is at data and where the other one is, and with `--apply` files the
  other one for the operator. Whatever is at data stays live and untouched; nothing is deleted.
- `DAEDALUS_DATA_FENCE=auto` (the default) uses the fence wherever it is available; `off` takes the
  verified backup on Linux too. A fence that is available and refuses is final — the backup is
  never the fallback for a refusal; a refusal that will not go away by itself (a special file, a
  file of another user, more files than the fence can hold) says so and names `off`. A data folder
  that is a symlink or a mount of its own is one the switch could only refuse, so it takes the
  backup from the start.

It needs, per file of the data folder, two open descriptors for the length of a switch (the
launcher raises its own limit to the hard one and refuses when that is not enough) and one inotify
watch per directory. A switch of an installation with about 8 000 files and 520 MB took 7–11 s on
the test machine.

## The runtime lives outside the data folder

The runtime (uv, Python, the environments, uv's cache, the extras) is a cache, and the launcher's
local state (logs, child records, the daemons' endpoints and tokens, terminal logs, the browser
profiles, the supervisor's socket) belongs to this machine: neither is data, and neither is copied
or fenced by an update.

| | runtime | local state |
|---|---|---|
| Linux | `$XDG_CACHE_HOME/daedalus/<key>` (`~/.cache/...`) | `$XDG_STATE_HOME/daedalus/<key>` (`~/.local/state/...`) |
| macOS | `~/Library/Application Support/Daedalus/Runtime/<key>` | `…/Daedalus/State/<key>` |
| Windows | `%LOCALAPPDATA%\Daedalus\Runtime\<key>` | `…\Daedalus\State\<key>` |

`<key>` is the data folder's name and a digest of its path, so two installations never share one.
macOS does not use `~/Library/Caches`, which the system may empty under a running agent. A data
folder from before the move still has `runtime/`: the launcher moves it out once, under its lock —
the logs, the terminal state and the browser profiles are copied and checked, then the old folder
is set aside next to the new runtime (or removed when it is on another filesystem); the interpreter
and the environments are rebuilt, because a moved venv keeps its old paths. The move can be cut off
anywhere and run again: each tree is copied under a `.partial` name and renamed into place only
when whole, and where the new place already holds state, the one changed last stays live and the
other is kept beside it. A runtime set aside is removed once an update or upgrade has committed.
Environments are kept one per dependency lock under `envs/`, so a rollback finds the old one as it
was.

## What the checks do and do not prove

- Signing: [SIGNING.md](SIGNING.md). The launcher, `install.sh` and `install.ps1` carry the
  project's release key and verify an Ed25519 signature over `daedalus-release <tag>\n` +
  `SHA256SUMS` before anything from a release is unpacked or run; a release without one is refused,
  and a newer release that is not signed is named as such, not passed over as "nothing newer". The
  installers themselves arrive unsigned over TLS, so a first install is authenticated only by the
  user comparing the fingerprint they print with the one in the README and the release notes.
- `SHA256SUMS` proves the download is the file the release lists; the signature over it proves who
  published the list. Neither covers the checkouts, which `update` and the end of every upgrade fetch
  from `main` over TLS. The launcher binaries carry no OS code signature on Windows and Linux and are
  ad-hoc signed on macOS unless the Apple secrets are present; OS code signing is out of scope for
  the preview.
- The backup manifest proves the backup is the copy taken and that a restore reproduced it. It is
  not an integrity record against someone who can write the data folder.
- The release source is fixed to `https` (or loopback `http`); redirects that leave `https` are not
  followed. `DAEDALUS_RELEASES_API` / `DAEDALUS_DOWNLOAD_BASE` change the source, and say so.

## Tests

- Unit (`go test -tags nowebview ./...`): `release_test.go`, `backup_test.go`, `upgrade_test.go`,
  `bridge_update_test.go`, `installer_test.go`, `signing_test.go`, and `live_test.go` — the last on
  live processes (this test binary re-run as a helper): the lock held by another process and freed by
  its SIGKILL, two upgrades at once, `update` refused while a launcher holds the installation, health
  accepting only this start's boot id, a foreign 200 without one after the port check (the race),
  an orphaned stack found and stopped, a port something else holds, a reused pid never killed.
- Go smoke (`DAEDALUS_SMOKE=1 go test -tags nowebview -run TestSmoke -v .`, Linux): the v0.12.0
  launcher built from its tag and two new ones as separate processes, releases from an httptest
  server on loopback, `install.sh`: the bridge and its byte-for-byte rollback; **two upgrades
  started together** (the second, `stop`, `--rollback` and `update` refused within milliseconds; the
  first's staging and the data untouched; the first committed with one backup); **SIGKILL** of both
  launchers while migrating, then `--rollback`; **Ctrl+C** (SIGINT to the process group) while
  migrating, and twice while the data is being restored — each a clean `rolled-back`, one
  `replaced-*`, the installation byte-for-byte as before; `update`; the installer handing over.
- Shell smoke (`desktop/upgrade-smoke.sh`): the same end to end through `install.sh`, 22 checks.
- Python (`tests/unit/test_launcher_updates.py`): the app's announcement (once per release, with
  the command, in the operator's language) and `X-Daedalus-Boot` on `/app` and `/app/` only.
- Browser (`DAEDALUS_BROWSER_TEST=1`): the launcher page's cards (the upgrade and the kept
  copies, the latter in English and Russian), rendered and read back.
- The fence (`fence*_test.go`, Linux, ext4; `DAEDALUS_FENCE_REQUIRE_EXT4=1` makes a skip a
  failure): writers held and arriving at each step, every change through a second name, the
  runtime guard, overflow at each phase, foreign exchanges, crashes and `update resolve`, removal
  races, and the update and upgrade taking the fence (`protect_linux_test.go`). The switch's
  guarantees are checked against their own removal by `fence_mutations.py`, which must turn every
  targeted test red. `DAEDALUS_FENCE_LONG=1` waits out the kernel's lease-break-time.

## Checking on a real, disposable native installation

Done on Linux, on a throwaway ext4 filesystem with its own home folder (never on the operator's
installation): a clean install through the setup page, the Update button and `update`, a new version
that does not come up (rolled back), a switch killed half way and settled with `update resolve`, and
the published v0.12.0 bridged to this launcher, with and without a new version that fails its health
check. Not done: the same on disposable macOS and Windows machines. The procedure, for a throwaway
machine or user account with nothing else of Daedalus on it, with network access:

1. Install the published v0.12.0 with the current one-liner (`DAEDALUS_DIR=$HOME/dx/Daedalus`),
   run it in native mode, skip the key, and let it reach "the app is up". Create one conversation so
   the database is not empty. Close it.
2. Build a candidate release from this branch (`build.sh`, or `go build -ldflags "-X
   main.version=desktop-v0.13.0-rc"` renamed to a plain `desktop-v0.13.0` tag in a local fixture),
   pack it like the workflow does, and serve it with `python3 -m http.server` on loopback;
   `DAEDALUS_RELEASES_API` and `DAEDALUS_DOWNLOAD_BASE` point `install.sh` at it.
   Before every step: `pgrep -a` shows no `daedalus`, `supervisor`, `ptyd` or `browserd` process,
   and at least three times the size of `data/` is free.
3. Run `install.sh` again: the bridge. Pass: the real `update` + start + `WaitReady` succeed; `curl -sI 127.0.0.1:<API_PORT>/app`
   shows `X-Daedalus-Boot` equal to the `DAEDALUS_BOOT_ID` in the supervisor's environment
   (`/proc/<pid>/environ`) and `ss -ltnp` names a process of that supervisor's session;
   `data/backups/*/manifest.json` verifies with the Python check from the smoke, the conversation is
   still there.
4. Force a failure (e.g. `chmod 000` the venv's python after the backup, or point
   `DAEDALUS_GIT_REMOTE` at a repository whose main does not start) and repeat: pass is the v0.12.0
   launcher and the data back byte-for-byte and v0.12.0 starting normally.
5. `update` with and without a forced failure on the upgraded installation.
6. Delete the VM. Then the same on disposable macOS and Windows machines (not the operator's):
   the bundle swap and Gatekeeper, and on Windows the rename of the running `.exe` and
   `install.ps1` under PowerShell 5.1 and 7.

## Left to do (no-go for users until done)

- Real macOS and Windows machines (above). The launcher's tests are set to run on both in CI
  (`desktop.yml`, the `window` job), `install.ps1` among them under Windows PowerShell — not yet run
  there; here it runs under PowerShell 7 on Linux, and the Windows test binary passes under Wine. What none of that
  covers: Gatekeeper and the bundle swap, the rename of a running `.exe`, and a real stack on either.
- The app's notification centre was not rendered with the new entry; there is no upgrade button and
  no path without a terminal.
- Docker mode.
