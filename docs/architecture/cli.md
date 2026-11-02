# The terminal client: daedalus-cli and remote conversations

`daedalus-cli` (alias `dae`) is a terminal client for a Daedalus server. The agent loop, memory,
skills, MCP, the model keys and the transcript stay on the server; the tools of a conversation started
from the terminal run on the operator's machine, in the folder the client was started in. A
conversation started this way is of kind **`remote`** ("Remote" / «Удалённо» in the app): an ordinary
server session, live in the app at the same time, that the app can type into, answer and approve.

```
 the operator's machine                           the Daedalus server
 ┌───────────────────────────────┐   REST + SSE   ┌─────────────────────────────────────────┐
 │ daedalus-cli                  │ ─────────────► │ /api/sessions/*, /api/events  (the app's)│
 │   the screen (Bubble Tea)     │                │ SessionManager + the agent loop         │
 │                               │   WSS, out-    │                                         │
 │   the executor  ◄─────────────┼── bound only ──┤ /api/cli/relay → the device link        │
 │   exec.run, fs.* in $PWD      │                │   ExecBackend / DeviceFS for the session │
 │   local guard (Shift+Tab)     │                │   bound to (device, folder)             │
 └───────────────────────────────┘                └─────────────────────────────────────────┘
```

The client is two things over one credential. It is a **screen** over the same API the app uses:
REST, the session stream and the global event stream. And it is a **device**: it opens one outbound
WebSocket to the server, and the server sends the shell and file calls of the conversations bound to
it down that socket. The client never listens on a port.

This document is the contract between the server and the client. A change to either side that is not
a change here is a defect on the side that drifted.

## Names other parts depend on

| What | Value |
|---|---|
| Session kind | `remote` (`SESSION_KINDS`), fixed at creation |
| Live control mark | `remote` in `controlling` (`daedalus.host.control_marks`), while a connected client shows the session |
| Presence kind | `cli` on `POST /api/presence` and `GET /api/events?kind=cli` |
| Answer and grant origin | `via: "cli"` on `ask.answered` and `permission.resolved` |
| Credential | `Authorization: Device dcl_<43 url-safe characters>` |
| Contract version | `1`, answered as `contract` by `GET /api/cli/me` and in the relay's `device.welcome` |
| Pairing | `POST /api/cli/pair/start`, `POST /api/cli/pair/poll` (public) |
| Approval link | `<public app address>/app/?pair-device=<user code>` |
| Relay | `wss://<server>/api/cli/relay` |
| Guard levels | `ask`, `auto`, `plan` |
| Events | `device.pairing`, `device.changed`, `device.connection`, `remote.guard`, `remote.waiting` |
| Tables | `cli_devices`, `cli_pairings`, `cli_device_sessions` |

## Pairing a device

Modelled on the OAuth device authorization grant (RFC 8628) and on the Chrome extension's pairing,
with one difference from both: the approval happens inside the operator's own signed-in app or their
Telegram chat with the bot, so the two-factor gate the app already passed is the one that guards it.

1. `daedalus-cli login http://<host>:<port>` calls

   ```
   POST /api/cli/pair/start
   {"name": "laptop", "hostname": "laptop", "os": "linux", "arch": "amd64", "client_version": "0.1.0"}
   ```

   Every field is optional and clipped (name 80 characters, the rest 64); the name defaults to the
   host name. The answer:

   ```
   {"device_code": "<secret, 43 characters>", "user_code": "ABCD-EFGH",
    "verification_uri": "<base>/app/?pair-device",
    "verification_uri_complete": "<base>/app/?pair-device=ABCD-EFGH",
    "expires_in": 600, "interval": 3}
   ```

   `<base>` is the configured public address of the app (`MINIAPP_PUBLIC_URL`), or the address the
   request came in by when none is configured. The user code is eight characters of
   `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (no 0/O, 1/I/L), shown with a dash and accepted in any case,
   with or without it.

2. The client prints the code and a QR code of `verification_uri_complete` (half-block characters),
   and the server tells the operator: a `device.pairing` event with `state: "pending"` (the app shows
   an approval card wherever it is open), and in Telegram a message naming the device, its address and
   the code, with **Allow** and **Deny** buttons. Both ask the operator to compare the code with the one
   on their terminal: the code is what proves the request is theirs.

3. The operator approves:
   - in the app: the QR link opens the approval card for that code; the card also appears from the
     event, and Settings → Devices lists pending requests (`GET /api/cli/pairs`);
   - in Telegram: the **Allow** button.

4. The client polls every `interval` seconds:

   ```
   POST /api/cli/pair/poll   {"device_code": "…"}
   ```

   | Answer | Meaning |
   |---|---|
   | `200 {"status": "pending", "interval": 3}` | not decided yet |
   | `200 {"status": "slow_down", "interval": 6}` | polled faster than `interval`; wait the new interval |
   | `200 {"status": "approved", "token": "dcl_…", "device": Device}` | the token, **returned exactly once** |
   | `200 {"status": "denied"}` | the operator refused |
   | `200 {"status": "expired"}` | ten minutes passed, or the token was already handed out |
   | `404 {"status": "unknown"}` | no such request; counts as a failed attempt |
   | `429 {"status": "too_many"}` | too many failed attempts from this address or overall |

   The device row and its token are made at the poll that finds the request approved, not at the
   approval, so the plain token exists only in that one answer and nothing is stored that could
   still produce it.

5. The client keeps the token in the operating system's keychain (a `0600` file where there is
   none) together with the server address.

Limits: at most 5 requests may be pending at once (the sixth start answers 429), a request lives ten
minutes, failed polls count toward the same per-address and overall bounds the browser pairing uses,
and nothing presented at the public doors is logged.

The operator's routes (any operator credential; a device token is refused):

| Route | |
|---|---|
| `GET /api/cli/pairs` | `{"requests": [Pairing]}`: the pending requests, newest first |
| `GET /api/cli/pair/{user_code}` | one request, for the approval card; 404 when unknown or expired |
| `POST /api/cli/pair/{user_code}/approve` | `{"name"?: str}` renames the device; → `{"ok": true, "request": Pairing}`; 409 when already decided |
| `POST /api/cli/pair/{user_code}/deny` | → `{"ok": true, "request": Pairing}` |

`Pairing` is `{id, user_code, name, hostname, os, arch, client_version, address, created_at,
expires_at, state: "pending" | "approved" | "denied" | "delivered" | "expired"}`.

## Devices and tokens

A device is one paired client installation. Its token is `dcl_` plus 32 random bytes, url-safe;
the server keeps only its SHA-256 (`cli_devices.token_hash`). The token does not expire on a clock:
it stops working when the device is revoked, or after **90 days** in which it was never used.

| Route | Credential | |
|---|---|---|
| `GET /api/cli/devices` | operator | `{"devices": [Device]}`; `?all=1` includes revoked and lapsed ones |
| `PATCH /api/cli/devices/{id}` | operator | `{"name": str}` → `Device` |
| `DELETE /api/cli/devices/{id}` | operator | revoke: the token stops at once, the relay is closed with `4401`, the device's presence is dropped, its control marks go |
| `GET /api/cli/me` | device | `{"device": Device, "server": {"version", "contract": 1, "public_url"}, "sessions": [id]}` |
| `POST /api/cli/token/rotate` | device | `{"token": "dcl_…"}`; the old token stops at once |

`Device` is `{id, name, hostname, os, arch, client_version, paired_at, approved_via: "app" |
"telegram", last_seen_at, last_address, expires_at, revoked_at, connected, sessions}`; `connected` is
whether its relay is up now, `sessions` how many conversations it is bound to.

`last_seen_at` and `last_address` move with any authenticated request or relay frame, written at most
once a minute. Every change to the list is announced as `device.changed {device_id, change: "paired"
| "renamed" | "revoked"}`; the relay coming up or going down as `device.connection {device_id,
connected}`.

Telegram: `/devices` lists the paired devices with a **Revoke** button each.

### Authentication

Every request of the client carries `Authorization: Device <token>`. The relay's upgrade request
carries the same header. Nothing takes the token in a query string.

- A missing, unknown, revoked or lapsed token answers `401 {"detail": "…", "code":
  "device_token_invalid"}`; the relay closes with `4401`. The client then asks the operator to run
  `daedalus-cli login` again.
- A route a device may not use answers `403 {"detail": "…", "code": "device_route"}` (the relay:
  `4403`). A session outside the device's scope answers `404`, exactly as a session that does not
  exist, so a stolen token learns nothing about other conversations.
- The static API token (`x-daedalus-token`, `DAEDALUS_TOKEN`) still works for scripts, unscoped.
  The client accepts it with `--token` for development, never stores it, and cannot open the relay
  with it.

The device credential is checked by an ASGI door in front of the app (`daedalus.remote.door`), before
any route runs, the way the side panel's credential is; the operator's handlers then see the caller as
`{"via": "device", "device": <id>}`. The routes it opens are the matrix in
`tests/unit/test_remote_door.py`, the one place that says what a device token reaches.

### What a device token reaches

A device token is the operator at a terminal, for conversations. It is not the operator's whole
installation: it cannot read or change settings, secrets, providers, MCP servers, projects, memory,
other devices or the pairing itself.

Its **scope** is the set of sessions it may drive:

- the sessions it created (role `home` in `cli_device_sessions`: the session's hands are on this device);
- the sessions the operator opened from it with `/resume` (role `resumed`, recorded by
  `POST /api/cli/sessions/{id}/attach`);
- the subagents of either (a session whose `subagent_of` is in scope, to three levels).

`/resume` may attach any conversation the operator owns: the operator at the terminal is the operator.
What the scope buys is that every conversation a device drives was opened from it explicitly, is
recorded, is listed under the device in the app, and stops being reachable when the device is revoked
or the operator detaches it. A resumed conversation keeps its own hands: an ordinary chat keeps working
on the server; a remote conversation bound to another device keeps that device's hands until the
operator moves them (`POST /api/cli/sessions/{id}/hands`).

Routes open to a device token:

| Method | Path | |
|---|---|---|
| GET | `/api/cli/me`, `/api/cli/sessions` | |
| POST | `/api/cli/token/rotate` | |
| POST, DELETE | `/api/cli/sessions/{sid}/attach` | any session (attach); detach drops only `resumed` |
| POST | `/api/cli/sessions/{sid}/hands` | a `remote` session; in scope after attach |
| PUT | `/api/cli/sessions/{sid}/guard` | in scope |
| GET | `/api/models`, `/api/commands`, `/api/skills`, `/api/auth/me` | |
| POST | `/api/sessions` | `kind: "remote"` only |
| POST | `/api/presence` | sessions outside the scope are dropped from the report |
| GET | `/api/events?session=<sid>` | `session` is required and must be in scope |
| GET | `/api/sessions/{sid}`, `/stream`, `/events`, `/subagents`, `/tasks`, `/checkpoints`, `/steer`, `/suggestion`, `/search`, `/tool-results/{call}`, `/policy/similar/{key}`, `/export/download`, `/attachments/download`, `/tool-results/{call}/images/download`, `/sent/{call}/download`, `/media/{id}/download`, `/media/{id}/{item}/content` | in scope |
| POST | `/api/sessions/{sid}/messages`, `/upload`, `/stop`, `/answer`, `/model`, `/compact`, `/clear`, `/command`, `/revert`, `/retry`, `/fork`, `/btw`, `/policy/grant`, `/policy/grant-similar`, `/policy/refuse`, `/tasks/{task}/stop`, `/steer/{msg}` | in scope |
| DELETE | `/api/sessions/{sid}/steer/{msg}`, `/btw` | in scope |
| PATCH | `/api/sessions/{sid}` | in scope: rename, archive |
| WS | `/api/cli/relay` | |

Everything else, `DELETE /api/sessions/{sid}` included, answers 403.

## Presence

The client reports itself like a window of the app, with `kind: "cli"`:

```
POST /api/presence
{"client": "<per-process id>", "kind": "cli", "visible": true, "focused": <terminal has focus>,
 "sessions": ["<the session on screen>"], "lang": "en", "tz": "Europe/Berlin"}
```

every 20 seconds and whenever the session or the focus changes, and it opens its
`/api/events?session=<sid>&kind=cli&client=<same id>` stream with the same id, which ties the report to
the connection exactly as a browser tab's is. `focused` comes from the terminal's focus reports
(`CSI ? 1004 h`); a terminal that does not send them reports `focused: true` while visible.

What it changes on the server:

- a focused client showing a session counts as **attending** it, so push and desktop notifications of
  that session are held back, as for an app window in front of the operator;
- a client that reported a session within the last 60 seconds, or whose relay is up and bound to it,
  gives that session the live **`remote`** control mark (`session.controlling`), which the app draws
  as the Remote mark, hollow with a breathing dot on a session of another kind.

## Conversations

### Starting one

```
POST /api/sessions
{"kind": "remote", "cwd": "/home/you/project", "prompt": "…", "autotitle": true,
 "preset": "<optional preset id>", "guard": "auto"}
```

- With a device token the session is bound to that device (role `home`, with `cwd`). With an
  operator credential the body must also name `device_id` (an operator script or the app opening a
  conversation on a laptop); otherwise 400.
- `cwd` is the absolute folder on the device, as the device spells it (`/home/you/x`, `C:\Users\you\x`),
  at most 4096 characters, required for `remote`.
- A remote session names no project, folder or environment (400 if it does): its hands are the device.
  On the server it has a scratch folder like any chat without a project, which holds its attachments
  and checkpoints, not its work.
- A device token may create only `remote` sessions (403 otherwise).
- `guard` is the client's local guard at creation (see Local guards); optional, `auto` when left out.

The answer is the usual `{id, title, model, kind}` plus `remote` (below).

### What a session shows about its device

`GET /api/sessions/{id}` and every row of `GET /api/sessions` carry `remote` for a session that has a
home device, and `null` otherwise:

```
"remote": {"device_id": "…", "device_name": "laptop", "cwd": "/home/you/project",
           "connected": true, "guard": "auto", "waiting": null}
```

`waiting` is `null`, or `{"since": "<iso>", "calls": [{"tool_call_id", "tool"}]}` while the turn is
parked on this device (see Disconnects). The rows of the list carry `waiting` as a boolean.

### `/resume`

```
GET /api/cli/sessions?cwd=<folder>&q=<words>&limit=50
```

→ `{"sessions": [{id, title, kind, status, last_message_at, created_at, remote, attached, here}]}`,
newest first, archived ones left out. `attached` says the session is in this device's scope; `here`
that its home is this device and `cwd` (what `daedalus-cli -c` continues). `q` matches the title.

- `POST /api/cli/sessions/{id}/attach` → the session row; puts it in the device's scope (`resumed`).
  Attaching a session already in scope changes nothing.
- `DELETE /api/cli/sessions/{id}/attach` → `{"ok": true}`; drops a `resumed` session from the scope.
  A home session cannot be detached; 409.
- `POST /api/cli/sessions/{id}/hands {"cwd": "…"}` → the session row: makes this device and folder the
  home of a `remote` session (the operator continues it on another machine). 409 while a turn of it is
  running; 400 for a session of another kind, whose hands are the server's.

### The screen

Everything below is the app's own API; the client reads it the way `miniapp/src/sessionstream.ts`
does. A device token reaches it for sessions in scope.

| On screen | Route |
|---|---|
| Transcript, status, model, thinking, usage, context, pending AskUser, open approvals | `GET /api/sessions/{id}?tail=600` (`before=<seq>` pages back) |
| Live events | `GET /api/sessions/{id}/stream` (see below) |
| Approvals and questions answered elsewhere, status, the Remote mark, waiting | `GET /api/events?session={id}&kind=cli&client=…&types=permission.,ask.,session.,run.,remote.` |
| Send; Enter while running steers | `POST …/messages {"text", "client_message_id"}` (steer is the default while running; `follow_up: true` queues after the turn) |
| The queue; take a message back | `GET …/steer`; `POST …/steer/{msg}` sends it now; `DELETE …/steer/{msg}` withdraws it |
| Esc | `POST …/stop` |
| AskUser card | the pending question in the session view; `POST …/answer {"answers": [{"question", "selected": [labels], "custom"}]}` |
| Permission box | a refusal arrives as a `tool_result` carrying the approval key, and the key is in `open_approvals`: **Yes** → `POST …/policy/grant {"key"}`; **Yes, and don't ask again** → `GET …/policy/similar/{key}` for the label, then `POST …/policy/grant-similar {"key"}`; **No, and tell it what to do** → `POST …/policy/refuse {"key"}` then `POST …/messages` with the text |
| Pasted images, files | `POST …/upload` (multipart), then the message names them |
| `/model` | `GET /api/models`; `POST …/model {"preset"}` or `{"reasoning_effort"}`, `{"thinking"}` |
| `/compact`, `/clear` | `POST …/compact {"focus"?}`, `POST …/clear` |
| The `/` palette | `GET /api/commands` (server commands: `name, args, description, scope, confirm`) and `GET /api/skills`; run one with `POST …/command {"line"}` → `{"text"}` |
| `/rewind` (conversation) | `GET …/checkpoints`, `POST …/revert`, `POST …/fork`; the code side is the client's own snapshots |
| `/rename`, archive | `PATCH /api/sessions/{id} {"title"}` / `{"archived": true}` |
| `/export` | `GET …/export/download` |
| Full tool output (Ctrl+O) | `GET …/tool-results/{call_id}` |
| `/btw` | `POST …/btw`, `DELETE …/btw` |
| Subagents, background tasks | `GET …/subagents`, `GET …/tasks`, `POST …/tasks/{id}/stop` |

Answers and grants made through a device token are recorded with `via: "cli"`.

**The session stream** (`GET /api/sessions/{id}/stream`, `text/event-stream`):

- the first frame is `event: hello` with `{session_id, event_seq, history_revision, runtime_epoch}`;
  open the stream first, then read the transcript, so nothing falls between the two;
- then each event as `id: <event_seq>`, `event: <kind>`, `data: <envelope>` where the envelope is
  `{kind, event_seq, run_id, history_revision, payload}`; an envelope with `ephemeral` (token deltas)
  has no `id:` and is never replayed;
- `: keepalive` every 15 seconds; a client that hears nothing for 45 seconds reconnects;
- reconnect with `?after=<last id>` (or `Last-Event-ID`). `event: resync_required` with
  `{reason: cursor_trimmed | cursor_ahead | replay_gap | slow_client, watermark}` ends the stream: the
  client re-reads the session and reconnects from `watermark`. So does a drop in the middle of a
  message, whose deltas are not replayed.
- Event kinds are the core's (`message_start`, `content_block_delta` with `text_delta` and
  `thinking_delta`, `content_block_stop`, `tool_use_start`, `tool_use_stop` with `final_input`,
  `tool_result`, `tool_call_pending`, `state_changed`, `usage_committed`, `compaction_started`,
  `compaction_completed`, `subagent_spawn`, `subagent_progress`, `subagent_complete`,
  `queue_update`, `model_changed`, `error`, `run_settled`, …) and the host's `steer_changed`,
  `history_cut`, `setting_notice`, `compaction_background`. A kind the client does not know is
  skipped, never an error.

### Models

```
GET /api/models
{"default": "<preset id or empty>",
 "presets": [{"id", "label", "provider", "model", "thinking", "reasoning_effort", "images",
              "context_window", "max_output_tokens", "provider_kind", "billing"}],
 "providers": [{"id", "kind", "name", "billing"}],
 "efforts": ["low", "medium", "high", "xhigh"]}
```

No provider's address or key, as for the side panel. Presets are sorted by label.

## The device connection

The client opens `wss://<server>/api/cli/relay` with `Authorization: Device <token>`, keeps it open
while it runs, and reconnects with backoff (1 s doubling to 30 s, with jitter) when it drops. A socket
opened with an `http://` or `https://` `Origin` (a web page) is refused before anything is read. One
link per device: a second connection of the same device replaces the first, which is closed with
`4409`.

**Frames.** Text frames carry JSON-RPC 2.0 messages, exactly ptyd's control channel
(`docs/architecture/terminals.md`, "The control channel") without the length prefix, which the
WebSocket's own framing replaces. Binary frames are reserved for ptyd's framed channels (a terminal on
the device opened from the app's dock, later); a peer that does not use them ignores them. A message is
at most 1 MiB.

**Opening.** The client's first message is a notification:

```
{"jsonrpc": "2.0", "method": "device.hello", "params": {
   "protocol": 1, "client_version": "0.1.0", "os": "linux", "arch": "amd64", "shell": "/bin/zsh",
   "home": "/home/you",
   "capabilities": {"exec": true, "fs": ["stat", "list", "read", "write", "mkdir"], "pty": false},
   "sessions": [{"session_id": "…", "guard": "ask"}],
   "known": [{"id": "<request id>", "state": "running" | "done"}]}}
```

`sessions` are the sessions this process shows with their guards; `known` the requests it still holds
from before a drop (see Disconnects). The server answers with a notification:

```
{"jsonrpc": "2.0", "method": "device.welcome", "params": {
   "device_id": "…", "contract": 1, "server_version": "0.1.0",
   "bound": [{"session_id": "…", "cwd": "/home/you/project"}]}}
```

and from then on sends requests. Before `device.welcome` the client sends nothing else; a server that
gets anything but `device.hello` first closes with `4400`.

**Requests, server to device.** The methods and their parameters and results are ptyd's side
channels, with these differences:

| Method | Params → result | Differences from ptyd |
|---|---|---|
| `exec.run` | `{argv, cwd?, env{}, timeout_ms? ≤ 1 800 000, stdin_b64?, max_output? ≤ 4 MiB}` → `{exit_code, signal, stdout, stderr, truncated, timed_out, duration_ms}` | no program list: the guard and the jail decide; `argv` is `["bash", "-c", <prelude + command>]` on Unix and `["powershell", "-NoProfile", "-Command", …]` on Windows, as the server's backend writes it |
| `fs.stat` | `{path}` → `{exists, type, size, mtime, mode}` | |
| `fs.list` | `{path, glob?, sort?, limit? ≤ 5000}` → `{entries[{name, type, size, mtime}], truncated}` | |
| `fs.read` | `{path, offset?, max? ≤ 4 MiB}` → `{data_b64, offset, size, eof}` | |
| `fs.write` | `{path, offset, data_b64, final?}` → `{size, created}` | anywhere under the root, not only an inbox; offset 0 writes a temporary file beside the target, and the piece with `final: true` renames it over the target, so a dropped write never leaves a cut file |
| `fs.mkdir` | `{path}` → `{created}` | anywhere under the root |

Every request's `params` also carry `_meta`, which ptyd does not have:

```
"_meta": {"session_id": "…", "tool_call_id": "…", "tool": "Exec", "intent": "read" | "write" | "exec",
          "title": "npm test"}
```

`intent` is the server's account of what the call does (a file read through the shell is `read`), so
the guard can decide without parsing shell; `title` is what the client shows in its own prompt.

Request ids are strings, unique on the server and stable across a reconnect:
`"<session id>:<tool call id>:<n>"`.

**The jail.** Every path, every `cwd`, and every `exec.run` working directory must lie under the
folder the session is bound to, both as written and as the filesystem resolves it; the client refuses
anything else with `1004`. The client's deny list (keys, credentials, other tools' logins, as ptyd's)
applies inside the jail too. The jail is the client's and the server cannot widen it.

**Errors.** ptyd's codes: `1001` not found, `1003` limit, `1004` forbidden, `1005` timeout, `1007`
unsupported, and the JSON-RPC ones. A refusal by the guard is `1004` with
`data: {"reason": "refused_by_operator" | "plan_guard" | "outside_root" | "denied_path", "message"?: "<what the operator typed>"}`;
the server turns it into the tool's result for the agent (`The operator refused this in their terminal: …`).

**Notifications, device to server.**

- `device.guard {"session_id", "guard"}`: the operator moved the guard (Shift+Tab). The same as
  `PUT /api/cli/sessions/{id}/guard`; either may be used.
- `device.sessions {"sessions": [{"session_id", "guard"}]}`: the sessions this process shows changed.

**Keepalive.** The server pings every 20 seconds (the WebSocket's own ping); a client that sees no
frame for 60 seconds reconnects.

**Closing codes.** `4400` protocol error, `4401` token invalid or revoked (log in again), `4403` not
allowed, `4409` replaced by a newer connection of the same device, `1001` server going away
(reconnect).

## Disconnects: the turn pauses until the device is back

A turn of a `remote` session whose device is not connected **pauses**; it does not fail.

1. A tool call that needs the device while its relay is down is held on the server, not sent and not
   failed. The turn waits on it. The session announces `remote.waiting {"waiting": true, "device_id",
   "since", "calls": [{"tool_call_id", "tool"}]}`, and its `remote.waiting` field in the session view
   says the same. The app shows "Waiting for <device> to reconnect"; the session's status stays
   `running`.
2. A call that was already sent when the link dropped is handled by what the device reports on its
   next `device.hello`:
   - `known` says `done`: the server asks for the result again (`device.result {"id"}` → the stored
     result, which the client keeps for 15 minutes after a drop) and the turn continues with it;
   - `known` says `running`: the server waits for its result as if nothing had happened;
   - the device does not know it (the client was restarted, which ended its processes): the call is
     **not run again**. Its result for the agent says the terminal disconnected while running it and
     reported no outcome, so the agent checks the state before trying again.
3. When the device connects, the held calls are sent in order, `remote.waiting {"waiting": false}` is
   announced, and the turn goes on.
4. The operator can end the wait from anywhere with **Stop** (`POST …/stop`): the held calls end with
   "cancelled while the terminal was offline" and the turn stops as any stopped turn does.

There is no time limit on the wait. A session that waits is listed as such in the app and in
`/resume`, so it is not forgotten, and nothing on the server is held open meanwhile except the parked
run.

AskUser and approvals are not affected by a disconnect: they are held on the server and answered from
the app, Telegram or the client once it is back.

## Local guards (Shift+Tab)

The guard is the client's own rule about what the executor does without asking. It is not an agent
mode: the agent runs the same configuration whatever the guard, and the server's own policy and
approvals apply under every level.

| Level | The executor | Shown as |
|---|---|---|
| `ask` | asks the operator in the terminal before every `write` and `exec` request; `read` runs | `⏸ ask before acting` |
| `auto` | runs every request; the server's policy is the only gate | `⏵⏵ auto` |
| `plan` | refuses `write` and `exec` requests with `1004 plan_guard`, except `exec` the client recognises as read-only (`ls`, `cat`, `rg`, `grep`, `find`, `git status`, `git log`, `git diff`, `git show`) | `⏸ plan, read-only` |

Shift+Tab cycles `ask → auto → plan → ask`. A new client starts in `auto`, or in the level of
`--guard` or its configuration.

The client reports the level for each session it shows, at creation (`guard` on `POST /api/sessions`),
in `device.hello`, and on every change (`device.guard`, or `PUT /api/cli/sessions/{id}/guard
{"guard"}`). The server keeps it in memory, per device and session, and:

- announces it as `remote.guard {"guard", "device_id"}` and shows it in the session's `remote.guard`;
- for `plan`, tells the agent in its turn context that the operator's terminal refuses writes and
  commands that change things, so it plans instead of trying;
- treats a session's guard as unknown (`auto`) until its device reports one after a restart.

The guard of the home device is the one that matters for a session's tools; a client that only views
a resumed session reports its guard for display.

## Where each part lives

| Part | Where | State |
|---|---|---|
| Pairing, devices, tokens, the door, scope, presence, the kind, `/api/models`, `/api/cli/*` except the relay | `daedalus/remote/`, `daedalus/extensions/api_remote.py` | being built |
| The relay, the device link as an `ExecBackend` and `DeviceFS`, binding a session's hands, the pause | the relay's unit | to come |
| The client | `cli/` (Go) | to come |
| The Remote tab and mark, the approval card, Settings → Devices | `miniapp/` | to come |
