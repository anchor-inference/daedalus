# Daedalus on your own machine

`daedalus-desktop` is one small program — `Daedalus.app` on macOS — that turns a folder into a
running Daedalus. It fetches the two repositories, asks the handful of questions the installation
needs on a page of its own, writes the environment files, and then runs the agent one of two ways.

**The first run asks which**, and the choice is written next to the data and never asked again
(`--mode docker` / `--mode native`, or `DAEDALUS_MODE`, answers it from a script):

| | Native | Docker |
|---|---|---|
| **What it needs** | nothing | Docker Desktop (macOS, Windows) or Docker Engine with the compose plugin |
| **What runs the agent** | a process under the launcher, out of a private folder of pinned, checksummed binaries | a container from one published image, with its own filesystem and its own network |
| **First run downloads** | **103 MB** measured on Linux x86-64; ~96 MB on macOS (CPython is half the size there), ~148 MB on Windows (MinGit) | **114 MB** to pull the runtime image — 478 MB once unpacked — plus Docker itself, which is a ~600 MB application with a multi-gigabyte VM disk behind it |
| **On disk** | 245 MB of runtime in the user's cache folder (73 MB of it a wheel cache you can delete), 390 MB for the whole installation | 478 MB of image, plus the volumes |
| **Start to app** | 26 s from an empty folder, **4.3 s** warm | the image pull, then seconds; Docker Desktop itself must be up first |
| **Browser** | `daedalus-desktop install browser` — both Chromium builds into the runtime's `browsers/` ([the agent's browser](#the-agents-browser)) | `COMPOSE_PROFILES=browser`: the `browser` service from the `:browser` tag of the same image, sharing every layer below the last |
| **Isolation** | **no container boundary** — `Exec` runs as you, behind the policy rules ([the isolation, honestly](#the-isolation-honestly)) | a command that goes wrong stops at the container's edge |

Neither is the "real" one. Docker buys a wall; native buys weight and speed, and
[Native mode](#native-mode) says exactly what the wall was doing and what still stands without it.
Nothing is ever installed system-wide either way, and Docker is never installed for you.

On macOS and Windows it opens a window: the system's own web view, which shows the launcher's page
while the stack comes up and the app itself once it answers. On Linux it opens a browser window with
nothing around it — no tabs, no address bar — and falls back to the default browser. [The window](#the-window)
says why the three are not the same.

It is about 8 MB without the window and 10–12 MB with it, twice that as the universal macOS build.
In Docker mode everything that runs is in containers; in native mode the launcher downloads what it
needs into a runtime folder in the user's cache location (outside `data/`) and runs it from there.

## Get it

One line on macOS and Linux — it takes the newest `desktop-v*` release, checks the release's
signature by the project's key and the download against its `SHA256SUMS`, and unpacks it into
`./Daedalus`:

```sh
curl -fsSL https://raw.githubusercontent.com/anchor-inference/daedalus/main/desktop/install.sh | sh
```

On Windows, the same in PowerShell (not yet run on a real Windows machine):

```powershell
irm https://raw.githubusercontent.com/anchor-inference/daedalus/main/desktop/install.ps1 | iex
```

Both print the release key's fingerprint before they install anything. It must read

```
f75fa5a293fdd55b36c794f4787f7af8646e4dac95e19d74e6288e55c357c285
```

the SHA-256 of the key line `RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2` (minisign key id
`A18524FA935353DF`), published here and in every release's notes; a script that prints anything else
is not the project's. [SIGNING.md](SIGNING.md) has the rest.

`DAEDALUS_DIR=/somewhere/else` puts it elsewhere. Run over an installation that already has data,
neither script replaces anything itself: the launcher's `upgrade` does it — the installed one, or for
a launcher older than that command the one just downloaded — after a yes and with the data protected
first (a kept copy of the data folder on Linux with ext4, a checked backup elsewhere), and it rolls
back on failure. [UPDATES.md](UPDATES.md) has the whole of it.
Or take the archive by hand from the
[releases page](https://github.com/anchor-inference/daedalus/releases) (the tags beginning with
`desktop-v`):

| Machine | File | What is in it |
|---|---|---|
| macOS, both kinds | `Daedalus-macOS.zip` | `Daedalus.app` — one universal build for Apple Silicon and Intel, with the window |
| Linux x86-64 | `daedalus-desktop-linux-amd64.tar.gz` | `daedalus-desktop`, already executable; opens a browser window |
| Linux ARM64 | `daedalus-desktop-linux-arm64.tar.gz` | `daedalus-desktop`, already executable; opens a browser window |
| Windows x86-64 | `daedalus-desktop-windows-amd64.zip` | `daedalus-desktop.exe`, with the window |

Unpack it into a folder of its own — the installation is made **inside that folder**, so deleting
the folder deletes the installation.

On **macOS**, double-click `Daedalus`. When the release was built with the signing secrets in place
the app is signed and notarized and simply opens. When it was not, it is signed ad-hoc, and macOS
asks once about a copy that arrived through a browser: right-click the app, choose *Open*, then
*Open* again. A copy fetched by the one-liner above never asks at all — the quarantine attribute
that makes Gatekeeper ask is set by the browser, and `curl` does not set it. Unzip with Finder or
`ditto -x -k`, not with `unzip`: an app bundle carries symlinks and the signature's own extended
attributes, and `unzip` drops both, which leaves an app macOS calls damaged.

On **Windows**, unpack with `Expand-Archive daedalus-desktop-windows-amd64.zip -DestinationPath
Daedalus` in PowerShell and run `daedalus-desktop.exe` from the folder you want the installation to
live in. The executable is not signed, so SmartScreen warns once: *More info → Run anyway*.

## What happens on the first run

1. A page opens and asks **Docker or native**, with what each costs written next to it. A machine
   with a running Docker is offered Docker; a machine without one is offered native, because the
   alternative there is installing a whole application first. In Docker mode the client is then
   looked for in the places the installers put it — a program started from Finder inherits a PATH
   with none of them in it — and without it the launcher says what to install and waits there. In
   native mode nothing is checked, because nothing is expected.
2. The two repositories are fetched into the folder as GitHub tarballs and committed there — with
   `git` running inside the agent's own image in Docker mode, and with the runtime's own git in
   native mode. Each checkout is a real local history with
   no remote — an update is the next commit on top of it.
3. A page opens at `http://127.0.0.1:8770` — in the launcher's own window where there is one — with
   the whole of the setup on it: how it runs, one model provider key, and a daily spending cap that
   already has a figure in it. Telegram is behind a disclosure and stays optional — without it you
   use the app in that window. The page is in English or Russian; the switch is in its corner and
   the choice is remembered.
4. **Docker:** the image is pulled (or built, if there is no published image for your platform),
   the stack comes up, and the same window moves to the app. One image, two containers from it: the
   agent, and the key proxy that holds the provider keys.
   **Native:** the runtime is downloaded and checked, the environment is built from the checkout's
   own lock file, and the launcher starts the supervisor and the key proxy as its own child
   processes. Same two programs, same key file, no container between them and the machine.
5. **The app asks for a model, and that is the last step.** A key is an address; which model runs on
   it — and what it costs — is yours to pick, so the installation ships with none. The app opens on
   *Add a model*: choose the endpoint, choose a model from the list it serves (its context window,
   its modalities and its prices are shown), save. Nothing runs before that, and everything does
   after it. Later ones are added the same way from Settings → Models.

**In Docker mode, closing the launcher does not stop anything**: the containers are
`restart: unless-stopped` and come back with the machine, and the launcher's page is only a remote
control. **In native mode it does stop the agent**, and that is deliberate rather than a setting: a
container is visible in `docker ps` and has a restart policy of its own, while a supervisor started
by the launcher is an ordinary process with nothing above it and no window to say it is there, and
an agent you cannot see is one you cannot stop. A run in flight is not lost — the supervisor gives
the bot 25 seconds to drain, the run is snapshotted, and it picks up where it left off on the next
start.

Starting the launcher a second time against the same folder does not start a second one. It brings
the first to the front, hands it the link it was opened with if it was opened with one, and exits.

## The pages

The launcher serves three pages of its own, on the loopback address and nowhere else.

- **The questions** (`/setup`) — three cards: how it runs, a provider key, a day's spending. What is
  not a question is behind a disclosure: the difference between the two modes, what happens if you
  skip the key, and the four Telegram values. An empty field means *leave what is there alone*, so
  opening this page again to change one value cannot blank the others; emptying one on purpose is
  the tick under it.
- **The wait** (`/progress`) — where a start has got to: the steps of the mode you are in, ticked off
  as they pass, and one line of the launcher's own commentary under them. The bar is indeterminate
  until something knows a size and determinate once it does — the runtime archives are pinned, so
  their sizes are known before the first byte is fetched. A failure becomes one calm card with the
  button that tries again.
- **The status** (`/status`) — what is running, the buttons the command line has, and the log behind
  a summary.

Both languages are complete: every line of every page is in a table in `i18n.go`, and a key in one
language and not the other fails a test rather than leaving an English sentence in a Russian page.
The choice is written to `data/lang` beside `data/mode`, so the next start opens in it. What the launcher is
doing is a sentence in the same language, chosen by the stage it has reached, and its own running
commentary sits under that in monospace, shown as what it is: the same words that go to the
terminal. A failure the launcher recognises — no network, Docker not answering, a port taken, a full
disk — is a sentence too, with the program's own text kept behind *What happened*.

<p align="center">
  <img src="../docs/screenshots/launcher-setup-en-1440.png" alt="The launcher's first screen: how it runs, one provider key, a daily spending cap" width="100%" />
</p>

Pictures of all three, in both languages, at a window's width and a phone's, are in
`docs/screenshots/launcher-*.png`; `tests/browser/launcher_shots.py` renders them against an
invented installation and checks there that a language switch loses nothing that was typed.

## The folder

Everything the installation owns is in one folder — the one you unpacked into. On macOS the data
sits **beside** the app, not inside it, because the bundle is replaced by the next download:

```
Daedalus/
  Daedalus.app            or daedalus-desktop / daedalus-desktop.exe elsewhere
  data/
    daedalus/               the bot checkout; deploy/compose.yaml runs from here
    protocore-exp/          the core checkout
    daedalus-secrets/
      keyproxy.env          provider keys (0600) — outside every folder the agent can read
      ssh/                  keys and config for hosts the agent may reach; may stay empty
    .env                    what compose interpolates and the agent container reads
    compose.desktop.yaml    the launcher's override: the published image, Telegram made optional
    window.json             where the window was and how big, restored on the next start
    launcher.json           the running launcher's port and token (0600) — how a second launch finds it
    scheme.txt              which executable daedalus:// links are registered to
    browser-profile/        only when the app is shown in a browser window rather than the launcher's own
```

In **native mode** the same folder also holds the data a container would have kept in volumes:
the database, the sessions and the workspaces are files here. What can be rebuilt — the runtime —
and this machine's own state live outside it, so that an update copies and protects only what
cannot be rebuilt ([UPDATES.md](UPDATES.md#the-runtime-lives-outside-the-data-folder) has where, per
platform):

```
  data/
    mode                    docker or native, written once and read on every start
    state/                  the database, the sessions, the pairing links, the known-good history
    workspaces/             one per session

  <runtime>/                ~/.cache/daedalus/<key> on Linux; Application Support on macOS; %LOCALAPPDATA% on Windows
    uv/uv                   the installer for everything below it
    python/                 the CPython uv manages, for this installation only
    envs/<digest>/          the environment the agent runs in, one per lock file of the checkout
    bin/rg                  what Search uses
    git/                    MinGit — Windows only; elsewhere git is the machine's own
    node/  browsers/        extras, fetched on demand
    cache/                  uv's wheel cache; safe to delete, and the next sync refills it
    installed/              which version and which hash each tool was unpacked from

  <local state>/            ~/.local/state/daedalus/<key> on Linux, beside the runtime elsewhere
    logs/                   the supervisor's, the key proxy's and the terminal daemon's output
    pids/                   the records of running children
    ptyd/run/  ptyd/state/  the terminal daemon's endpoint and token (sealed), its journal of agent writes
    browserd/               the browser daemon's endpoint and the agent's browser profiles (sealed)
    supervisor.sock         the supervisor's socket
```

`data/` is next to the `.app` when the launcher runs from a bundle, and next to the working
directory otherwise — a plain executable run from a terminal makes `./data` where you are, as
before. `--data DIR` overrides both. (Finder starts a bundled program with `/` as its working
directory, which is why the bundle does not follow that rule.)

This is the layout a server install has, which is why the repository's compose file runs against it
unchanged. Provider keys are deliberately not in `.env`: that file is mounted into the agent
container, and the key proxy's file is not.

To move an installation, move the whole folder. To use a fork, set `DAEDALUS_GIT_REMOTE` and
`DAEDALUS_CORE_GIT_REMOTE` before the first run: the image is pulled from the fork owner's namespace
as well, so a fork's code never runs upstream's image. A fork that publishes no image has nothing to
pull, and the first start builds it locally instead. Both remotes must be GitHub repositories — the
checkouts are fetched from `codeload.github.com`, not cloned.

### The launcher runs the Python from the published branch, so releases go core first

The launcher is a binary; the supervisor and the key proxy it starts are `launcher/supervisor.py` and
`deploy/keyproxy/proxy.py` **out of the checkout it fetched**, which is the published branch and not
the tree the launcher was built from. So a launcher built from a branch whose Python has not been
published yet pairs with code that does not have that branch's fixes, and the two disagree silently:
the launcher passes `KEYPROXY_HOST=127.0.0.1` and a proxy that predates that variable binds every
interface anyway.

Release in the order **core → checkout → launcher**: publish the Python to the branch the launcher
fetches first, and cut the `desktop-v*` tag afterwards. Where the two must be able to disagree —
they always can, since the checkout moves on `update` — the Python side is written to fail safe on
its own: the key proxy binds the loopback interface unless something asks it for more, and a base URL
persisted against a container's address is migrated to this machine's on start rather than trusted.

Double-clicked from Finder there is no terminal to read, so the launcher's page opens in the browser
first and everything — the progress, a Docker that is not installed or not started, and the buttons
to try again — is on it. The launcher keeps serving that page whether the start succeeded or not;
closing it leaves the containers running.

## Projects

A project is a folder of your own — a repository, a directory of documents — that you add in the app
(Projects in the rail, the grid icon on the Agents screen). The agents you start in it work in that
folder: every path they *resolve* is checked against the project root and one that leads out of it is
refused — the file tools, the file browser, the preview, the download and the files they send you,
all at the one point that turns a path into a place. `Exec` runs **in** the folder and is bounded by
whatever bounds a command here: the sandbox where one is switched on, and the policy rules where it
is not (see [What the agent may do](../README.md#what-the-agent-may-do)). Several agents share one
project and see the same files. An agent started without a project still gets a scratch directory of
its own, as before.

A session writes five directories into the folder it works in — `inbox/`, `.exec/`, `.jobs/`,
`.services/`, and `.checkpoints/` when snapshots are on. Where the project root is a git checkout
they are added to `.git/info/exclude` when the first agent starts there, so they stay out of your
`git status` and out of a `git add -A`; they are yours to delete whenever you like.

`GET /api/projects` reports, per folder of each project, whether it is reachable from inside the
running process (`reachable`), whether it may be written (`writable`), and who could ever work in it
(`reach`: every agent, only what runs in a host terminal, or nothing yet). A project can have several
folders; they are added, locked read-only and removed in the project's settings.

**In Docker mode a project is also a bind mount, and that is the one place this mode is visibly
heavier than native.** The agent container sees only what is mounted into it, so a folder that is not
mounted is a project whose files are simply not there — which is what `reachable: false` says. The
launcher is what closes that gap: once the stack is up it asks the app which container folders of
its projects it cannot see and puts each of them in `data/project-mounts`, one path per line, which it splices into
the agent service's `volumes` in `data/compose.desktop.yaml` every time it writes that file. It then
says so on its page. **It does not restart anything by itself** — a restart takes the agent away from
whatever it is doing — so **Stop** and **Start** are what mount the folder, and until you press them
the project keeps saying it is not mounted.

If the launcher cannot reach the app or cannot write the file, it prints the entry to add by hand,
which is the same entry.

The entry is the folder mapped to **itself** — the same absolute path inside the container as outside:

```yaml
services:
  daedalus:
    volumes:
      - /home/you/work/bakery:/home/you/work/bakery
```

Same path on both sides, because the project stores the path you gave and that one string has to name
the folder from inside the container and from outside it. Read-only (`:ro`) is a supported choice and
is reported back as `writable: false`; the agents of that project can then read it and not change it.
Nothing else about the project lives in compose: the name, the root and the settings are in the
database, and the mount is only how the container comes to see the folder.

In native mode there is no container and nothing to mount: a project is reachable the moment it is
added.

## The window

What shows the app is decided when the launcher starts, by what the machine can actually do, and
never by a setting. Three steps, and none of them is a hard failure:

1. **The launcher's own window** — the operating system's web view: WKWebView on macOS, WebView2 on
   Windows. Nothing is bundled to provide it and nothing is downloaded; the window is about 2–4 MB
   of binary. The title is *Daedalus*, and the size and position it was left at are remembered in
   `data/window.json` and restored on the next start.
2. **A Chromium-family browser in application mode** — `--app=<url>` in Chrome, Edge, Brave or
   Chromium, which is a window with the page in it and no tabs, address bar or bookmarks. It gets a
   profile of its own in `data/browser-profile`, so it is separate from your browsing and keeps its
   own session. The browsers are looked for on `PATH` and in the places their installers put them,
   because a program started from Finder or Explorer inherits a `PATH` with none of them in it.
3. **The default browser** — a tab, which is what the launcher has always done.

**Windows** needs the WebView2 runtime for step 1. Windows 11 has it, and so does any machine whose
Edge is current; the launcher asks the registry before it tries, and quietly takes step 2 when the
answer is no. Microsoft's Evergreen bootstrapper installs it in a minute if you would rather have
the window.

**Linux does not get step 1 at all**, and that is deliberate. `webview_go` links GTK and WebKitGTK at
load time, so a binary built with it does not start on a machine without those libraries — it does
not fall back, and it does not warn; it fails to start. One binary that runs on every Linux is worth
more than a window of our own, so the published Linux builds begin at step 2. If you want the
window on Linux, you have the source: install `libgtk-3-dev` and `libwebkit2gtk-4.0-dev` and run
`go build` without `-tags nowebview` — and then that binary needs those libraries wherever it runs.

Whatever is showing it, closing it leaves the stack running.

### The menu bar, and the keyboard

On macOS the launcher installs the ordinary application, Edit and Window menus when it makes its
window. This is not decoration. A Mac application without a menu bar has no Edit menu, and without
an Edit menu ⌘C, ⌘V, ⌘X, ⌘A and ⌘Z do nothing anywhere in it: the key equivalent on a menu item is
what sends `copy:` and the rest down the responder chain, and with no item carrying it the
keystroke is never dispatched. It looks as though the window is eating the shortcuts. It is not:
nothing is delivering them. The menus carry the standard selectors and nothing of our own, and the
web view — which is the first responder — does the work. ⌘Q quits, ⌘W closes, ⌘M minimises.

Windows needs none of it: WebView2 hosts the same edit commands Edge does and handles Ctrl+C,
Ctrl+V, Ctrl+X, Ctrl+A and Ctrl+Z inside the page itself.

### Links

`daedalus://open/<session-id>` opens that conversation, from anywhere the desktop can follow a link.
The launcher registers the scheme once per executable, with no installer and no administrator:

| | How |
|---|---|
| macOS | `CFBundleURLTypes` in the bundle's `Info.plist`, read by Launch Services when it first sees the app |
| Windows | a key under `HKCU\Software\Classes\daedalus`, written on a first start |
| Linux | `~/.local/share/applications/daedalus-desktop.desktop` with `MimeType=x-scheme-handler/daedalus`, which also gives the launcher its name and icon in the desktop's menu |

A link handed to a launcher that is already running goes to that one; it never starts a second.

### Notifications

The launcher listens to the app's event stream (`/api/events`, as a `launcher` client) and raises a
desktop notification for each one the app's router marks for the desktop — `osascript` on macOS, a
toast through PowerShell on Windows, `notify-send` on Linux. The router decides, not the launcher: it
knows whether the app is in front of the operator, their quiet hours and what already reached their
phone. Nothing is bundled for it; a machine without `notify-send` says so once in the launcher's log
and is not asked again.

A click opens what the notification is about where the platform tells anyone about it: on Windows
the toast follows its `daedalus://open/<session>` link, and on Linux the launcher waits for the click
(at most ten minutes, at most eight notifications at once) and shows the address. On macOS a
notification raised by `osascript` carries no link, so a click brings nothing forward.

After a reconnect the app replays what the launcher missed; anything more than two minutes old is
folded into one line ("3 notifications while the launcher was away"). The stream is authorised with
the token the app minted for itself, which the launcher reads out of the state database and keeps in
memory only. An installation where that token cannot be read gets no notifications and says so;
nothing else changes.

## Commands

| Command | What it does |
|---|---|
| `daedalus-desktop` | set up if needed, start the stack, open the app |
| `daedalus-desktop setup` | ask the questions again and rewrite the configuration |
| `daedalus-desktop status` | what is configured, what is running |
| `daedalus-desktop stop` | stop the containers; they stay down until started again |
| `daedalus-desktop logs -f` | the stack's logs |
| `daedalus-desktop update` | move both checkouts to what is published and restart, after a checked backup of the data; a failed start puts the data back. Native mode only for now; see [UPDATES.md](UPDATES.md) |
| `daedalus-desktop check-update` | say whether a newer launcher release is published; installs nothing |
| `daedalus-desktop upgrade [--yes]` | move to a newer launcher release: asks, stops the stack, backs up and verifies the data, swaps the launcher, updates and starts, and rolls both back on failure. Native mode only for now; see [UPDATES.md](UPDATES.md) |
| `daedalus-desktop open` | open the app in the browser |
| `daedalus-desktop pair` | print a fresh pairing link for signing in to the app |
| `daedalus-desktop uninstall [--keep-data]` | remove the containers, networks and volumes (Docker mode; see [Uninstalling](#uninstalling)) |

Flags: `--data DIR` (default `./data`), `--port N` for the launcher's own page (default 8770),
`--setup` to ask the questions again on a start, `--version`.

On the setup page, **a field left empty keeps whatever is already in force** — re-running setup to
change the daily cap does not blank the provider keys, and the public address set by hand in
`data/.env` is never touched. A value is removed only by ticking *remove* beside it.

## Signing in

The first start opens the app through a one-time link the stack writes when it comes up, and the
launcher offers that link once, while it belongs to that start. Afterwards `open` goes to the app
itself, whose login screen takes a passkey, Telegram, or a pairing link. `daedalus-desktop pair`
prints a fresh link whenever a browser needs one — a new machine, a cleared cookie jar, or a session
that has expired. Each link opens once and expires after thirty minutes.

Settings that are not on the setup page — a GitHub token for self-development, a public address for
the app, extra providers and the search APIs — are edited in `data/.env` and
`data/daedalus-secrets/keyproxy.env` afterwards, exactly as on a server. `deploy/env.example` and
`deploy/keyproxy.env.example` in the checkout describe every key.

## Self-development on a desktop install

A desktop install has the two checkouts and usually no GitHub token, so the capability probe resolves
`[self_change] mode` to **`local`**: the agent gets `SelfWorkspace` and `SelfApply` and improves its own
code in the checkout the stack runs from, with no fork, no remote and no pull request. The app marks the
Changes screen `local`.

**How a change reaches you.** The agent works in a worktree, runs the tests through `Verify`, commits, and
calls `SelfApply`. Its commits are fast-forwarded onto the checkout's own branch — a plain local git
history you can read with `git -C data/daedalus log` — and then three places say the same thing:

- the app shows a strip above the screen, *"Changes are ready — restart to apply"*, with the summary and a
  **Restart** button;
- the launcher's status page shows a card with the same line and a **Restart to apply** button;
- `daedalus-desktop status` prints a `changes` line.

Any of the three applies it, and so does the plain thing: **close the app and open it again.** The
checkout is what runs — the image only supplies the environment — so the containers coming back up is the
whole of applying a change.

**What protects you.** The restart is not a leap. The supervisor checks out the agent's commit into a
detached worktree of its own and runs there: `uv sync` (only when `uv.lock` or `pyproject.toml` changed —
the virtualenv lives on the `daedalus-venv` volume and survives), `compileall`, `daedalus check` and the
smoke tests. Only if all of that passes is the running bot stopped. A change that fails is taken back out
of the checkout and the app says so with the reason. A change that passes but cannot stay up — three
starts dying within ten minutes — puts the last known-good commit back on its own and tells you; the
commit is not lost, it is still on the branch the agent made it on. A change to the `Dockerfile` or the
system packages is applied as far as a restart can take it, and says the rest needs
`daedalus-desktop update`.

Give the install a `GITHUB_TOKEN` with Contents and Pull requests on both forks and it
resolves to `server` instead — the full worktree → pull request → approval → rebuild workflow.
Set `mode = "off"` in the configuration and the subsystem is not there at all: no tools, no screen, no
`/api/proposals`, and nothing in the prompt about changing its own code. `daedalus doctor` names the
mode it resolved and what a mode you chose yourself is missing.

## Native mode

The same agent, the same supervisor, the same code — with the operating system where the container
was. What that changes, in both directions.

### What is downloaded, and where

Everything goes into the runtime folder and nowhere else. No package manager is run, no PATH is
changed, nothing is installed system-wide. Every version is pinned in `desktop/runtime.go` next to
the SHA-256 the publisher published, and **a download whose hash does not match is not used**: it is
refused by name and the start fails saying so.

| | Version | Download | On disk | Where it comes from |
|---|---|---|---|---|
| `uv` | 0.12.15 | 19.4 MB (Linux x86-64) | 50 MB | astral-sh/uv release, `sha256.sum` |
| CPython | 3.12 | ~32 MB | 103 MB | python-build-standalone, fetched **by uv**, which checks its own downloads — which is why there is no second hash for it here |
| `rg` | 15.2.0 | 2.3 MB | 5 MB | BurntSushi/ripgrep release, its own `.sha256` |
| `git` | — | — | — | the machine's own, everywhere but Windows |
| MinGit | 2.55.0.5 | 39 MB | ~120 MB | git-for-windows release; **Windows only** |
| the environment | from `uv.lock` | the rest of the 103 MB | 85 MB | PyPI, through uv, against the lock |

**Measured, Linux x86-64, from an empty folder to the app answering: 103 MB over the wire, 26
seconds.** On disk that is 245 MB of runtime (73 MB of it uv's wheel cache, deletable at any
time) and 390 MB for the whole installation including both checkouts. A warm start — everything
already downloaded — is **4.3 seconds** from launching the binary to `/app/` answering 200. macOS is
smaller (CPython is about half the size there) and Windows larger by MinGit.

Optional, fetched only when something asks for them — `daedalus-desktop install node` /
`daedalus-desktop install browser`, or the buttons on the launcher's page:

| | Download | What needs it |
|---|---|---|
| Node 24.21.0 | 58 MB (Linux x86-64) | four skills that shell out to `npx`, and rebuilding the Mini App |
| Chromium, full and headless shell | 316 MB (Linux x86-64: 193 MB full, 120 MB shell); 656 MB on disk | the agent's browser (the full build, under `browserd`), the browser skills and `ImageView`'s screenshots (the shell) |

Against Docker mode that is roughly four times lighter to download, and it does not need Docker
Desktop — a ~600 MB application with a multi-gigabyte VM disk behind it — at all.

### The isolation, honestly

Everything in the agent still applies, because it was never the container doing it: the policy
engine with its ASK and DENY rules, the approval gates, the protected paths, the egress allowlist,
the per-run and per-day spending caps the supervisor enforces from its own environment, and the
public-text gate. `daedalus doctor` prints the same sentence.

What is gone is the wall behind them:

- **`Exec` runs as you.** A command the agent runs has your files and your credentials, and the only
  things between it and them are the rules above. On Linux bubblewrap still confines what a command
  **writes**: `tools.exec.sandbox` **defaults to `workspace`** on a native install where bubblewrap
  actually runs — there is no container here to be the wall instead. It binds the filesystem
  read-only rather than hiding it, so it is a wall against writing and not against reading; what
  refuses the installation's own files is the rule below. Ubuntu 24.04 and Debian 13 forbid
  unprivileged user namespaces out of the box, and `bwrap` is often installed on them anyway by
  flatpak or a desktop; there the probe fails, the default is `off`, and the doctor says which of
  the two it is. On macOS and Windows there is no bubblewrap, and the doctor says so rather than
  reporting it as missing software.
- **Key isolation is weaker, but not gone.** The key proxy is still a separate process, still the
  only one that holds a provider key, still bound to `127.0.0.1` and nothing else, and the key file
  is still `0600` outside every folder the agent works in — the agent process never holds a key. But
  a shell the agent starts runs as the same user, and a file mode protects nothing from a process
  that owns the file. So the rule below names it instead.
- **The ports are the machine's.** `SERVICES_PUBLIC_HOST` is `127.0.0.1` and a service a session
  starts binds there, so nothing is published to the network — but it is the same loopback interface
  every other program of yours can reach. The supervisor's own command channel is a socket file with
  an owner everywhere but Windows, where it has to be a loopback port: there it asks for a secret it
  keeps in `data/state/supervisor.token`, because a port has no owner and a file does.

**Two rules exist only here**, and both are off in Docker mode, where the directories they name are
not in the container at all:

| | |
|---|---|
| **The installation's own files are refused, to read as well as to write** | `data/daedalus-secrets/` (the provider keys), `data/state/daedalus.sqlite` and its journals, `data/state/supervisor.token`, the launcher's executable and the whole of the runtime and the local state folders. Through `Exec` too — `cat`, `cp`, a redirection, a `tar -C` — because the rule reads the paths in the command, not only the tool that was called. A denial is final: no approval lifts it. |
| **A path in your home folder, outside every project, asks** | Anything under `$HOME` that is not a project root, a session workspace, one of the two checkouts or part of the installation is a question with an approval key. *Allow once* in the app, or `/allow <key>` in the chat, lets that exact call through one time. |

Everything else is where it was: the egress allowlist still escalates a host it does not know, the
spend caps are still the supervisor's and not the agent's to edit, and `GOVERNANCE.md` is still the
one file the agent can read and cannot change. `daedalus doctor` prints the isolation in one line,
and so does the launcher's status page.

If any of that matters more than 500 MB and a second of start-up, use Docker mode. That is the
whole of the trade, and the setup page says it in those terms.

### The supervisor, and the launcher above it

`launcher/supervisor.py` is unchanged and does the same job in both modes: it starts the bot,
restarts it when it dies, preflights a change on a detached copy of itself before letting it run,
and rolls a revision back that cannot boot three times in ten minutes. In Docker mode it is PID 1 of
the container; here it is a child of the launcher, and the launcher does what compose did — rotates
its log, starts it again when it exits, and stops it on quit.

**Apply is therefore two hops, not one.** The launcher asks the supervisor for a restart over its
socket; the supervisor preflights the commit and re-execs the bot only if that passes. Stopping and
starting the process from the launcher would put the change live with nothing having looked at it,
which is why Apply does not do that.

Three small differences inside the supervisor, all of them about the platform rather than the mode:
the command socket is a file in the state directory where there are unix sockets and a loopback port
on Windows where there are not; the zombie reaper reads `/proc` and is therefore Linux-only; and the
owner-restoring `chown` exists because a container runs as root over a host mount, which is not the
case when it is your own process writing your own files.

### Host terminals

A native install's terminals are **host terminals**: shells on this machine, as you, in your home,
with your own logins and PATH (the runtime's tools come after yours). They are served by `ptyd`, the
terminal daemon, which each release archive carries beside the launcher (`ptyd`, `ptyd.exe`, or
`Contents/MacOS/ptyd` in the Mac bundle). The launcher starts it first, as a child of its own beside
the supervisor rather than under it, so **applying a change or restarting the agent leaves every
terminal running** and the app reattaches. **Quitting the launcher ends them**, as it ends the agent:
nothing of the installation keeps running behind a closed launcher. A daemon that crashes is started
again with the supervisor's backoff; the terminals it held are gone, and the app shows them lost.

- On Linux and macOS the daemon listens on a socket in the local state's `ptyd/run/` (loopback TCP when
  the folder is so deep that the socket's path would be too long); on Windows on a loopback port,
  with the directory's access list giving it to you alone. The directory is sealed from the agent.
- **Updating it** is updating the launcher: a new release carries its daemon, and the restart that
  installs it ends the terminals like any quit.
- A build without the daemon (`go build` in `desktop/`, most often) still runs; the app shows host
  terminals unavailable with "this build carries no ptyd". `DAEDALUS_PTYD=/path/to/ptyd` names one
  built by hand (`ptyd/release.sh`, or `go build ./cmd/ptyd` for a daemon without the screen emulator).
- On Windows the shell is PowerShell 7 when it is installed, else Windows PowerShell; there is no
  sandbox toggle there, and no process statistics.
- Docker mode on a desktop has container terminals only (the `terminals` service); the launcher starts
  nothing for them.

### The agent's browser

The agent's browser natively is `browserd`, the browser daemon, which each release archive carries
beside `ptyd` (`browserd`, `browserd.exe`, or `Contents/MacOS/browserd` in the Mac bundle). The
launcher starts it after the terminal daemon, as another child of its own, so applying a change or
restarting the agent leaves every browser open; **quitting the launcher closes them**. Chromium is
not in the archive: `daedalus-desktop install browser` fetches Playwright's pinned builds into
the runtime's `browsers/` — the full Chromium the daemon runs, and the headless shell the browser
skills drive (Playwright's headless launch looks for that one and does not start without it).

- **The wall.** There is no container here. Every connection a page makes goes through the daemon's
  own proxy, which refuses your LAN, cloud metadata addresses and the installation's own ports (the
  app's API, the key proxy, the daemons' hook ports, the launcher's page), on loopback and on your
  machine's other addresses alike. Your other local ports are a question you answer once; the
  services the agent starts are open. **That proxy is the only wall** between a page and your
  machine: `daedalus doctor` says so under "browser walls (host)".
- **The sandbox.** Chromium's own sandbox is always on. On Ubuntu 23.10 and later a downloaded
  Chromium cannot make the user namespaces it sandboxes with, so the launcher hands it the setuid
  helper of a Chrome or Chromium installed on the system (`/opt/google/chrome/chrome-sandbox` and
  the like) when there is one. With none, the browser does not start, and the doctor names the fix;
  the daemon never turns the sandbox off by itself.
- **Memory.** On Linux the daemon and every Chromium it starts run in a systemd scope of yours
  capped at 3 GB (`systemd-run --user --scope -p MemoryMax=3G`), when your session has a user
  manager to ask; without one they run uncapped and the launcher's log says so.
- **Profiles**, which hold the logins you make for the agent, live in the local state's
  `browserd/state/`, and the run directory in `browserd/run/` beside it; the agent's policy seals
  the local state folder whole. `DAEDALUS_BROWSERD=/path/to/browserd` names a daemon built by hand
  (`browserd/release.sh`); a build without one shows the browser unavailable with "this build
  carries no browserd".
- Docker mode on a desktop runs the `browser` compose service instead when `COMPOSE_PROFILES=browser`
  is in the data folder's `.env`, from the published `:browser` image.
- **Not yet proven:** the Mac's Gatekeeper on a Chromium downloaded into the data folder, and the
  whole of it on Windows, where it is cross-compiled but has not run.

### Windows

Implemented and cross-compiled, with the path and argument logic under tests of its own, but **not
run on a real Windows machine** — see [what is not yet proven](#what-is-not-yet-proven).
MinGit is unpacked into the runtime's `git/` with no installer and no PATH change. It ships `sh.exe`
(a dash), **not** bash: `Exec` runs `sh -c` there, so a command written with bash arrays or `[[ ]]`
will not run. The supervisor listens on `127.0.0.1:8769` instead of a socket file — which is a port
any process on the machine can reach, where the socket file has an owner; it is the platform's
limitation, not a choice, and it is stated here rather than hidden.

### What is not yet proven

- Everything Windows: the MinGit unpack, `sh -c`, the loopback supervisor, `taskkill` stopping the
  tree. Compiled, unit-tested for the path and argument logic, never run on Windows.
- The terminal daemon on Windows: the pseudoconsole, the job object, the access list, PowerShell
  and its shell marks, the hard-linked hook command. Cross-compiled with the emulator for amd64 and
  arm64, its text rules unit-tested everywhere; its tests of a real console run only on the release
  workflow's Windows runner. Older console hosts may drop the shell's marks.
- macOS: the `xcode-select` probe and the folder dialog. Compiled, unit-tested, never run on a Mac.
  The terminal daemon has only been vetted for macOS; its tests and the signing of the nested binary
  run for the first time on the workflow's Mac runner.
- The folder picker's dialogs (`osascript`, `FolderBrowserDialog`, `zenity`/`kdialog`) — each needs
  its own desktop.

## Disk

In **native mode** there are no images at all: the runtime is 245 MB after a first start (73 MB
of it uv's wheel cache, safe to delete at any time) and the whole installation is about 390 MB with
both checkouts in it. `install node` adds 58 MB of download, `install browser` 316 MB (656 MB on disk).

In **Docker mode**, one image, and the key proxy is a second container from it:

| Image | Size on disk | Why |
|---|---|---|
| `ghcr.io/ascorblack/daedalus` | 478 MB unpacked (114 MB to pull) | Ubuntu, Python, uv, the environment, the built Mini App. Runs the agent and the key proxy |
| `aiogram/telegram-bot-api` | ~66 MB | the local Bot API server; only with Telegram on |

Budget **about half a gigabyte for the image**, plus the volumes: the database and the agent's
memory are megabytes, but the per-session workspaces grow with what the agent downloads and builds.
`uninstall` without `--keep-data` removes the volumes; images are removed with `docker image prune -a`.
Token estimation runs on the core's pure-Python estimator in this image — its optional compiled
extension has no published wheel yet, so there is nothing to install; `daedalus doctor` reports
which one is active under "token counter".

What is not in it, and what it costs to add:

| | Size | How |
|---|---|---|
| the browser skills (Playwright, a headless Chromium, Pillow) | +~550 MB | run the `:browser` tag of the same image; it shares every layer below the last |
| a self-hosted SearXNG | +~382 MB | `--profile search`. Without it `WebSearch` goes to DuckDuckGo directly |
| the rebuilder, for a server that builds its own images | +~237 MB | `--profile selfdev` |

The image is published for `linux/amd64` and `linux/arm64`, so Apple Silicon pulls it like
everything else. If a pull fails anyway the launcher builds locally instead — a few minutes the
first time, and everything after it is the same.

## Uninstalling

**Run `daedalus-desktop uninstall`, then delete the folder.** The folder holds what the installation
owns — the checkouts, the database, the sessions, the workspaces, the keys, and beside the data folder
`.daedalus-update/`, the copies of the data an update kept. No package manager was run, no PATH was
changed, nothing was installed system-wide.

In native mode two things of the installation's live outside the folder, because neither may be part
of what an update copies and switches: the downloaded runtime (the interpreter, the environments, the
tools) and this machine's local state (the logs, the terminal and browser daemons' endpoints, the
browser profiles with their logins). `uninstall` removes both; by hand they are

| | Runtime | Local state |
|---|---|---|
| Linux | `~/.cache/daedalus/<name>-<hash>` | `~/.local/state/daedalus/<name>-<hash>` |
| macOS | `~/Library/Application Support/Daedalus/Runtime/<name>-<hash>` | `~/Library/Application Support/Daedalus/State/<name>-<hash>` |
| Windows | `%LOCALAPPDATA%\Daedalus\Runtime\<name>-<hash>` | `%LOCALAPPDATA%\Daedalus\State\<name>-<hash>` |

where `<name>-<hash>` is the data folder's name and a hash of its path, so two installations on one
machine never share them.

Three more things live outside it, all of them small, all of them optional to clean up:

| | Where | Remove it with |
|---|---|---|
| Docker images and volumes (Docker mode only) | Docker's own storage | `daedalus-desktop uninstall` before deleting the folder — it takes the containers, networks and volumes; then `docker image prune -a` for the images |
| the `daedalus://` link registration | `~/.local/share/applications/daedalus-desktop.desktop` on Linux, `HKCU\Software\Classes\daedalus` on Windows, Launch Services on macOS (which forgets a bundle that is gone) | delete the file or the key; on macOS nothing to do |
| a browser profile, if the app was shown in one | `data/browser-profile/` | inside the folder already |

Your projects are **not** in the folder: a project is a folder of your own that the installation only
ever pointed at, and nothing here deletes one. What an agent wrote inside it stays there — the files
it was asked to make, and the five directories listed under [Projects](#projects) — so a project
folder you are finished with is cleaned up by deleting those, in the folder itself.

## Building it yourself

`./build.sh` cross-compiles all five binaries into `dist/` inside the Go container, so the only
dependency is Docker here as well:

```bash
cd desktop
./build.sh            # GO_IMAGE=golang:1.23 by default; VERSION= to stamp a version
```

Those are `nowebview` builds — the browser fallback, no window. The window is cgo, and cgo is not
cross-compiled: a windowed macOS build is made on macOS and a windowed Windows build on Windows,
which is what the release workflow's matrix of native runners does. To build the windowed launcher
for the machine you are on, `go build -trimpath -o daedalus-desktop .`.

`./package-macos.sh VERSION AMD64 ARM64 OUTPUT_DIR` turns the two macOS binaries into
`Daedalus.app` inside `Daedalus-macOS.zip` — one universal executable made with `lipo`, the
`Info.plist`, an `AppIcon.icns` built from `docs/brand/avatar-bot.png`, a signature, and the zip
made with `ditto -c -k --keepParent`. It needs macOS: `lipo`, `sips`, `iconutil`, `codesign` and
`notarytool` are all Apple's. Without the signing secrets in the environment it signs ad-hoc and
says so, which is also what happens in CI when the secrets are not set.

The module has one dependency outside the standard library — `github.com/webview/webview_go`, the
web view, pinned to a commit because the project publishes no tags. Tests, for both builds:

```bash
docker run --rm -v "$PWD":/src -w /src -e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod golang:1.23 \
  sh -c 'go vet ./... && go test ./... && go vet -tags nowebview ./... && go test -tags nowebview ./...'
```

On Linux both of those are the same build, since the window is not compiled in there; the cgo build
is exercised by the release workflow on macOS and Windows runners.

## Signing releases

A release signed with a Developer ID certificate and notarized by Apple opens with a double-click
and no questions; without the secrets below the same release is signed ad-hoc, which is fine for
anyone installing with the one-liner and one right-click → *Open* for anyone who downloaded it in a
browser. **A missing secret never fails the release** — the workflow signs ad-hoc, says so in the
job log, and says so in the release notes.

Five repository secrets, all five needed before signing is attempted:

| Secret | What it is | Where it comes from |
|---|---|---|
| `APPLE_CERTIFICATE_P12` | base64 of a **Developer ID Application** certificate exported as `.p12` | Apple Developer account → *Certificates, Identifiers & Profiles* → *Certificates* → **+** → *Developer ID Application*. Upload a CSR made by Keychain Access (*Certificate Assistant → Request a Certificate From a Certificate Authority*), download the `.cer`, open it (it lands in the login keychain), then right-click the **private key** under *My Certificates* → *Export* to get the `.p12`. Requires the Apple Developer Program. |
| `APPLE_CERTIFICATE_PASSWORD` | the password that export asked for | you choose it during the export |
| `APPLE_ID` | the Apple account the app is notarized under | the account that owns the certificate |
| `APPLE_TEAM_ID` | ten characters, e.g. `A1B2C3D4E5` | [developer.apple.com/account](https://developer.apple.com/account) → *Membership details* → *Team ID* |
| `APPLE_APP_PASSWORD` | an **app-specific** password, not the account password | [appleid.apple.com](https://appleid.apple.com) → *Sign-In and Security* → *App-Specific Passwords* → **+** |

The certificate is put into the secret base64-encoded, because a repository secret holds text:

```bash
base64 -i DeveloperID.p12 | tr -d '\n' | pbcopy    # macOS
base64 -w0 DeveloperID.p12                         # Linux
```

What the workflow then does on the macOS runner: imports the certificate into a temporary keychain
it deletes afterwards, signs the executable and the bundle with `--options runtime --timestamp` and
the (deliberately empty) entitlements in `macos/entitlements.plist` — the hardened runtime is what
notarization requires — submits the zip with `xcrun notarytool submit --wait`, staples the ticket to
the bundle so the first launch needs no network, and zips it again.
