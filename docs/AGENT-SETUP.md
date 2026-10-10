# Setting up Daedalus on a server — instructions for a coding agent

This page is written for an agent (Claude Code, Codex, Cursor, OpenCode, a Daedalus of your own …) that
was asked to install Daedalus on a Linux server for one person, the operator. Every step is a command or
a file; every check names what it must print. Ask the operator only for what the steps say you need.

## 0. What you are installing

Daedalus is a personal, self-developing agent: a Docker Compose stack of three containers from one image
(the agent, a key proxy that holds provider keys, and the terminals the app opens), plus three optional ones behind profiles —
`search` (a self-hosted SearXNG), `selfdev` (the rebuilder, which a server that develops itself needs)
and `telegram` (a local Bot API server). The operator talks to it in a web app (a PWA, installable on a
phone) and, optionally, in Telegram. Read `README.md` once; it is short.

Requirements on the server: Docker Engine with the compose plugin (`docker compose version`), 4 GB of
RAM free, 5 GB of disk, outbound HTTPS. A public HTTPS address is
optional: without it the operator opens the app over an SSH tunnel or a VPN; with it the app is a
proper PWA and Telegram's Mini App works.

## 1. What to ask the operator before you start

Ask these once, together; do not start until you have the answers.

1. **A provider.** One of: an API key for DeepSeek, OpenRouter or OpenCode Go; or a self-hosted
   OpenAI-compatible endpoint (base URL + key); or a ChatGPT / Claude Code / SuperGrok login already
   present on this server (`~/.codex`, `~/.claude`, `~/.grok`) — those are used as providers through
   the key proxy. A key is an address, not a model: the installation ships with no model at all, and
   the operator picks theirs in the app at the end (section 3a). Ask which one they want and what
   they are willing to spend on it, so you can point them at it when you hand the app over.
2. **Telegram, or not.** If they want Telegram: a bot token from @BotFather, their numeric Telegram
   user id, and (for files over 20 MB) API id + hash from https://my.telegram.org/apps. If not, skip
   every Telegram value: the app in the browser is the whole interface.
3. **A public address, or not.** A domain that already points at this server (for HTTPS) or "no".
4. **The daily spend cap** in USD (default 20).
5. **Self-development.** A fine-grained GitHub token limited to the two repositories they fork
   (`daedalus`, `protocore-exp`), with Contents and Pull requests read/write — or "later", or "never".
   This answer decides `[self_change] mode` (see §7): with the token, the checkouts and the
   `rebuilder` service the installation resolves to `server`; without the token, to `local` — the
   agent edits its own checkout and the change applies on a restart; "never" is `mode = "off"`, and
   the tools, the API, the app screen and the prompt section all go with it.

## 2. Install

```bash
# as the operator's user, in their home directory
git clone https://github.com/anchor-inference/daedalus
git clone https://github.com/anchor-inference/protocore-exp     # the core, next to the bot: ../protocore-exp
mkdir -p daedalus-secrets/ssh && chmod 700 daedalus-secrets
cd daedalus
cp deploy/env.example .env
cp deploy/keyproxy.env.example ../daedalus-secrets/keyproxy.env && chmod 600 ../daedalus-secrets/keyproxy.env
```

Fill `.env` (secrets do NOT go here; it is mounted into the agent container):

| key | value |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `OWNER_USER_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | the Telegram answers, or leave empty |
| `MINIAPP_PUBLIC_URL` | `https://<domain>` when there is one, else empty |
| `USD_PER_DAY` | the cap |
| `SEARXNG_SECRET` | any random string (`openssl rand -hex 16`) |
| `DAEDALUS_COMPOSE_PROJECT_DIR`, `DAEDALUS_COMPOSE_FILE`, `DAEDALUS_CORE_PROJECT_DIR` | absolute paths: the `daedalus` checkout, its `deploy/compose.yaml`, the `protocore-exp` checkout |
| `SERVICES_PUBLIC_HOST` | the server's LAN or public address (the operator opens agent-hosted services at `<host>:<port>`) |
| `GITHUB_TOKEN` | the self-development token, or empty (empty resolves self-development to `local`) |

Fill `../daedalus-secrets/keyproxy.env` with the provider keys (`DEEPSEEK_API_KEY=…`, `OPENROUTER_API_KEY=…`,
`OPENCODE_API_KEY=…`; a self-hosted endpoint is `VLLM_BASE_URL`/`VLLM_API_KEY` in `.env`).

Before starting, create the host terminal's directory as the operator — compose mounts it whether or not
the host terminal is installed, and Docker would create a missing one as root:

```bash
bash deploy/host-terminal.sh prepare-dir        # ../daedalus-host-terminals, 0700, the operator's
```

Start:

```bash
# without Telegram
docker compose -f deploy/compose.yaml --env-file .env up -d --build
# with Telegram
docker compose -f deploy/compose.yaml --env-file .env --profile telegram up -d --build
```

That is **one image and three containers from it**: the agent, the key proxy that holds the
provider keys, and `terminals`, the daemon behind the app's container terminals. Everything else is
a profile, and none of them is on unless you name it. Add only the
ones the operator's answers in section 1 asked for:

| `--profile` | What it starts | Cost | Add it when |
|---|---|---|---|
| `telegram` | the local Bot API server — files up to 2 GB instead of Telegram's 20 MB | ~66 MB | they want Telegram **and** gave you `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` |
| `search` | a self-hosted SearXNG | ~382 MB | they asked for one. Without it `WebSearch` goes to DuckDuckGo directly and SearXNG is only the fallback it cannot reach |
| `selfdev` | the rebuilder, the only container that can reach Docker | ~237 MB | self-development is to resolve to `server` (section 7), which needs a way to build a new image |
| `browser` | the agent's browser: `browserd` and Chromium from the image's `browser` target, on a network of its own with no route to the key proxy or the agent | the `:browser` tag | they want the agent to browse and to watch it in the app. Put `COMPOSE_PROFILES=browser` in `.env` rather than passing the flag, so every later compose command (the rebuilder's too) keeps it |

The browser skills — driving a page with Playwright, drawing with Pillow — are not in the default
image: they are two thirds of one and most sessions never open a page. A server that needs them runs
the `:browser` tag of the same image instead (`ghcr.io/ascorblack/daedalus:browser`); without it the
skills say they are not installed rather than writing scripts that cannot run.

The first build takes a few minutes (the Python environment and the Mini App). Check:

```bash
docker compose -f deploy/compose.yaml --env-file .env ps        # daedalus, keyproxy (+ whatever profiles are on) "running"
docker logs deploy-daedalus-1 2>&1 | grep -E "bot started|pairing link|polling"     # "pairing link written to …" without Telegram
```

`bot started pid=… bot=<sha> core=<sha>` must appear. Without Telegram there is no "polling" line; that is
expected.

## 3. Log the operator in

The app is at `http://127.0.0.1:8765/app` on the server. When there is no other way in (no Telegram, no
passkey yet) the server writes a one-time **pairing link** to the state volume at start (the log says
`pairing link written to …`; the link itself is never logged):

```bash
docker exec deploy-daedalus-1 cat /srv/state/pairing-url
# a new one at any time:
docker exec deploy-daedalus-1 /srv/venv/bin/python -m daedalus auth pair
```

Give that link to the operator (it works once, for 30 minutes). Opening it signs the browser in. Then tell
them to add a **passkey** in the app (Settings → Security → Add a passkey) so the next login is Face ID /
Touch ID / a security key and needs no link. With Telegram configured, the login page also offers
"Log in with Telegram".

If there is no public address, the operator reaches the app through a tunnel:
`ssh -L 8765:127.0.0.1:8765 <server>` and then `http://127.0.0.1:8765/app` on their machine; the pairing
link works through the tunnel as printed (it uses `http://127.0.0.1:8765`).

## 3a. The operator adds a model

Signed in, the app opens on **Add a model** and stays there: the installation has none, and nothing —
not a chat message, not a schedule — can run until one exists. Three steps on one page: the endpoint
(the ones whose key the key proxy holds are marked ready), the model from the list that endpoint
serves (with its context window, its modalities and its prices), and how it runs. Saving makes it the
default.

This is the operator's decision, not yours: do not pick a model for them. If they ask you to, ask
which one and what it may cost first. You can confirm the state from the shell at any time:

```bash
docker exec deploy-daedalus-1 /srv/venv/bin/python -m daedalus check   # "model: none — …" until one is added
```

## 3b. Projects, when the operator has folders of their own on the server

A **project** is a folder the operator adds in the app (Projects in the rail, or the grid icon on the
Agents screen): the agents started in it work in that folder, and every path they resolve is checked
against it — the file tools, the file browser, the preview, the download and the files they send.
`Exec` runs in the folder; the body of a command is bounded by `tools.exec.sandbox`, which is `off` by
default in a container, so switch it on if the boundary has to hold against a shell too. Without a
project a session gets a scratch directory of its own under the workspaces root, which is what every
session had before and still gets.

Each session writes `inbox/`, `.exec/`, `.jobs/`, `.services/` and — with snapshots on —
`.checkpoints/` into the folder it works in; where the root is a git checkout these go into
`.git/info/exclude` the first time an agent starts there.

`.checkpoints/` is what "revert to this turn" restores the files from, and it is kept inside bounds
rather than growing for ever: `[ops]` in `config.toml` carries `checkpoint_keep_days` (30),
`checkpoint_total_max_gb` (2.0, every store together) and `checkpoint_keep_last` (50 per session,
kept whatever the other two say). The maintenance tick prunes the oldest past either bound and packs
the stores; `python -m daedalus db checkpoints-prune` does it now and prints what it freed, and
`doctor` shows the stores against their bounds. A turn whose snapshot has been dropped is no longer
offered as an undo in the app, which says once that the older ones were removed.

The workspaces themselves are watched by the disk guard, set in `[disk]`. Every session home and
managed project folder under the workspaces root is measured (at most every `check_minutes`, an idle
small one every `idle_recheck_hours`); one past `workspace_soft_limit_gb` (20), and again at twice
that, has its agent asked to clean up and the operator told, once per crossing. Free space below
`min_free_gb` or `min_free_percent` tells the operator whoever filled it. Directories named like
throwaway (`throwaway_patterns`: `scratch*`, `_scratch*`, `tmp`, private `uv` and `npm` caches)
that nothing inside changed in for `cleanup_after_days` (7) are deleted and the agent is told at its
next turn; one a checkout tracks, or anything outside the managed root, is never touched. The
session's details and the project page show the largest folders and clean up the chosen ones.

In this Compose install the container sees only what is mounted into it, so a folder outside the stack
needs a bind mount before an agent can work in it. The app says which projects are not reachable; add
the mount to the agent service and to the terminals service, and restart:

```yaml
# deploy/compose.yaml → services.daedalus.volumes, and the same line under services.terminals.volumes
      - /home/<operator>/work/<folder>:/home/<operator>/work/<folder>
```

Mount it at **the same path inside the container as outside**, in both services: the project stores the
path the operator gave, and the same string has to name the folder to the agent and in a terminal. Then
`docker compose up -d daedalus terminals` and the project reports itself reachable. Recreating
`terminals` ends every container terminal, so tell the operator before you run it if any are open
(`GET /api/terminals` counts them).

This is the operator's decision too: do not add folders they did not ask for, and never mount their
whole home directory — the point of a project is that the boundary is a real one.

## 4. HTTPS (when there is a domain)

Put any reverse proxy with TLS in front of port 8765 and set `MINIAPP_PUBLIC_URL=https://<domain>` (that is
what marks the session cookie secure). The proxy must keep long connections open (the app streams events)
and pass WebSocket upgrades on `/ws/` (terminals). A terminal's socket is accepted only from the app's
own origin: the `Host` the proxy forwards, or exactly the origin of `MINIAPP_PUBLIC_URL` — which is also
what Telegram's webview sends, so with Telegram the setting is required. Caddy needs one line, and
forwards upgrades by itself:

```
<domain> {
    reverse_proxy 127.0.0.1:8765
}
```

nginx:

```
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
location /ws/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

Restart the stack after changing `.env`
(`docker compose … up -d`). With Telegram, register `https://<domain>/app` as the bot's menu button in
@BotFather.

## 5. Telegram (when wanted)

Two shapes; both keep every feature:

- **Private chat only** (default, no group): the operator writes to the bot; `/new <title>` creates
  sessions, `/sessions` lists them, `/use <n>` switches; the app shows all of them.
- **A group with topics**: create a private supergroup with topics, add the bot as administrator with
  *manage topics*, send `/bind` there; every session is then a topic.

## 6. Verify the whole thing works

1. After the operator has added a model (section 3a), create an agent in the app with the task "list
   the files in your workspace and tell me the date"; it must answer within a minute and the run must
   show tool steps. Before a model exists this step answers 409 with "No model is configured yet",
   which is the correct behaviour, not a fault.
2. Settings → Models shows the model they added, marked default; Usage shows the call you just made.
3. `docker logs deploy-daedalus-1 | grep -i error` shows nothing about providers.

## 7. Updating, and whether the agent may update itself

`[self_change] mode` in `config.toml` decides what the agent may do to its own code. Left at `auto`
— the default — it is worked out at startup from what the installation has:

| resolved | needs | what the agent gets |
|---|---|---|
| `server` | `GITHUB_TOKEN`, an `origin` on both checkouts the token may push to, and the `rebuilder` service (or the supervisor socket) | `SelfWorkspace`, `SelfPropose`, `SelfRebuild`, `SelfRollback`, the proposals API and the Changes screen |
| `local` | writable git checkouts of `daedalus` and `protocore-exp` | `SelfWorkspace` and `SelfApply`: it edits a worktree and commits into the checkout, and the change applies on a restart |
| `off` | — | nothing: no tools, no `/api/proposals`, no Changes screen, no self-development text in the prompt |

Set the mode explicitly to overrule the resolution — `mode = "off"` on an installation the operator
does not want changing itself. `daedalus doctor` (and `GET /api/capabilities`) names the mode, the
reasons behind it, and anything a mode you chose yourself is missing. A change takes effect on the
next restart.

In `local` mode — the desktop default — there is no pull request to approve: the agent commits into the
checkout the installation runs from, and the app shows *"Changes are ready — restart to apply"* with a
Restart button (`POST /api/self/restart`). The supervisor preflights that commit on a detached worktree of
itself before it stops anything, keeps the running version if the checks fail, and puts the last
known-good commit back on its own if the new one starts and dies three times inside ten minutes. Tell the
operator that restarting the app is how a change goes live, and that closing and reopening it does the
same. A change to `deploy/Dockerfile` or `deploy/apt-packages.txt` needs a new image, which a restart
cannot deliver: the app says so and the fix is `docker compose … up -d --build` (or
`daedalus-desktop update`). The virtualenv is on the `daedalus-venv` volume so it survives a replaced
container; it is re-synced only when `uv.lock` or `pyproject.toml` changed.

In `server` mode the agent updates itself through pull requests the operator approves; the supervisor
pulls `main`, preflights and restarts (rolling back on failure). To update by hand: `git -C
~/daedalus pull`, `git -C ~/protocore-exp pull`, then `docker compose … up -d --build`.

That last command also recreates the `terminals` service whenever the image changed, which ends every
container terminal. To update the agent and leave the terminals running, name the agent's service:
`docker compose … up -d --build daedalus`. The terminals' daemon is then updated separately and only
when the operator chooses: the app offers it (with the count of terminals it ends), or `docker compose
… up -d terminals`. The `browser` service is the same: a deploy never recreates it, and recreating it
(`docker compose … up -d --build browser`, or the app's update) closes every open browser, though the
profiles and their logins stay. An installation that predates the `terminals` service needs one `docker compose … up
-d --build` to create it — restarting the agent's container does not, and the image has to be rebuilt
because an older one carries no daemon. In `server`
mode never edit files inside the `daedalus` checkout on the server: the supervisor resets it to
`origin/main` on every rebuild.

An update that carries a schema change migrates the database in place on the next start; nothing is
asked of you. The one thing a migration cannot do is give back the space the old shape occupied — a
file made before the incremental auto-vacuum setting keeps its freelist. Once, after such an update,
on an installation whose database has grown:

```bash
docker exec deploy-daedalus-1 /srv/venv/bin/python -m daedalus db vacuum
```

It rewrites the file, hands the free pages back, and puts it on incremental auto-vacuum so the bot's
own maintenance pass can do it from then on. The bot may be running.

## 7a. What the doctor tells you

`docker exec deploy-daedalus-1 /srv/venv/bin/python -m daedalus doctor` is the one command that says
whether this installation is finished. The lines worth reading back to the operator:

| line | what it means |
|---|---|
| `default model` | fails with "no model is configured; nothing can run yet" until section 3a is done. This is the expected state of a fresh install, not a fault |
| `self-development` | the resolved mode and the reason for it; a mode set by hand that is missing a prerequisite is a warning naming what to provide |
| `browser tools` | whether this image carries a headless Chromium. "not installed in this image" is correct for `:latest` |
| `isolation` | on a server, nothing: the agent is in a container. It appears only on a native desktop installation, where it says there is no container boundary |
| `terminals (container)` | the terminal daemon answers, its version and how many terminals run. "not installed" on a stack started before the service existed: run `docker compose … up -d --build`. `terminals update (container)` means the image holds a newer daemon than the one running |
| `terminals (host)` | the host terminal (optional). "not installed" is information, not a fault: install it only if the operator asked for a shell on the server, with `bash deploy/host-terminal.sh install` run **as the operator, not with sudo**, after the stack is built. "permission denied" means Docker is rootless or uses userns-remap, where it cannot work |
| `browser (container)` | the browser daemon answers, its version and its Chromium. "not installed" when the `browser` profile is off, which is correct unless the operator asked for the agent's browser. `browser walls` says what a page is behind; `browser sandbox` appears only when Chromium's sandbox is off, which in a container means the service lost its `seccomp=unconfined` |
| `container image`, `image rebuild channel`, `published ports` | the container-only checks. On a native installation each says "not applicable (native)" rather than being left out |

## 8. What to report back

Tell the operator: the app address, how to log in (the pairing link, then a passkey), that the app will
ask them for a model before anything else and which providers are ready for one, the daily cap, and
whether Telegram is on. Do not paste tokens or keys into
the chat.
