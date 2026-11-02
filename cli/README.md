# daedalus-cli

A terminal for a Daedalus server. The agent, its memory, skills and model keys stay on the server; the
tools of a conversation started here run on this machine, in the folder the client was started in,
through one outbound connection. The client never listens on a port. `dae` is the same binary under a
shorter name. The contract with the server is [`docs/architecture/cli.md`](../docs/architecture/cli.md).

```sh
daedalus-cli login http://<host>:<port>    # pair: a code and a QR code to approve in the app or Telegram
cd ~/src/project && dae                    # a conversation whose tools work in this folder
dae -p "why does the build fail?"          # one turn, the answer on stdout
dae -c                                     # continue this folder's latest conversation
dae resume                                 # pick any conversation to continue here
dae status | dae sessions | dae doctor | dae logout
```

## Local guards

Shift+Tab cycles the guard the executor enforces on this machine. It is the operator's rule, applied
before the server's own policy, and the server cannot override it.

| Guard | What runs without asking |
|---|---|
| `ask` | reads and plainly read-only commands; every write and every other command is asked about here |
| `auto` | everything the server sends (the default) |
| `plan` | reads and read-only commands; writes and other commands are refused |

Start in one with `--guard ask|auto|plan` (`--permission-mode` is the same flag), or set `guard` in the
profile. A command counts as read-only when every program in it is one that cannot change a file (`ls`,
`cat`, `rg`, `grep`, `find` without `-delete` or `-exec`, `git status`, `git log`, `git diff`, …) and
nothing is redirected anywhere but `/dev/null`; anything the client cannot follow counts as a write.

Whatever the guard, a call is held to the session's folder (as written and as the filesystem resolves
it), and the deny list keeps keys and other tools' logins unreadable inside it too. The folder may not
be the home folder or the filesystem's root.

## Headless mode

`daedalus-cli -p "prompt"` opens a conversation in the current folder, runs one turn and exits. A prompt
on stdin is read too (`git diff | dae -p "review this"`). `-c` and `-r <session>` continue one instead.
`--timeout 10m` stops the turn after that long.

`--output-format`:

- `text` (default): the final answer on stdout. With `--verbose`, the tool calls on stderr.
- `json`: one object when the turn ends, of type `result`.
- `stream-json`: one object per line as things happen: `init`, `text` (a piece of the answer),
  `tool_use`, `tool_result`, `permission`, `question`, `notice`, `connection`, `device` (the turn
  waiting for a device to reconnect, and resuming), with `--verbose` also `thinking` and `tool_output`
  (a running command's output), and last a `result`.

A `result` carries `subtype` (`success`, `error`, `unauthorized`, `unreachable`, `waiting_for_operator`,
`timeout`, `interrupted`), `is_error`, `result` (the answer), `session_id`, `run_id`, `exit_code`,
`duration_ms`, `tool_calls` and, on failure, `error`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | the turn completed |
| 1 | the turn failed, or another error |
| 2 | the command line does not parse |
| 3 | not paired, or the server refused the device's token: run `daedalus-cli login` |
| 4 | the server could not be reached |
| 5 | the turn waits for the operator (an approval or a question); the session stays open in the app |
| 124 | `--timeout` ran out; the turn was stopped |
| 130 | interrupted (Ctrl+C, or a stop from elsewhere) |

## Disconnects

If the connection drops mid-turn, the turn pauses on the server and the client reconnects on its own
(1 s doubling to 30 s). A call that was running when the link dropped keeps running here and is never
run twice: the client keeps each call's result for fifteen minutes and tells the server, on
reconnecting, which calls it holds and whether they finished.

## Files and variables

| | |
|---|---|
| configuration | `config.json` in the user configuration folder's `daedalus-cli/` (`DAEDALUS_CLI_CONFIG_DIR` moves it): one profile per server |
| token | the OS keychain (Secret Service through `secret-tool`, the macOS keychain), else `credentials.json` beside the configuration, mode 0600; `DAEDALUS_CLI_CREDENTIAL_STORE=file|keychain|auto` |
| `DAEDALUS_SERVER`, `DAEDALUS_PROFILE` | override the profile's server, choose the profile |
| `DAEDALUS_TOKEN`, `--token` | a token for this run only (the server's API token works for scripts but cannot run tools here) |
| `DAEDALUS_CLI_LOG` | a file to write a debug log to |

## Building

```sh
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=0.1.0" -o daedalus-cli ./cmd/daedalus-cli
scripts/install.sh                     # build and install daedalus-cli and dae into ~/.local/bin
scripts/install.ps1 .\daedalus-cli.exe # Windows: install, with dae.exe as a hard link
```

The build is static and needs no cgo, for Linux, macOS and Windows on amd64 and arm64. The shell and file
calls are ptyd's own handlers (`../ptyd/sidechan`), imported rather than copied.
