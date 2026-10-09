# Terminals: the daemon and its protocol

Terminals are real pseudo-terminals owned by `ptyd`, a small daemon written in Go (`ptyd/`). There
is one daemon per **environment**: `container` (a compose service beside the agent's container)
and `host` (the operator's machine). The Daedalus host talks to each over a local socket and relays
browser attachments to it. `ptyd` knows terminals, bytes, screens and processes; it knows nothing
about sessions, projects or agents.

This document is the contract both sides are tested against. A section marked *not yet* describes a
part of the protocol whose method names and shapes are fixed but which the daemon does not serve yet;
calling it returns `-32601 method not found`.

## Running it

```
ptyd serve --env container --run-dir /run/ptyd --state-dir /var/lib/ptyd
ptyd version
```

| Flag | Default and meaning |
|---|---|
| `--env` | the environment's name, echoed to clients (required) |
| `--run-dir` | the run directory: endpoint, token, socket (required) |
| `--state-dir` | the journal of agent writes, terminal logs, launch directories and dial sockets (required) |
| `--listen` | `unix` (a socket in the run directory), or `tcp:127.0.0.1:<port>`; on Windows `tcp:127.0.0.1:0` |
| `--home` | the home directory of spawned programs (`$HOME`) |
| `--shell` | the login shell: `$SHELL`, then the user's passwd entry, then `/bin/bash`, then `/bin/sh`; on Windows `pwsh.exe` when PATH finds it, else `powershell.exe` |
| `--hooks-listen` | loopback address of the hook listener (`127.0.0.1:0`); anything off the loopback interface is refused |
| `--config` | a JSON file: `{"limits": {"max_terminals", "ring_bytes", "input_idle_ms", "kill_grace_ms"}, "exec": {"allow": []}, "fs": {"roots": [], "deny": []}}`; the lists only add to the built-in ones; unknown keys are refused |
| `--log-file`, `--log-level` | the daemon's own JSON-lines log (stderr, `info`) |

`SIGTERM` or `SIGINT` stops the daemon: the endpoint file is removed first, every running terminal
gets `SIGHUP` and then, after the grace (2 s), `SIGKILL`, all at once, and the socket and token are
removed. `SIGHUP` is ignored. The daemon owns its terminals' processes: when it stops, they stop.
Restarting the Daedalus host is the case that matters, and it leaves the daemon and its terminals
running.

### In a compose install

The `container` environment is the `terminals` service of `deploy/compose.yaml`: the agent's own
image with `ptyd` as its entrypoint (`serve --env container --run-dir /run/daedalus-terminals
--state-dir /var/lib/ptyd --home /root`), root, `init: true`, on a network of its own. The run
directory is a volume both containers mount; the agent's container is told it as
`TERMINALS_CONTAINER_DIR`. The workspaces volume and every project folder are mounted into both at
the same paths. `/root` is the `terminals-home` volume, which the agent's container never mounts.
Servers started in a terminal listen in `TERMINALS_PORT_RANGE` (`8120-8139`), which the service
publishes.

The service is never recreated by a deploy or by the agent's restart, which is what lets terminals
outlive both. So the image can hold a newer daemon than the one running: the image's build stamps
`ptyd` with a digest of its sources (`src-<12 hex>`), the agent's container reads that version from
its own `/usr/local/bin/ptyd`, and `EnvStatus` reports `image_version` and `update_available` when
it differs from the running daemon's. Updating is the operator's: the route below asks the rebuilder
(`deploy/rebuild.sh`, the `selfdev` profile) to run `docker compose up -d --no-build --no-deps
terminals`, which ends every container terminal, and refuses with the count until the operator
confirms. Without a rebuilder the refusal names the command to run on the server.

### On Windows

The daemon runs on Windows as the host environment of a native install (below). What differs:

- **The terminal** is a pseudoconsole (ConPTY) instead of a PTY. The console host renders the
  program's console into VT sequences, and those are what the scanner and the emulator read. The
  program is created suspended, put in a job object that ends every process in it when it is
  closed or terminated, and only then resumed, so nothing it starts is born outside the job.
- **The endpoint** is loopback TCP (`tcp:127.0.0.1:<port>`): the host's asyncio has no unix-socket
  client on Windows. The port is sealed from the agent like the hook listener's. The run and state
  directories get an access list that gives this user, and nobody else, full control (inherited by
  the token and every file made inside); a lock held on `ptyd.lock` keeps a second daemon out, since
  there is no socket file to find alive.
- **The shell** is PowerShell 7 (`pwsh.exe`) when PATH finds it, else Windows PowerShell, with no
  login flag. Its integration is loaded as text run as a script block rather than dot-sourced as a
  file, because the default execution policy refuses every script file; the user's policy is not
  changed. Older console hosts drop OSC sequences they do not know, and then the marks never
  arrive: the shell works, and `terminal.commands` answers 1007.
- **Signals:** `INT` is a Ctrl+C typed into the console (the daemon turns the processing of Ctrl+C
  back on before its first program: the launcher starts it in a process group of its own, which
  ignores Ctrl+C, and every program would inherit that); `TERM`, `HUP`, `QUIT` and `KILL` terminate
  the job (with `group`) or the program; the rest answer 1007. `terminal.kill` closes the console
  first, which is the hangup (attached programs get `CTRL_CLOSE_EVENT`), and terminates the job
  after the grace. There is no foreground process group, so `busy` comes only from the shell's marks.
- **Programs** are found by Windows rules: `PATH` split on `;`, each of `PATHEXT` tried (a CLI
  installed by npm is a `.cmd`), the current directory not searched. Variable names are one name
  whatever their case. `exec.run` holds its program in a job too, and allows it by its name without
  the extension (`claude.cmd` is `claude`); fs paths are compared with the roots and the deny list
  in lower case.
- **The hook command** is `<state>\bin\hook-post.exe`, a hard link to the daemon (a copy where a
  link cannot be made): a symbolic link needs a privilege an ordinary account lacks.
- The sandbox is `not available on Windows`, and process statistics report `supported: false`.

### On a server: the host terminal

The `host` environment of a compose install is `ptyd` on the server itself, as the operator: a
systemd user unit (`deploy/ptyd/daedalus-ptyd.service`, installed by `deploy/host-terminal.sh`, which
`deploy/setup.sh` offers) running `serve --env host --run-dir <dir> --state-dir
~/.local/state/daedalus-ptyd`, with the image's own binary copied to `~/.local/lib/daedalus/ptyd` so
the two daemons speak one protocol. The run directory is `../daedalus-host-terminals` beside the
checkout (`DAEDALUS_HOST_TERMINALS_DIR`), and compose mounts it into the agent's container at
`/run/daedalus-host-terminals` (`TERMINALS_HOST_DIR`) whether or not the unit is installed: an empty
directory reads as `not_installed`, and the socket appears in a directory already mounted. setup
creates it as the operator, `0700`, because Docker would create a missing source as root. The unit
keeps the default `KillMode=control-group`, so stopping it ends every host terminal, and the
installer turns on lingering so it outlives the operator's login. The host reports why it cannot
use the daemon: `not_installed` (empty directory; its detail says when root owns it), `not_running`
(a lock or token without an endpoint, or an endpoint nothing answers at), `permission_denied` (root
in the container is not root on the host: rootless Docker or userns-remap), `protocol_mismatch`.

## Run directory and handshake

```
<run>/endpoint   "unix:ptyd.sock" or "tcp:127.0.0.1:<port>"
<run>/token      64 hex characters (32 random bytes), mode 0600, new at every start
<run>/ptyd.sock  mode 0600
<run>/ptyd.lock  held (flock) for the daemon's lifetime
```

- The directory is created with mode 0700. The token is written before the endpoint, and the
  endpoint only once the socket accepts, so an endpoint that exists always leads to a ready daemon.
  A client is told only the directory. A relative `unix:` endpoint is resolved against it, so a
  directory bind-mounted at another path works the same.
- **One daemon per directory.** A second one fails to take the lock, or connects to the existing
  socket, and exits with `another ptyd holds the run directory`. A socket left by a daemon that died
  is removed.
- **Handshake.** The first frame, on channel 0, carries the token exactly as the file holds it
  (trailing whitespace ignored). A good token is answered with a notification
  `{"jsonrpc":"2.0","method":"hello","params":{"version","protocol","instance","env"}}`. A wrong
  one waits one second and closes. `instance` is new at every start of the daemon. The client
  refuses a `protocol` it does not know and reports the environment unavailable.

## Framing

```
frame   := u32be length | u32be channel | payload     (length = 4 + len(payload), at most 1 MiB + 4)
channel 0     : one UTF-8 JSON-RPC 2.0 message per frame
channel n > 0 : an attachment (payload = one browser frame) or a byte stream
empty payload : closed from that side; the other side answers with its own empty frame
```

Channel ids are allocated by the daemon, increase, and are never reused within a connection.
Requests run concurrently; at most 256 may be in flight per connection. Errors use the JSON-RPC
codes plus:

| Code | Name | Meaning |
|---|---|---|
| 1001 | `not_found` | no such terminal |
| 1002 | `exited` | the terminal has exited |
| 1003 | `limit` | a limit was reached (terminals, requests, reply size, write size) |
| 1004 | `forbidden` | not in this state: the id exists, the terminal still runs |
| 1005 | `timeout` | |
| 1006 | `keyboard_held` | a human holds the keyboard and an agent write timed out waiting |
| 1007 | `unsupported` | not in this build or on this platform |
| 1008 | `stale_launch` | the launch is not registered, or it has ended |
| 1009 | `invalid_size` | outside 20×4 … 500×300 |

Parameters are decoded strictly: an unknown field is `-32602`.

## Methods

| Method | Params → result | |
|---|---|---|
| `daemon.info` | → `{version, protocol, instance, env, os, arch, pid, started_at, uptime_s, home, shell, capabilities{sandbox, shells[], shell_integration[], emulator, stats}, hooks{listen, launches, held}, side_channels{exec_allow[], fs_roots[], state_dir}, limits{…}, counts{running, exited}, machine}` | |
| `terminal.create` | see below → `{id, pid, cwd, cwd_fallback, shell, shell_integration, created_at}` | |
| `terminal.list` | `{ids?, preview_rows? 0..12}` → `{terminals:[Info]}` | see The screen |
| `terminal.get` | `{id}` → `Info` | |
| `terminal.write` | see below → `{bytes, seq_before, queued_ms, delivered_at}` | |
| `terminal.resize` | `{id, cols, rows, px_w?, px_h?}` → `{cols, rows}`; the host becomes the size owner | |
| `terminal.signal` | `{id, signal, group? = true}` | |
| `terminal.kill` | `{id, grace_ms? ≤ 30000}` → `{exit_code, signal}` | |
| `terminal.forget` | `{id}`, exited terminals only | |
| `terminal.read_output` | `{id, since_seq, max_bytes? ≤ 1 MiB, strip?}` → `{from_seq, to_seq, head_seq, gap, data \| data_b64}` | |
| `terminal.stats` | `{ids?}` → `{at, supported, terminals:[{id, pid, processes, rss_bytes, cpu_percent, daemon_bytes}], daemon{pid, rss_bytes, cpu_percent}, machine}` | |
| `events.subscribe` | `{after_seq}` → `{instance, from_seq, resync}`, then `event` notifications | |
| `events.unsubscribe` | | |
| `terminal.attach` | `{id, client{kind? = "human" \| "viewer", label?, via?, read_only?}}` → `{channel, client_id}` | see Attachments |
| `terminal.detach` | `{channel}`; the daemon closes the channel | |
| `terminal.keyboard` | `{id, owner: "auto" \| "human" \| "agent", ttl_ms? ≤ 86 400 000}` → `{owner, until?}` | |
| `terminal.snapshot` | `{id, scrollback? ≤ 10000 = 2000}` → `{cols, rows, seq, first_abs_row, data_b64}` | see The screen |
| `terminal.read_screen` | `{id, format? "text" \| "runs" \| "vt", scrollback? ≤ 10000, tail_rows?}` → `{cols, rows, cursor{x, y, visible, abs_row}, alt_screen, title, cwd, seq, first_abs_row, lines[] \| runs[][] \| data_b64, truncated?}` | |
| `terminal.wait_for` | `{id, regex?, scope? "screen" \| "output", since_seq?, idle_ms?, command_done?, timeout_ms}` → `{matched: "regex" \| "idle" \| "command_done" \| "exited" \| "timeout", seq, match?, command?}` | see Shell integration |
| `terminal.commands` | `{id, last? 1..500 = 20, with_output?, output_max? ≤ 256 KiB = 64 KiB}` → `{commands:[Command], busy, shell_integration}` | see Shell integration |
| `exec.run`, `fs.*`, `net.dial`, `net.allow`, `hooks.*` | side channels for CLI adapters: see below | |

### `terminal.create`

```
{id, argv[] | shell{program?, login? = true, integration? = true}, cwd?, env{}, strip_env[], cols?, rows?, title?,
 ring_bytes?, log_to_disk?, input_idle_ms?, sandbox?, launch_id?, labels{}}
```

- `id` is the host's: 1–64 of `A-Z a-z 0-9 - _`. An id in use (running or exited, not forgotten) is
  `1004`.
- `argv` is run exactly, never typed into a shell. Without it the login shell runs, as `<shell> -l`,
  or, for bash, zsh, fish and PowerShell, launched with its integration (see Shell integration)
  unless `integration` is false; the reply's `shell_integration` names the one loaded, or is `""`.
  `argv[0]` is resolved against the `PATH` of the program's own environment.
- `cwd` must be absolute. A missing directory falls back to the home directory and the reply says
  `cwd_fallback: true`.
- Size defaults to 80×24; outside 20×4 … 500×300 it is `1009`.
- `labels` (at most 32, each at most 256 bytes) are stored and echoed, never interpreted.
- `log_to_disk` also writes the output to `<state>/terminals/<id>.log` (rotated at 32 MiB).
- `sandbox {writable[]}` runs the program in the sandbox (below); `1007` where it is not available,
  with the reason `daemon.info.capabilities.sandbox` gives.
- Beyond `max_terminals` running terminals (128) it is `1003`.

### The sandbox

`terminal.create {sandbox: {writable: [...]}}` wraps the program in bubblewrap, which the daemon
probes and runs itself, because it has to happen where the process lives. The wall is against
writing, not reading:

```
bwrap --ro-bind / / --dev /dev --dev-bind /dev/pts /dev/pts --proc /proc --tmpfs /tmp
      --unshare-pid --die-with-parent  [--bind <w> <w>]...  --tmpfs <run> --tmpfs <state>
      --ro-bind <state>/shell <state>/shell  [launch paths bound back]  --chdir <cwd> -- <program>
```

- Everything is read-only except the `writable` folders and a private `/tmp`. The daemon's run
  directory (the token) and state directory are empty inside, masked after the writable folders so
  a writable folder that holds one cannot show it again.
- A writable folder that is not an absolute path, is `/`, lies in `/proc`, `/dev` or `/sys` or in the
  daemon's own directories, is a symbolic link, is missing or is not a directory is left read-only.
  The program still starts, and the reply says which were left and why:
  `sandbox {writable[], skipped[{path, reason}]}`.
- There is no `--new-session`: the shell keeps its controlling terminal and with it job control
  (`sleep 100 &`, `fg`, Ctrl+C). The outer `/dev/pts` is bound in, so `tty`, `ssh` and `sudo` find the
  terminal where it is. `TIOCSTI` in a PTY the daemon owns can only type into that same PTY.
- `HISTFILE` is `/tmp/.shell_history` unless the caller sets it: home is read-only inside.
- What a program needs from the masked state directory is bound back: the shell-integration scripts
  (`<state>/shell`) read-only, so a sandboxed shell keeps its command marks; and for a terminal started
  with a launch, its overlay directory and the hook command (and the daemon binary it links to)
  read-only, its dial directory writable.
- A working directory inside the daemon's own directories is refused (`-32602`) rather than shown as
  an empty one; one in `/tmp` that no writable folder covers is shown read-only.
- `daemon.info.capabilities.sandbox` is `ok`, or why not: `bwrap is not installed`, `bwrap cannot
  create namespaces here: …`, `not available on <os>`. The probe is `bwrap --ro-bind / / --dev /dev
  --proc /proc --unshare-pid true`, the agent's own `Exec` probe; a success is kept, a failure is
  believed for five minutes, so namespaces allowed later are noticed without a restart.
- `Info.sandbox` is true and `Info.argv` is the program's, not bubblewrap's; `pid` is bubblewrap's.
  `busy` compares the foreground group with the program's own group inside, not bubblewrap's.
- In the compose install the `terminals` service carries `cap_add: [SYS_ADMIN]` with unconfined
  seccomp and AppArmor for this; on a machine, unprivileged user namespaces must be allowed.

The host sends as `writable` what the owner's agent may write: a session's own sandbox set when the
session lives in this environment, else the owner's project folders there that are not read-only,
else the working directory alone. `POST /api/terminals/{id}/restart {sandbox}` starts the same
program with the sandbox switched on or off.

### `Info`

```
{id, pid, argv, cwd, title, status: "running"|"exited", exit_code, exit_signal, created_at,
 exited_at, last_output_at, last_input_at, last_human_input_at, cols, rows, size_owner, clients[],
 last_detach_at, keyboard{owner, until}, modes{alt_screen, bracketed_paste, mouse, app_cursor,
 mouse_mode?, kitty_flags?}, busy, last_command{command, exit_code, at}, labels, launch_id, sandbox,
 shell, shell_integration?, output_seq, preview?}
```

`busy` means a command is running: once the shell has printed a mark with its nonce, between a
command's start and end marks; before that, and for a program that prints none, a job other than the
terminal's own program holding the foreground. `clients` are `{id, kind, label, via?, read_only, attached_at}`; `size_owner` is
`"host"`, `"human:<client id>"`, or null once the owning client left and no other offered a size. `cwd` and `title` follow OSC 7 and OSC 0/2. `output_seq` is one past the last
output byte.

### Writes

`terminal.write {id, text? | paste? | keys[]? | bytes_b64?, origin{kind: "agent", actor, launch_id?,
note?}, wait? = "keyboard" | "none", timeout_ms? = 30000 (≤ 600000)}` — exactly one payload, at most
1 MiB.

- `text` and `bytes_b64` are written as they are.
- `paste` is wrapped in `ESC[200~ … ESC[201~` when the application turned bracketed paste on, with
  every marker inside the text removed so a paste cannot end itself early. Without bracketed paste,
  line feeds become carriage returns.
- `keys` are named: `Enter Tab S-Tab Esc Backspace Up Down Left Right Home End PgUp PgDn Delete
  Insert F1…F12 C-<c> M-<key>`. Arrows, Home and End follow the application's cursor-key mode.
- **Arbitration.** A human keystroke goes straight through and takes the keyboard. An agent write
  with `wait: "keyboard"` waits until the human has been idle for `input_idle_ms` (10 s) and no human
  holds the keyboard; a grant to agents lets it through at once, and human typing ends that grant.
  A write that times out is `1006` when a human holds the keyboard, else `1005`. Once a write has
  started it is finished without waiting again, in 4 KiB chunks, so a keystroke goes in between
  chunks but never splits the agent's text into two deliveries.
- The receipt means the bytes were written to the PTY. It never means the program read them.
- Every delivered agent write is appended to `<state>/agent-writes.jsonl` (rotated 16 MiB × 3): time,
  terminal, actor, launch, kind, byte count, the first 4 KiB and the SHA-256 of the whole. Human
  keystrokes are never recorded anywhere.

### Output

Every terminal keeps a ring of its output (8 MiB, 1–64 MiB per terminal), addressed by absolute byte
offset since the terminal started. `read_output` returns bytes from `since_seq`; `gap: true` means
older bytes had already left the ring and the read starts at the oldest byte held. Raw output comes
as `data_b64` (at most 512 KiB per call); with `strip: true` it comes as text in `data` (at most
768 KiB per call) with escape sequences removed, CR LF and lone CR as LF, and the read ending on a
character boundary. `to_seq` is where the next read starts. When a terminal exits, its ring shrinks to
its last 64 KiB.

### Signals and ending

- `terminal.signal` accepts `INT TERM HUP KILL QUIT TSTP CONT WINCH USR1 USR2`. With `group` (the
  default) it goes to the PTY's foreground process group, as a typed Ctrl+C would; otherwise to the
  program alone.
- `terminal.kill` sends `SIGHUP` to the program's group and the foreground group, waits up to the
  grace for the program to exit, then sends `SIGKILL` to the group and to every process of the
  terminal it can find: on Linux and macOS, the process tree, the session, and every process whose
  environment carries the terminal's `DAEDALUS_TERMINAL_ID`. That catches a child that called
  `setsid` after its parent exited. Windows is described above.
- A program starts with the default action for `SIGHUP` and `SIGINT` even when the daemon was
  started with them ignored (in the background of a script, under `nohup`): a shell hands an
  ignored signal on to every command, and Ctrl+C would otherwise interrupt nothing.
- A program that exits while a background job still holds the PTY open is reported exited after
  half a second of draining; closing the PTY hangs the job up, as closing a terminal window would.
- Exited terminals are kept, with their screens, for an hour, then forgotten.

### Process statistics

`terminal.stats` and a `terminal.stats` event every 10 s (only while a terminal runs) report, per
terminal, the processes of its tree, its session and every process whose environment carries its
`DAEDALUS_TERMINAL_ID` (a daemon that forked twice and called `setsid` has left both of the others),
their summed resident memory, and their CPU use since the previous sample as a percentage of one CPU.
Shared pages are counted once per process, so the memory is an estimate of cost, not an accounting.
`daemon_bytes` is what the terminal holds inside the daemon that none of its processes shows: its
output ring as allocated now. `daemon` is the daemon's own process, whose memory holds every
terminal's emulator; a load estimate adds it, shared out over the terminals. `machine` is
`{mem_total_bytes, mem_available_bytes, cgroup_limit_bytes?, cgroup_used_bytes?, cpus, cgroup_cpus?,
cpu_percent, load1, load5, load15}`, read from `/proc` and, inside a container, from its cgroup's
memory and CPU limits. On macOS the processes come from the kernel's process table (`kern.proc`,
`proc_info` and `kern.procargs2` for the environment) and the machine from `hw.memsize` and the
`vm` page counts (free, speculative and file cache as available); the machine's `cpu_percent` is 0
there, since macOS keeps it only behind an interface a build without cgo cannot reach. On Windows
`supported` is false and the numbers are zero.

## Events

An `event` notification carries `{seq, at, type, terminal_id?, data}`. Every event takes its `seq`
from one counter, so a subscriber sees one total order across terminals and kinds — a hook post and
a terminal's output included. The daemon keeps the last 20 000, and no more than 64 MiB of them by
size.

- `events.subscribe {after_seq}` delivers every event after `after_seq`. `resync: true` means some
  were lost: they left the log, or `after_seq` came from an earlier life of the daemon (compare
  `instance`). Delivery then starts at the oldest event held, `from_seq`.
- A subscriber that falls behind by more than the log receives `events.resync {from_seq}` and
  continues from there.

| Type | `data` |
|---|---|
| `terminal.created` | `{pid, argv, cwd, labels, launch_id}` |
| `terminal.exited` | `{exit_code, signal, seq}` — published after every event of the terminal's output |
| `terminal.title` | `{title, seq}` — at most one per 250 ms per terminal, the latest wins |
| `terminal.cwd` | `{cwd, seq}` |
| `terminal.bell` | `{seq}` — at most one per 2 s per terminal |
| `terminal.notify` | `{title, body, seq}` — OSC 9 and OSC 777;notify, at most one per 5 s |
| `terminal.progress` | `{state, value, seq}` — OSC 9;4, at most one per 500 ms, the latest wins |
| `terminal.command` | `{phase: "end", n, exit_code, command, cwd, abs_row, end_row, started_at, duration_ms, seq}` — a command ended (see Shell integration); `abs_row` is its first output row, `seq` the offset after its end mark |
| `terminal.mode` | `{alt_screen, bracketed_paste, mouse, app_cursor, mouse_mode?, kitty_flags?}` |
| `terminal.stats` | a `terminal.stats` result |
| `hook` | `{launch_id, terminal_id, name, body, size, truncated?, reply_id?, hold_ms?}` — a hook post of a launch; tagged with the launch's terminal |
| `launch.ended` | `{launch_id, reason}` — `unregistered`, `terminal_exited`, `expired` or `shutdown` |

`seq` inside `data` is the output offset at which the mark occurred. An event too large for one
frame is delivered as its envelope with `data: {"too_large": true}` rather than ending the
subscription.

## The screen

Every answer about the screen is taken at one output offset, `seq`: the screen shows every byte
before it and none after. Rows have absolute numbers counted from the terminal's start, so a row
keeps its number while older ones leave the history (`first_abs_row` is the number of the first row
returned). The numbering is exact between resizes; a resize reflows the history and can shift it.

- `terminal.snapshot` returns VT bytes that rebuild the screen, its history (`scrollback` lines of
  it), both screens, the cursor, the pen, the modes, the scroll region, the tab stops, the character
  sets, the links and the colours the program set, when written into a freshly reset terminal of the
  same size. The stream continues at `seq`. A snapshot that would not fit in one frame (700 KiB) is
  taken again with less history.
- `terminal.read_screen` returns the visible screen and `scrollback` lines above it (only the last
  `tail_rows`, when given). `text`: one string per line, soft-wrapped rows joined, trailing spaces
  and trailing blank lines dropped; when the text would not fit in a frame the oldest lines go and
  `truncated` is true. `runs`: per row, `[{t, fg?, bg?, b?, i?, u?, d?, inv?}]` (a colour is 1–256 for
  palette entries 0–255, `0x1000000 + RGB` for true colour, absent for the default), at most 1000
  rows. `vt`: a snapshot. Under the alternate screen there is no history.
- `terminal.wait_for` ends at the first condition met: `regex` (RE2) found in the visible screen's
  text, re-evaluated at most every 50 ms and only after new output, or with `scope: "output"` in the
  output since `since_seq` (default: the call), escape sequences removed; `idle_ms` of no output,
  counted from the call at the earliest; the program's exit; or `timeout_ms` (at most 30 minutes).
- `terminal.list {preview_rows}` fills `preview` with that many rows as runs: under a shell the rows
  ending at the cursor, on the alternate screen its bottom rows.

## Terminal queries

A program asks its terminal questions and waits for the answer on its input. The daemon answers all
of them, with nobody attached as with three, at the exact point of the stream where they were asked
and ahead of any queued input; every browser swallows them. The answers are the ones xterm.js gives,
since xterm.js draws the terminal and encodes the keys and the mouse, so a program is never told
about a feature the browser lacks:

| Query | Answer |
|---|---|
| DA1 `CSI c`, DA2 `CSI > c` | `CSI ? 1 ; 2 c`, `CSI > 0 ; 276 ; 0 c` |
| XTVERSION `CSI > q` | `DCS > \| xterm.js(<the app's xterm.js version>) ST` |
| DSR `CSI 5 n`, CPR `CSI 6 n`, `CSI ? 6 n` | `CSI 0 n`; the cursor, from the top left whatever DECOM says |
| `CSI ? 996 n` | `CSI ? 997 ; 1 n` when the viewer's background is darker than its foreground, else `; 2` |
| DECRQM, both forms | the modes xterm.js knows, with its values (`0` for the rest), plus 2027 (grapheme clustering, on) |
| kitty `CSI ? u` | the current flags |
| DECRQSS `m`, `r`, `SP q`, `" q`, `" p` | the pen, the margins, the cursor shape, `0`, `61 ; 1` |
| OSC 4/10/11/12 when every slot is `?` | the colour, `rgb:rrrr/gggg/bbbb`, one reply per slot: the one the program set, else the size owner's theme (from ATTACH), else the app's dark palette |
| XTWINOPS 14, 15, 16, 18, 19 | the text area in pixels and cells, once a browser has measured it (18 and 19 always) |

Other `CSI n` requests and XTWINOPS reports get no answer; the title reports (20, 21) never do,
because echoing a title a program wrote back as input is a way to type into a shell. Where xterm.js
reports an option rather than the state (the cursor shape, cursor blink, the pen) or a column past
the margin while a wrap is pending, the daemon reports the state and the last column. At most 64 KiB
of answers wait for a program that does not read its input; beyond that they are dropped.

## What every consumer of the output may rely on

The output is scanned before anything else sees it, and the same clamped bytes go to the ring, to
the daemon's emulator and to every browser. Several terminal emulators allocate or loop in proportion
to a parameter, or buffer an unterminated string without limit, so:

- every numeric CSI parameter is at most 10 000, and a CSI carries at most 32 parameters and
  sub-parameters;
- an OSC, DCS, APC, PM or SOS payload longer than 64 KiB is dropped whole, and a title (OSC 0/1/2) is
  cut to 4 KiB at a character boundary;
- a sequence interrupted by another is never passed on half-finished: an unfinished CSI or ESC is
  dropped, and an interrupted string is closed with ST;
- a lone UTF-8 lead byte `0xC2` becomes U+FFFD, so dropping a sequence can never join it with a later
  byte into a C1 control;
- terminal sizes are 20×4 … 500×300;
- the queue to the emulator is bounded, so a program that writes faster than its terminal is emulated
  blocks on its own writes instead of growing the daemon.

A sequence split across reads is held until complete; the result does not depend on how the output
was chunked. Per terminal, the daemon's memory is bounded by the ring, 64 KiB of pending sequence,
the emulator's queue and the emulator's history: about 10 000 lines at the terminal's width, at most
64 MiB.

## The emulator

Each terminal has a headless emulator, fed from the PTY whether or not anyone is attached, behind
one Go interface (`ptyd/internal/emulator`): `Feed`, `Resize`, `Size`, `Cursor`, `Modes`, `History`,
`Snapshot`, `Text`, `Runs`, `Close`. One goroutine per terminal owns it; feeds and questions are
served in order, so every answer corresponds to an exact output offset. Emulators start with
grapheme clustering (mode 2027) on, which is how the browser's width provider measures text.

The emulator is libghostty-vt, Ghostty's terminal core, linked statically into the daemon and built
from pinned sources (`ptyd/libghostty`). It was chosen by measurement against xterm.js headless,
alacritty_terminal and charmbracelet/x/vt:

- It was the only one to survive every unclamped hostile stream (32 of them, up to 20 MB each) with
  no crash and no hang, at most 92 MB. Behind the daemon's clamps the same corpus peaks at 38 MB
  above the daemon's own memory.
- Its snapshots, replayed into a fresh xterm.js, reproduce the live xterm.js screen at 162 of 192
  checkpoints of recorded programs and synthetic cases, and at 59 of the 68 checkpoints of real
  programs (vim, less, htop, top, git, rich, Textual, bash, and five coding agents), with 3 cells
  wrong. The differences left are policies (below).
- It parses 75–186 MB/s on an idle machine and holds a 200×50 terminal with 10 000 lines of history
  in about 15 MB. A snapshot of 2000 lines of history takes about 7 ms, of 10 000 about 30 ms.

The daemon's snapshot layer puts right what the library's formatter leaves out or gets wrong: rows
the formatter trims, the primary screen under an alternate one, the cursor's shape, the title,
mouse modes a program already turned off, colours a program set, and the cursor position under
origin mode. A patch carried against the pinned commit fixes three formatter defects (rows of only
background colour, blanks after a styled cell, links).

The width of every character is Ghostty's with grapheme clustering (mode 2027) on, and the app's
xterm.js measures with a width provider generated from the same tables; the two are held together by
recorded grids both sides test against. Where the library and xterm.js differ by policy, the library
is kept: lines scrolled out of a scroll region that starts at the top go into the history (as in
xterm; Codex keeps its conversation this way), the cursor's line reflows on resize, invalid UTF-8
becomes U+FFFD, left and right margins (DECSLRM) exist, and a soft reset keeps bracketed paste.

A build without cgo has no screen emulator: it tracks only the size and the modes, answers only the
queries that need no screen, and `daemon.info` names it `basic@1` instead of `libghostty-vt@…`.

## Browser frames

The host relays these unchanged between a WebSocket and an attachment channel. Big-endian; the first
byte is the type.

| Direction | Frame | Layout |
|---|---|---|
| to the client | `0x01 OUTPUT` | `[u64 seq][bytes]`, `seq` the offset of the first byte |
| to the client | `0x02 SNAPSHOT` | `[u16 cols][u16 rows][u64 seq][vt bytes]`; the stream continues at `seq` |
| to the client | `0x03 EVENT` | a JSON object with a string `type` |
| to the daemon | `0x10 INPUT` | `[bytes]`, at most 32 KiB |
| to the daemon | `0x11 RESIZE` | `[u16 cols][u16 rows][u16 px_w][u16 px_h]` |
| to the daemon | `0x12 ACK` | `[u64 seq]`, the offset the client has parsed |
| to the daemon | `0x13 ATTACH` | `{lastSeq, haveState, readOnly, scrollback?, theme?{fg, bg, cursor}}` |

Offsets are at most 2^53 − 1, the largest integer a browser holds exactly. The golden frames are in
`ptyd/internal/wire/testdata/frames.json` and `miniapp/src/terminal/testdata/frames.json`, which are
byte-identical (a host test checks it); each codec is tested against its copy.

## Attachments

`terminal.attach` opens a channel that carries browser frames both ways; the host relays them to a
WebSocket unchanged. The daemon sends nothing on it until the client's first ATTACH. `read_only` (or
`kind: "viewer"`, or `readOnly` in ATTACH, whichever says so) makes the client a watcher: its INPUT
and RESIZE are dropped. A terminal takes at most 32 clients (`1003` past that). An exited terminal can
still be attached, to see its last screen. `terminal.detach`, the host closing the channel, the
connection ending and the terminal being forgotten all end the client; the channel's closing frame is
always its last.

**On ATTACH** the client receives `hello`, then `clients`, then either the bytes it is missing or a
fresh screen. `hello` is `{client_id, read_only, ack_bytes, window_bytes, terminal{id, title, cwd,
status, cols, rows}, size{cols, rows, owner}, keyboard{owner, until}, modes{alt_screen, mouse,
bracketed_paste, app_cursor}}`; `clients` is `{count, others[]}`, sent again whenever someone attaches
or leaves.

- OUTPUT from `lastSeq` when `haveState` is set, `lastSeq` is still in the ring, the PTY was not
  resized after `lastSeq`, and at most 1 MiB is missing. Bytes drawn for another size would be
  garbage on the client's screen, and replaying more than a megabyte costs more than one screen.
- Otherwise `resync {reason, first_abs_row}` and a SNAPSHOT at the emulator's size, with
  `min(scrollback, 10000)` lines of history (2000 by default), and OUTPUT from the snapshot's `seq`.
  `reason` is `attach` (no state), `ring` (`lastSeq` left the ring, or is ahead of it), `resized` or
  `backlog`. A screen that does not fit in one frame is taken again with half the history; if even the
  bare screen does not fit, an empty one is sent at the right offset, followed by
  `error {code: "snapshot_too_large"}`.
- The client resets its terminal before writing a snapshot, so modes of the old screen cannot survive.
- For an exited terminal, then `exit {code, signal}` once every byte is sent. The channel stays open.

A second ATTACH on the same channel starts over the same way.

**Flow control.** OUTPUT goes out when 64 KiB are waiting or 8 ms after the previous frame, so a
keystroke's echo after a quiet spell is sent at once and a flood in large frames. The daemon stops
sending once 256 KiB are unacknowledged (`window_bytes`); the client acknowledges every 64 KiB it has
*parsed* (`ack_bytes`), and at least every 20 s. An acknowledgement past what was sent counts as
everything sent. When an acknowledgement reopens a full window and what the client missed has left the
ring or is more than 1 MiB, the client gets `resync` (`lagged`) and a snapshot instead of the backlog;
the window starts again from the snapshot. The PTY reader never waits for a client: a client that
stops acknowledging holds its window and nothing else. `ping {at}` (milliseconds) comes every 20 s.

**Size.** Each client's RESIZE is the size it would like. The client that last resized or typed owns
the PTY's size; the others render at it. A RESIZE, or INPUT from a client with a size of its own
while another owns the size, applies that client's size before the keystroke is written. When the
owner leaves, the most recently active client with a size takes over; with none, the PTY keeps its
size and the owner is nobody. `terminal.resize` from the host makes the host the owner until a client
claims. A size outside 20×4 … 500×300 is refused with `error {code: "invalid_size"}` and not
remembered. A read-only client never owns the size. Every change of the grid or the owner is sent as
`size {cols, rows, owner}`, with `owner` `you`, `other` or `host` as the receiving client sees it.

**The keyboard.** `auto` is the default: an agent write waits for `input_idle_ms` of human quiet.
`human` holds agents back until the grant's expiry (or until changed); `agent` lets their writes
through at once, and human typing ends it. Clients receive `keyboard {owner, until}` (`until` in
milliseconds, or null) at every change and when a grant expires, and `agent_typing {actor, active}`
when an agent's writes start being delivered and after 1.5 s without one.

**Terminal queries.** The daemon is their only answerer; clients swallow them. An INPUT frame that
repeats byte for byte an answer the daemon gave to a query that client was shown, within 2 s, is
taken for the client answering too and dropped once; the same bytes later are typing and pass.

**Other events**, as the terminal produces them: `title`, `cwd`, `bell`, `notify {title, body}`,
`progress {state, value}`, `mode {alt_screen, mouse, bracketed_paste, app_cursor}`, and the command
marks of Shell integration: `command {phase: "prompt", abs_row, seq, at}` where a prompt starts,
`command {phase: "start", n, command, abs_row, prompt_row, seq, at}` and `command {phase: "end", n,
exit_code, command, abs_row, prompt_row, end_row, duration_ms, seq, at}`. `seq` is the output offset
just after the mark: events go out ahead of output still waiting to be sent, so a client holds a
`command` until it has drawn the bytes up to `seq`. After every SNAPSHOT of a terminal whose shell
reports commands comes `marks {list[{n, command, exit_code, prompt_row, output_row, end_row, running}],
first_abs_row, prompt_row}`: every command whose rows reach the snapshot's first row, as of the
snapshot's own offset, so a client places its marks again. A `command` event queued before the
snapshot may still arrive after `marks`; `n` identifies a command, so applying one twice is harmless. A client that
is behind receives only the latest of each kind, except `notify`, of which at most 64 wait. A frame the
daemon cannot use is answered with `error {code: "bad_frame"}`.

## Shell integration

A shell launched by the daemon marks its prompts and commands in its output, and the daemon turns the
marks into records: what ran, where, where its prompt and output are, and how it ended.

**The scripts** are compiled into the daemon and written to `<state>/shell/` at every start
(directories 0755, files 0644), because the daemon runs where no checkout is. They are loaded by the
launch alone; the user's own startup files are never edited, and each script reads them first, as
the shell would have:

| Shell | Launch | Reads |
|---|---|---|
| bash | `bash --init-file <state>/shell/bash/init.sh -i`, with `DAEDALUS_SHELL_LOGIN=1` for a login shell | `/etc/profile` and the first of `~/.bash_profile`, `~/.bash_login`, `~/.profile` (else `~/.bashrc`) for a login shell, since a login bash never reads `--init-file`; `~/.bashrc` otherwise |
| zsh | `zsh -l` with `ZDOTDIR=<state>/shell/zsh`, and `DAEDALUS_USER_ZDOTDIR` when the environment had a `ZDOTDIR` | `.zshenv`, `.zprofile` and `.zshrc` from the user's `ZDOTDIR` (or home), each sourced at the top level with `ZDOTDIR` as the user's files expect it; after `.zshrc`, `ZDOTDIR` is handed back for good, so zsh reads the user's own `.zlogin` and a nested zsh starts plainly |
| fish | `fish --login --init-command "source <state>/shell/fish/init.fish"` | the user's configuration, as always: the init command runs after it |
| PowerShell | `pwsh [-Login] -NoLogo -NoExit -Command "try { . ([scriptblock]::Create([IO.File]::ReadAllText('<state>/shell/pwsh/init.ps1'))) } catch { }"` (no `-Login` on Windows) | the user's profile, as always. Read as text so no execution policy stops it. Tested with pwsh on Linux; not yet run by a daemon on Windows, where it is meant for |

**The marks.** `OSC 133 ; A` where the prompt starts and `B` where it ends, `C` when a command starts,
`D ; <exit status>` when it ends, `OSC 633 ; E ; <command line>` just before `C` (a backslash doubled,
`;` and control characters as `\xNN`), and `OSC 7` with the directory before every prompt. What the
user set up is extended, never replaced: bash's `PROMPT_COMMAND` gains an entry before and after its
own (array or string), `PS0` keeps its text (bash 4.4 and later; older bash chains the user's `DEBUG`
trap instead), and with bash-preexec loaded its hook arrays are used; zsh gains hooks in
`precmd_functions` and `preexec_functions`; fish gains event handlers, and its `fish_prompt` is kept
under another name and called first. `B` is appended to the prompt after the user's prompt code ran,
so a framework that rebuilds the prompt at every prompt keeps it; a theme that rebuilds it later
loses `B`, which costs nothing but that row. bash takes the command line from its history, checked
against the number the prompt expected: a line kept out of the history (a leading space under
`ignorespace`, or history switched off) is reported without its text.

**The nonce.** Every mark carries `k=<nonce>`, 16 random bytes in hex, new for every terminal and
passed in `DAEDALUS_SI_NONCE`, which the scripts take out of their environment at once (fish after the
user's configuration ran). A shell mark without it — output replayed from a recorded session, a nested
shell over ssh, a program imitating a shell — is ignored. Every terminal gets a nonce, so a program
that is not a shell may report commands itself by printing marks with it. The nonce is not a secret
from the terminal's own user: a process of the same user can read it from the shell's initial
environment, and a log of this terminal's own output replayed into it carries it.

**Records.** `C` starts a command: its number `n`, the command line from the `E` before it, the
directory, the prompt row of the `A` before it, and its first output row (the cursor's absolute row at
`C`). `D` ends it with its status (none when `D` carried none), its end row (the cursor's row, one
further when the output left the cursor mid-line) and its duration. A `D` with no command open (the
first prompt, an empty line) is ignored; an `A` or a second `C` while a command is open ends it with
no status. A command line is kept to 4 KiB and the newest 500 commands per terminal.

`Command` in `terminal.commands` is `{n, command, cwd, exit_code, started_at, finished_at,
duration_ms, prompt_row, output_row, end_row, start_seq, end_seq, output?, output_truncated?}`,
oldest first; a running command has no `finished_at`, `end_row` or `end_seq`. Rows are absolute
(`first_abs_row` in The screen) and approximate across a resize. `with_output` adds each command's
output as the screen shows it, while its rows are still in the history: the end is kept past
`output_max`, and `output_truncated` says that some is gone. The whole answer is kept within one frame
by leaving out the oldest commands. A terminal whose shell loaded no integration and printed no mark
with its nonce answers `1007`.

`terminal.wait_for {command_done: true}` ends on the first command whose end mark comes after
`since_seq`, or after the output head at the call; passing the `seq_before` of the write that typed
the command closes the race with a command that ends before the wait begins. It answers
`{matched: "command_done", seq: <end_seq>, command: Command}`, and `1007` where commands are unknown.
`terminal.command` events go out at every end, and `daemon.info.capabilities.shell_integration`
lists the shells integrated with.

**In the app** (`miniapp/src/terminal/marks.ts`). Each command gets a dot beside its prompt, in the
colour of how it ended (running, zero, non-zero, no status), and a session dock's tab takes the last
command's colour. Ctrl+↑ and Ctrl+↓ scroll from prompt to prompt, outside the alternate screen and
only in a terminal whose shell has marked something; anywhere else the keys reach the program. "Copy
last command output" (the dock's toolbar, the full-screen header, and the terminal's own menu on a
right click or a long press) copies the rows between the last finished command's start and end marks,
and asks `GET /api/terminals/{id}/commands?last=5&output=1` when those rows have left the browser's
history. Absolute rows are tied to xterm.js's lines by a marker whose row is known: line 0 after
every snapshot (`first_abs_row`), moved down with the output so that trimming the history cannot take
it. A reattach that continues from a tail brings no `marks`, so after one the app asks the host for
the recent commands.

## The environment of a spawned program

The daemon's environment, minus `DAEDALUS_PTYD_*`, `DAEDALUS_TERMINAL_ID`, `DAEDALUS_LAUNCH_*`,
`DAEDALUS_HOOK_*`, `DAEDALUS_DIAL_DIR`, `DAEDALUS_SI_*`, `DAEDALUS_SHELL_LOGIN`,
`DAEDALUS_USER_ZDOTDIR`, `TERM_PROGRAM*`, `VSCODE_*`, `TMUX*`, `STY`, `WINDOW`,
`KITTY_*`, `ITERM_*`, `WT_SESSION`, `CLAUDE*` and the caller's `strip_env` patterns (a name, or a
prefix ending in `*`); plus `TERM=xterm-256color`, `COLORTERM=truecolor`, `CLAUDE_CODE_NO_FLICKER=1`,
`CLAUDE_CODE_SCROLL_SPEED=3`, `DAEDALUS_TERMINAL_ID=<id>`, `DAEDALUS_SI_NONCE=<nonce>` and `HOME`;
plus the caller's `env` on top; and last, outside Windows, `PWD` set to the working directory, so a
shell shows the path it was asked for rather than what its links resolve to (on macOS `/tmp` is
`/private/tmp`).
When the effective character type (the first of `LC_ALL`, `LC_CTYPE`, `LANG` that is set) is not
UTF-8, the non-UTF-8 overrides are removed and `LANG=C.UTF-8` is set; a user's `ru_RU.UTF-8` is
kept.

## Side channels

What a CLI adapter reaches besides a terminal. Every one is fenced by a list, all over the same
authenticated socket.

### `exec.run`

`{argv, cwd?, env{}, timeout_ms? ≤ 1 800 000, stdin_b64?, max_output? ≤ 4 MiB}` →
`{exit_code, signal, stdout, stderr, truncated, timed_out, duration_ms, path}`

- Not a terminal: the program runs in its own process group, with the environment a terminal gets
  (without `DAEDALUS_TERMINAL_ID`), and the whole group gets `SIGHUP` and then, after 2 s, `SIGKILL`
  when the time runs out or the caller's connection closes.
- `argv[0]`, resolved through the program's `PATH`, must have a basename on the list: `claude codex
  opencode pi grok npm npx node git uname`, plus the configuration's `exec.allow`. `1004` otherwise.
  This guards against mistakes, not against a hostile host: the token already starts any terminal.
- stdout and stderr are captured up to `max_output` each (default 1 MiB), the rest read and
  dropped, as text (invalid UTF-8 replaced). One reply is one frame, so together they are cut to
  about 640 KiB; `truncated` says so. A program that fails is a result, not an error. At most 16
  run at once (`1003`).

### `fs.*`

A path must be absolute. It is allowed when, both as written (cleaned) and as the filesystem
resolves it, it lies under a root and matches no deny pattern; the file actually opened is checked
again by the path the kernel reports for it, and its last component is never a followed symlink.
FIFOs and devices are refused.

- **Roots** are the host's list (`fs.set_roots`) plus the configuration's `fs.roots`. `fs.set_roots
  {roots[]}` → `{roots, accepted, refused[{root, reason}]}` replaces the host's list; a root that is
  `/`, holds the home directory, or is the daemon's own is refused.
- **Deny** patterns (`**` any directories, `*` part of a name) are compiled in and only extended by
  the configuration's `fs.deny`: every CLI's login (`**/.claude/.credentials.json`,
  `**/.codex/auth.json`, `**/.grok/auth.json`, `**/.local/share/opencode/auth.json`,
  `**/.pi/agent/auth.json`), `**/.ssh/**`, `**/.gnupg/**`, `**/.config/gh/hosts.yml`, `**/.netrc`,
  `**/.git-credentials`, `**/.docker/config.json`, `**/.aws/**`, `**/.kube/**`,
  `**/.config/gcloud/**`, `**/.azure/**`, `**/.npmrc`, `**/.pypirc`, and the daemon's run and state
  directories. A refusal is `1004` and is written to the daemon's log.

| Method | Params → result |
|---|---|
| `fs.stat` | `{path, as_root?}` → `{exists, type, size, mtime, mode, file_id}`; a missing path under a root is `{exists: false}`. With `as_root`, the path need not be under a root: it is held to the rules a root is (not `/`, not holding the home directory, nothing denied or the daemon's own) and the answer adds `writable` |
| `fs.mkdir` | `{path}` → `fs.stat {as_root}`'s answer plus `created`: makes a folder that is to become a root (a project folder), with its parents, under the same rules; an existing folder is left as it is |
| `fs.write` | `{path, offset, data_b64}` → `{size, created}`: a file handed to a staff member. Only inside an inbox — below the last `.agents` of the path, `inbox/<…>/<name>` — under a root, by the read rules (resolved, not denied, not the daemon's own); the directories are made and checked again, the file is opened without following a link. Offset 0 creates and refuses an existing file; a later offset continues one that holds exactly that many bytes. At most 512 KiB a call and 50 MiB a file. With `fs.mkdir`, the side channels' only writes |
| `fs.list` | `{path, glob?, sort? "name"\|"mtime", limit? ≤ 5000}` → `{entries[{name, type, size, mtime}], truncated}`; denied entries are left out, symlinks listed as such |
| `fs.browse` | `{path, hidden?, limit? ≤ 2000}` → `{path, parent, home, writable, is_git, entries[{name, path, mtime, writable, readable, is_git, link}], truncated, places[{name, path, kind "home"\|"volume"}]}`: the folders of one directory, for the folder picker of a new project. `path` empty or `~…` is the home directory. See below for why it is not held to the roots |
| `fs.read` | `{path, offset?, max? ≤ 4 MiB}` → `{data_b64, offset, size, eof, file_id}` |
| `fs.tail` | `{path, from_offset, max?, follow_ms? ≤ 60 000, file_id?}` → `{data_b64, next_offset, size, rotated, file_id}` |

One reply carries at most 640 KiB of a file; a longer read continues at its offset. `fs.tail`
waits up to `follow_ms` for bytes past `from_offset` (at most 128 tails wait at once). `rotated`
means the file at the path is another one than `file_id` names, or it shrank; its data then starts
at the beginning of the file.

`fs.browse` is the one read that is not held to the roots, because a folder being chosen for a new
project is under none yet: the roots are the folders of the projects that already exist, so the
roots rule would leave the picker nothing to show. It is kept safe by what it reads instead. It
lists directories only — a name, the modification time, whether this user may write and list it,
whether it holds a `.git` — and never lists a file or opens one. The directory asked for, as written
and as resolved, may be neither denied nor the daemon's own, and the directory actually opened is
checked again by the path the kernel reports, so a link swapped in between is refused (`1004`, in
the log). An entry that is denied or the daemon's own, as written or as resolved, is left out as if
absent; a symbolic link is listed (`link: true`) only when it resolves to a directory that passes
the same rules; dot-folders are left out unless `hidden`. It reads at most 100 000 directory
entries, returns at most `limit` (500 by default), and stops the per-folder stats after 1.5 s, so a
slow network mount answers with what it has; each cut says `truncated`. It adds no write: the picker
makes a folder with `fs.mkdir` and checks the chosen one with `fs.stat {as_root}`, which hold the path
to the rules a project folder is held to. A daemon older than the call answers "method not found",
which the host turns into "update the host terminal daemon" instead of a broken picker.

### Launches and hooks

A **launch** is how a program in a terminal speaks back without typing on a screen.

`hooks.register_launch {launch_id?, terminal_id?, files{name: base64}, ports[], hold_max_ms? ≤ 600 000,
ttl_s?}` → `{launch_id, hook_url, hook_token, dir, dial_dir, env{}, files[]}`

- `launch_id` is the host's, like a terminal id; without one the daemon picks it.
- `files` (at most 32 of 512 KiB) are written 0600 into `dir`, `<state>/launches/<launch_id>/`
  (0700): a CLI's settings overlay, its MCP entry, its system prompt. A name is one plain part, or up
  to six joined by `/` for a file a CLI finds only at a fixed place under a directory it is given
  (Claude Code's skills at `.claude/skills/<name>/SKILL.md`); the directories are made 0700 inside
  the new launch directory, and `.`, `..` or an empty part is refused.
- `hooks.put_file {launch_id, name, data: base64, replace?}` → `{path}` adds a file to an open launch
  later — a message too long to type, which the CLI is told to read by its path. The name is one plain
  part, the file must be new (never over something there, never through a link), at most 1024 per
  launch. With `replace` the content is written to a fresh hidden file and renamed over the name: an
  operator's secret handed again with a new value reaches a running member that way, whole, and a
  link planted under the name is replaced rather than followed.
- `ports` are the loopback ports `net.dial` may reach for it; `net.allow {launch_id, port}` adds one.
- A terminal created with `launch_id` gets the launch's environment on top of the caller's:
  `DAEDALUS_LAUNCH_ID`, `DAEDALUS_HOOK_URL` (`http://127.0.0.1:<port>/hook/<launch_id>`),
  `DAEDALUS_HOOK_TOKEN`, `DAEDALUS_HOOK_CMD` (`<state>/bin/hook-post`, one path, a link to the
  daemon), `DAEDALUS_PTYD_BIN` (the daemon's executable), `DAEDALUS_DIAL_DIR` (`dial_dir`,
  `<state>/dial/<launch_id>`) and `DAEDALUS_LAUNCH_DIR` (`dir`). `env` in the reply is the same set.
  A launch that is not registered is `1008`.
- It ends 30 s after the last of its terminals exits, after `ttl_s` (default 600) if no terminal was
  started with it, or at `hooks.unregister_launch {launch_id}` → `{removed}`. Its directories are
  removed, its streams closed, its waiting posts answered 410, and `launch.ended` is published.

The **hook listener** takes `POST /hook/<launch_id>/<name>[?wait_ms=N]` with `Authorization: Bearer
<hook_token>` (`name`: 1–64 of `A-Z a-z 0-9 . - _`; a body of at most 1 MiB; at most 100 posts a
second per launch):

| Answer | When |
|---|---|
| 204 | the `hook` event was published (and a held post got no reply in time) |
| 200 + body | a held post, answered by `hooks.reply` |
| 401 | a wrong token |
| 410 | no such launch, or it ended; nothing is published |
| 413 | a body over 1 MiB |
| 429 | over the rate, or too many posts waiting (32 per launch, 256 in all) |

A post is **held** with `wait_ms` (or `hold_ms`) in the query, or a `daedalus_hold_ms` field in a JSON
object body, at most the launch's `hold_max_ms`. Its event carries `reply_id`, and
`hooks.reply {reply_id, launch_id?, status? = 200, body?, content_type?}` → `{delivered}` answers it:
a JSON string body is sent as text, anything else as JSON. A reply for a post that stopped waiting,
or whose `launch_id` differs, is `1001`. The event's `body` is the post's JSON, or its text; one over
256 KiB has its long strings shortened (every key kept) and `truncated: true`.

`ptyd hook-post <name> [--wait-ms N]` (also `$DAEDALUS_HOOK_CMD <name>`) posts its stdin with the
URL and token from its environment and prints a successful reply's body. It exits 0 on 2xx, 2 on
401 or 410, and 1 on anything else, including no listener.

`ptyd hook <source> [--wait-ms N]` is the same post for a CLI's **command hook**, and it always exits
0: a wrong token, an ended launch, no listener or a bad argument are said on stderr, and stdout stays
empty. Claude Code reads exit 2 from a command hook as a refusal and feeds stderr to the model, so a
failing bridge would otherwise stop the work it only observes. It gives up ten seconds past its hold.

### The team tools: `ptyd team-mcp`

A Model Context Protocol server on stdio (one JSON object per line) that a CLI starts from its
per-launch MCP entry, `{"command": "$DAEDALUS_PTYD_BIN", "args": ["team-mcp"]}`, under the server
name `daedalus_team`. It answers `initialize` (echoing a revision it knows — 2024-11-05, 2025-03-26,
2025-06-18, 2025-11-25 — and the newest for any other, logged on stderr), `ping`, `tools/list`,
`tools/call` and `notifications/cancelled`; calls run concurrently (at most 16), and a cancelled
call is not answered and releases its held post. Its two tools carry the names and arguments a
Daedalus staff member has:

- `Report {kind: checkpoint|needs_input|stuck|done, note, artifacts?[], remember?}`
- `AskOrchestrator {question, options?[], context?}`

Arguments are checked before anything is posted; a wrong one is a tool result marked as an error.
The call is posted to the launch's hook listener as `team` with the body `{"tool": "report"|"ask",
"call_id", …the arguments}` (optional strings left out when empty, lists always present; `call_id` is
`<launch>:<process>:<n>`, on which the host dedupes a post it sees twice), held for the host's
reply: `DAEDALUS_REPORT_HOLD_MS` (default 15 s) for a report, `DAEDALUS_ASK_HOLD_MS` (default 5 min)
for a question, both capped by the launch's `hold_max_ms`. The host answers with `hooks.reply`:

| Reply body | Tool result |
|---|---|
| `{"text": "…"}` | the text |
| `{"text": "…", "error": true}` | the text, marked as an error (a report refused: "commit first") |
| a JSON string, or plain text | that text |
| none in time (204) | a report: `recorded` (it was published when posted); a question: "Pending: nobody has answered yet. The answer will arrive as a message; …" — the host delivers a later answer as the worker's next message |

On `initialize` and on `tools/list` the server also posts, unheld and without waiting, `{"tool":
"hello", "stage": "initialize"|"tools/list", "client"?}`: the host's proof that the CLI loaded the
team tools at all.

401 or 410 tells the worker its session is no longer connected to its team. When the launch
environment names `DAEDALUS_TEAM_URL` and `DAEDALUS_TEAM_TOKEN`, the calls go instead to
`<DAEDALUS_TEAM_URL>/report|ask` with `X-Daedalus-Team-Token`, the host's own team route; that
reaches the host only where the CLI can reach its port, which a CLI in the `terminals` container
cannot.

The server inherits its environment from the CLI. A CLI that gives its MCP servers a filtered
environment (Codex) must be told to pass `DAEDALUS_HOOK_URL`, `DAEDALUS_HOOK_TOKEN` and the two
holds through.

### Other tool sets: `ptyd tools-mcp --set <name>`

`team-mcp` is the `team` set of `ptyd tools-mcp`, whose wire is unchanged. Any other set is read
from the launch: `$DAEDALUS_LAUNCH_DIR/tools/<name>.json`, a launch file the host writes, so the host
changes its tools without a new daemon:

```
{"server": "daedalus_browser", "instructions": "…", "hold_ms": 330000, "unanswered": "…",
 "tools": [{"name", "description", "inputSchema"}, …]}
```

- The server names itself `server` (default `daedalus_<name>`) and lists the tools as they are
  written; a tool without a name or a schema is left out. A set whose file cannot be read still
  serves, with no tools, and every call says why; the reason is on stderr. `<name>` is 1–32 of
  `a-z 0-9 _ -`, starting with a letter.
- A call is checked only as far as its arguments being an object with the schema's `required` names
  present; the host checks the rest. It is posted to the hook listener as `tools` with the body
  `{"set", "tool", "arguments", "call_id"}`, held for `DAEDALUS_TOOLS_HOLD_MS`, else the file's
  `hold_ms`, else 330 s — above the host's five-minute hold on a question to the operator, which a
  call may be waiting on — and capped by the listener. The replies are read as for the team; silence
  (204) is **an error**, `unanswered` (default: the action may or may not have happened; look before
  trying again), never a success.
- On `initialize` and `tools/list` it posts, unheld, `{"set", "tool": "hello", "stage", "client"?}`
  to `tools`. `DAEDALUS_TEAM_URL` is never used for a set other than `team`.

The browser is such a set (`browser`, server `daedalus_browser`): see `browser.md`, The host side.

### `net.dial`

`{target, launch_id}` → `{channel}`: a byte stream on its own channel. `unix:<name>` is a socket in
the launch's dial directory; `tcp:127.0.0.1:<port>` only a port registered for a live launch
(natively the loopback interface also holds the Daedalus API and the key proxy). An empty frame
from either side closes it. The daemon holds at most 1 MiB the socket has not read before it closes
the stream; the host bounds its side the same way. At most 16 streams per launch and 256 in all.

## The host side

The host (`daedalus/terminals/`) keeps a row per terminal in the `terminals` table and an append-only
`terminal_audit`. It is told each environment's run directory (`TERMINALS_CONTAINER_DIR`,
`TERMINALS_HOST_DIR`), connects in the background and retries until the daemon is there. Both
directories are sealed from the agent — named in a command, even inside a container, it is refused —
because the token in them is a shell.

- **Ids and labels.** The host makes the id (12 hex characters) and writes the row before
  `terminal.create`, so the row is the reservation under the cap. `labels` carry `owner_kind`,
  `owner_id`, `project_id` and `profile`.
- **Reconcile**, on every connection and every minute: a running row the daemon does not list is
  `lost` when the daemon's `instance` changed and `exited` when it did not (ended and forgotten
  while the host was away); a running terminal with no row is adopted from its labels; a terminal
  whose owner no longer exists is ended. Events are resumed from the last `seq` seen when the
  instance is the same.
- **Owners** are `session`, `staff`, `project` or `free`. Deleting a session or a project ends its
  terminals. Ended rows are kept for `terminals.exited_retention_hours` with their last screen.
- **The cap.** At most `terminals.running_cap` terminals run at once across both environments. An
  agent's launch waits in line for a place; the operator's is refused with `409 {"code":
  "over_cap"}` and admitted past the cap when repeated with `confirm: true`.
- **The audit** records create, kill, restart, signal, keyboard, update and remove for every
  terminal, every agent write with its first 4 KiB and the SHA-256 of the whole, and each browser's
  attach and detach of a host terminal. What a person types is never recorded, only how much.
- **Commands.** A row's `shell_integration` says whether its shell was started with its integration,
  and the view carries it. Each `terminal.command` from the daemon is written to the row as its last
  command (`{command, exit_code, at, duration_ms}`), so the list shows it after the daemon forgot the
  terminal. `Terminals.commands()` and `GET /api/terminals/{id}/commands` ask the daemon for the
  records.
- **Load.** The daemons' `terminal.stats` events feed a rolling average cost per profile; `GET
  /api/terminals/load?cap=N` reports what runs now and the machine with the cap filled. "Used now"
  (`used.rss_bytes`) is the terminals' process trees plus each daemon's own memory
  (`used.daemon_rss_bytes`), because the daemon holds every terminal's emulator and output ring and
  no terminal process shows that memory. The cap is set in Settings → Terminals, whose bar redoes the
  projection in the page as the value is typed and warns, without refusing, past what the estimate
  carries.
- **The event bus.** Every terminal event on the bus carries `terminal_id`, `project_id`, and
  `session_id` or `staff_id` from the owner. The host publishes `terminal.created` and
  `terminal.exited` itself (`lost: true` when the daemon went with it), and retells the daemon's
  (`daedalus/terminals/bus.py`) in the bus registry's shapes:

  | Bus type | From the daemon's | Payload |
  |---|---|---|
  | `terminal.title` | `terminal.title`, only when the title changed | `{title}`, at most 500 characters |
  | `terminal.cwd` | `terminal.cwd`, only when the directory changed | `{cwd}` |
  | `terminal.command` | `terminal.command` | `{exit_code, command?, mark_seq?, duration_ms?}`; the command line at most 2000 characters |
  | `terminal.bell` | `terminal.bell` | `{}` |
  | `terminal.notify` | `terminal.notify` | `{title, body}`; OSC 9 has one text, which becomes the title |
  | `terminal.progress` | `terminal.progress` | `{state: remove\|set\|error\|indeterminate\|pause, percent?}`; live only, never stored |

  `terminal.mode`, `terminal.stats` and the harnesses' hook posts never reach the bus: they are
  control traffic, and every subscriber would wake for them. A bell or a progress value more than
  30 s old is dropped, because the host replays the daemon's events from its saved cursor after a
  restart and an old bell would ring now for nothing.
- **The agent's read.** A session's own agent reads its terminals with the `TerminalRead` tool:
  the list, the screen, the recent output and the commands, never a write. It sees only terminals
  its session owns, and host terminals only when `terminals.agent_reads_host` is on (it is off by
  default). What it reads passes the redactor first and is bounded like every tool result.
- **Side channels** (`daedalus/terminals/sidechannels.py`, methods of the same service): the file
  roots of each environment are its project folders plus the directories adapters name
  (`set_extra_roots`), sent on every connection and whenever they change. `hook_events(launch_id)`
  yields a launch's posts in the daemon's order, kept from the moment the launch is registered, and
  ends with the launch. `net_dial` returns a stream bounded at 1 MiB unread. Every program run,
  launch registered or ended, port opened, stream dialled and hook answered is in the audit, with
  the launch's terminal when it is known. Natively the daemons' state directories are sealed like
  the rest of the installation.
- **The host bridge** (`daedalus/terminals/bridge.py`, `app.extensions["host_bridge"]`) is how the
  rest of a Docker install reaches the host: `available()` is the live connection, not the endpoint
  file (a killed daemon leaves that behind); `check_folder(path, create_missing=)` checks or makes a
  project folder through `fs.stat {as_root}` / `fs.mkdir` and says whether it is a git work tree;
  `exec_run` runs git for the staff worktrees of host folders, and a call that cannot reach the
  daemon is an `OSError` there, so "the bridge is down" is never mistaken for git failing;
  `write(path, offset, data)` puts a file handed to a staff member on the host into its inbox
  through `fs.write` (a daemon older than that call answers "update the host terminal");
  `browse(path)`, `stat_folder(path)` and `mkdir(path)` are the new-project folder picker's view of
  the host through `fs.browse`, `fs.stat {as_root}` and `fs.mkdir`, and a daemon without `fs.browse`
  is `HostDaemonOutdated`, an `OSError` that says to update it. Project
  folders accept a host folder only while it answers.

| Route | |
|---|---|
| `GET /api/terminals?env&owner_kind&owner_id&project_id&status&preview=0..12` | `{envs, terminals, capacity}` |
| `POST /api/terminals` | `{env, owner_kind, owner_id?, project_id?, cwd?, title?, sandbox?, cols?, rows?, confirm?}` → the view (201) |
| `GET /api/terminals/load?cap=` | the cost now and the projection |
| `GET`, `PATCH`, `DELETE /api/terminals/{id}` | the view; rename or hand to another owner; remove an ended row |
| `POST /api/terminals/{id}/kill`, `/signal`, `/restart` | end; `{signal}`; the same program as a new terminal |
| `GET /api/terminals/{id}/screen`, `/audit` | the screen (with the emulator); the audit, newest first |
| `GET /api/terminals/{id}/commands?last=1..500&output=0\|1` | `{commands}`, as `terminal.commands`; 501 for a terminal whose program reports none |
| `POST /api/terminals/{id}/ticket` | `{read_only?}` → `{ticket, expires_in}` for the WebSocket |
| `POST /api/terminals/envs/container/update` | `{confirm?}` → `{job, running, image_version}` (202): recreate the terminals service from the image; `409 {"code": "live_terminals", "running"}` until confirmed while any run, `503 {"code": "no_rebuilder", "command"}` without a rebuilder |
| `GET /api/terminals/envs/container/update/{job}` | `{state: pending\|completed\|failed, detail}` |
| `WS /ws/terminals/{id}?ticket=` | the attachment, below |

### The WebSocket

The browser reaches a terminal through the host (`daedalus/terminals/gateway.py`; the two routes are
declared in the API extension).

- **The ticket.** `POST /api/terminals/{id}/ticket {read_only?}`, behind the usual sign-in, returns
  `{ticket, expires_in}`: single use, for that terminal only, valid for `terminals.ticket_ttl_seconds`
  (30 s). It records how the caller signed in (`telegram`, `token` or `cookie`), the user agent and the
  address. `404` for an unknown or lost terminal; `409 {"code": "unavailable"}` while its environment is
  down. Tickets live in memory, at most 256; a host restart drops them with the sockets.
- **The socket.** `WS /ws/terminals/{id}?ticket=…`. The server accepts first and then judges, because a
  refusal during the handshake reaches a browser as a bare 1006 and the app needs the code:
  - the Origin must be the one the `Host` header names (http or https), or exactly the origin of
    `MINIAPP_PUBLIC_URL` (a proxy that rewrites `Host`, and Telegram's webview); a missing or `null`
    Origin is refused — **4403**, and the ticket is not spent;
  - the ticket must be known, unexpired and for this terminal, and is spent whatever follows —
    **4401**;
  - `terminal.attach` with `client {kind: human | viewer, label: the user agent, via, read_only}` —
    **4404** when the daemon has no such terminal, **4409** when the environment is unavailable.
- **The relay.** Frames pass unchanged both ways. From the browser, only INPUT (at most 32 KiB), RESIZE
  (9 bytes), ACK (9 bytes, offset ≤ 2^53 − 1) and ATTACH (a JSON object, at most 4 KiB) are accepted;
  anything else ends the socket with **1008** (1009 when too long, 1003 for a text message). A read-only
  ticket makes the client a viewer, drops its INPUT, and rewrites ATTACH's `readOnly` to true. Messages
  over 1 MiB are refused by the server itself (1009). Nothing is buffered beyond what the daemon's
  window lets through.
- **The end.** The browser leaving closes the channel with its empty frame. The daemon closing the
  channel closes the socket with **1000**; the daemon's connection going closes it with **1012**, which
  the app retries.
- **The audit.** For host terminals only, `attach {via, user_agent, address, socket_address, read_only,
  client_id}` and `detach {client_id, bytes_typed, input_dropped, bytes_out, seconds, ended_by, code}`,
  with the actor `operator`. How many bytes were typed, never which.
