# Remote hands: the server side of a conversation whose tools run on a terminal

`docs/architecture/cli.md` is the contract with `daedalus-cli`. This document is how the server keeps
its side of it: how a remote conversation's tool calls reach the operator's machine, what is recorded
about each of them, and what happens to a turn when the terminal goes away in the middle of it.

## Where a session's tools run

A session's shell and file tools all go through one `ExecBackend` (`daedalus/host/filesystem.py`): none
for this process's own folders, `HostExecBackend` for a host folder seen from the container, and
`DeviceExecBackend` (`daedalus/remote/hands.py`) for a remote conversation. The host never asks which
class a backend is; it reads what the backend says of itself:

| Attribute | Meaning |
|---|---|
| `machine` | where the commands run: `host`, `device:<id>`; absent for a benchmark's container |
| `foreign_paths` | the paths the tools name are another machine's: the working directory, the notes files, an attachment and a sent file are there |
| `put_bytes(path, data)` | puts an attachment of the operator's message into the folder there |
| `prompt_note(workdir)` | the system prompt's line on where the tools run |
| `filesystem(timeout)` | a file interface of its own instead of the shell-built `ShellFS` |

A session's environment (`env` on the session) is `device:<id>` for a remote conversation. The terminal
service has no daemon on a device yet and refuses a terminal there by that name.

A subagent of a remote conversation works on its leader's device, in the same folder.

## Which connection

A device holds one relay connection per running client process (`client` in its `device.hello`), so
`DeviceHub.links` is keyed by device and client. Every connection carries the device's home sessions as
well as the ones its process shows; `RemoteControl` (`daedalus/remote/control.py`) records what each
carries and names a session's carrier, the newest connection that carries it, and that is the link
the backend sends to. Binding a conversation to a device (`RemoteTurns.bind`, called by the create and
hands routes) re-declares the device's connections at once, so the first call of a new conversation does
not wait for the client to say it shows it. The control book, not the hub, announces
`device.connection` when a device's first connection comes up and its last goes.

## One request

Each tool invocation opens a `CallScope` (`daedalus/host/call_scope.py`): its session, its tool call
and a counter. Every request the backend sends for it is `<session>:<tool call>:<n>`, with `n` counting
from 1 within the invocation, and carries `_meta` with the call, the tool, the intent (`read`, `write`,
`exec`) and a title. A file read the shell does is a `read`.

- **No turn, no command.** A command or a write without a call behind it, or for a session whose run is
  not going, is refused before it leaves. The host's own reads at the start of a turn (the notes files)
  and the operator's attachments are the only requests without a call; they are never parked.
- **The terminal's refusal is the tool's.** A `1004` whose `data.reason` is `refused_by_operator`,
  `plan_guard`, `outside_root` or `denied_path` becomes the call's result in the operator's words
  (`ToolPolicyDenied`), not a failed command.
- **Native file calls.** `DeviceFS` reads, writes, stats and lists with `fs.*` (one round trip each; a
  write goes to a temporary file that the last piece renames over the target) and uses the shell only
  for what the terminal does not list under `capabilities.fs`. `find` and `search` are native when the
  terminal lists them, the shell otherwise.
- **Output while it runs.** A command's `exec.output` notifications reach the session's screens as
  `remote_output` events, gathered into pieces of at least 4 KiB or 100 ms, live only.

## The journal

`remote_calls` (`daedalus/remote/journal.py`) has one row per request, written before the request leaves
and moved as it goes. It is the audit (`GET /api/remote/calls`, kept 90 days) and the delivery record:

| `state` | `delivery` | |
|---|---|---|
| `held` | `not_delivered` | the terminal was away; nothing was sent |
| `sent` | `unknown` | handed to the socket, no answer yet |
| `done`, `failed`, `refused` | `delivered` | answered |
| `lost` | `unknown` | sent, and the terminal came back without knowing it |
| `cancelled` | as it was | ended by the operator |

The answer of a request is kept in the row while its tool call is open and cleared once the call has its
result, so a repeat of the call can be answered without the terminal.

## When the terminal goes away

A request that cannot be delivered (no link), or whose link drops before the answer, raises
`ExternalCallPauseRequested` from the backend. The core parks the call as an `external_call` interrupt
and the run stops waiting (`protocore`, `redispatch_external_calls`). The host records the wait as it
records a question, one `pending_questions` row of kind `external_call`, so the session reads as
`waiting`, no other run starts in it, and a restart restores the wait. The wait is announced as
`remote.waiting` and the session view's `remote.waiting` names the parked calls. A message the operator
sends meanwhile is queued for the turn, not taken as an answer.

When the device's relay comes up, every turn waiting on it goes on: the parked calls are handed to their
tools again, in the order the model asked for them, and each tool issues the same requests as before:

- a request already answered is answered from the journal;
- a request never sent is sent now;
- a request sent and not answered is the terminal's to account for, by what its `device.hello` listed
  under `known`: `running`, and its answer is awaited on the new link; `done`, and it is fetched with
  `device.result`; not listed, and it is **not run again**: the call ends saying the terminal came back
  without knowing it, so the agent checks before trying again.

A call whose terminal goes away again parks again under the same interrupt, and the turn waits again.

**Stop** on a waiting turn ends it: each parked call ends with "cancelled while the terminal was
offline" (and, for a call already sent, that it may have run), the turn is closed as cancelled, and the
terminal coming back afterwards runs nothing. Stop on a running turn whose command is on the terminal
cancels the tool and tells the terminal to end the command (`request.cancel`).

**There is no time limit on the wait.** A parked turn holds nothing open on the server (its run ended,
parked, and its snapshot is on disk), it is listed as waiting, and it survives a restart. A clock would
end a turn the operator may come back to the next morning, which is the case the wait exists for.

## Security

- The relay takes only `Authorization: Device <token>` on the upgrade request, and refuses a socket
  opened from a web page before reading anything.
- A device is sent only the requests of the conversations whose hands are on it, and the server takes no
  requests from it on the relay: the client acts only through the REST routes and their scope.
- A guard or a mark a client reports for a conversation outside its scope is ignored.
- Every request is in the journal, whatever became of it.
