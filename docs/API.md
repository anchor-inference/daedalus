# The HTTP API, where the app is not enough

Everything the app does it does over the same API, with the same token (`X-Daedalus-Token`, or the
browser session cookie), so anything the app can show you a script can fetch. Three of those
endpoints are worth naming here.

**Finding a file.** `GET /api/sessions/{id}/files/search?q=&limit=` searches the session's tree by
name: a case-insensitive substring, or a glob when `q` carries one of `*?[` — `*.py` matches the
name, `src/**/*.py` the path. It answers `{"query", "results": [{"path", "kind", "size", "mtime"}],
"truncated", "engine"}`, where `kind` is `file` or `dir` and `engine` says whether ripgrep or the
built-in walk produced the list. `GET …/files/grep?q=&limit=` is the same search over file
*contents* — `q` is matched literally, never as a pattern — and answers
`{"query", "hits": [{"path", "line", "text"}], "truncated"}`. Both are bounded rather than complete:
at most 200 results, 20,000 paths looked at, and a fifth of a second of wall clock for names or one
second for contents, whichever runs out first, with `truncated` saying so. Both reach the filesystem
through the containment the file browser uses, so a symlink out of the tree, a `..` and anything
belonging to the installation itself are absent from the answer rather than refused. Content search
needs ripgrep: without it, `files/grep` answers **501** and a sentence saying to search by name.

**The queue in front of a working agent.** A message sent to a session that is already running is a
steer: it waits in a queue and is placed in front of the model's next call rather than starting a
second run. `GET /api/sessions/{id}/steer` lists what is still waiting, oldest first, as
`[{"id", "text", "queued_at"}]`, and `DELETE /api/sessions/{id}/steer/{id}` takes one back — **409**
once the run has read it, because by then it is in the history and no longer in a queue. The ids are
the ones the queue is stored under, so they survive a restart and mean the same thing to every
client. Whenever the queue changes — a steer taken in, one withdrawn, or a round placing what was
waiting — the session's event stream (`GET /api/sessions/{id}/stream`) carries a `steer_changed`
event with the whole queue in its payload, so a composer draws its cards from the events and never
polls.

**Everything that happens, as one stream.** `GET /api/events` is a server-sent event stream of what
happens to sessions, terminals, staff and notifications. It opens with a `hello` frame carrying
`head` (the newest event number) and `oldest` (the oldest one still kept), then sends each event
with its number as the `id:` line and `{"seq", "at", "type", "project_id", "session_id",
"staff_id", "terminal_id", "payload"}` as its data, and a `: keepalive` comment every 15 seconds.
`types=` narrows it to a comma-separated list of names or prefixes ending in a dot
(`types=run.,terminal.bell`). `after=<seq>` — or `Last-Event-ID` on a reconnect — replays what was
missed and goes on live without a gap; a cursor older than what is kept (seven days by default),
ahead of the stream, or more than 5,000 events behind gets one `resync` frame instead, and the
client re-reads what it shows. The events are stored before they are sent, so a script that
remembers the last number it handled never misses one across a restart of the agent.

**What the operator is looking at.** Each window of the app reports itself with `POST
/api/presence` — `{"client", "kind", "visible", "focused", "sessions", "terminals", "projects",
"lang", "tz"}`, at most four sessions — every 20 seconds while it is visible and whenever that
changes (**204**). A window that is visible, focused and shows a session is *attending* it: a run that
ends there is marked as watched, and a result nobody attended, from a session whose answers do not go
to Telegram, leaves `unread_result` on the session until someone opens it or writes to it. A report
counts for a minute; a window that also holds `/api/events?client=<its id>` stops counting five
seconds after that stream drops. `kind=launcher` on the stream marks a desktop launcher listening
for notifications, which is never a presence. A refused tool call can be answered either way from
any client: `POST /api/sessions/{id}/policy/grant` or `…/policy/refuse` with `{"key"}`, and the
stream carries `permission.pending` and `permission.resolved` for it.

**Notifications.** Everything that wants your attention — a failed run, a scheduled task's result, a
loop that needs an answer, a service that did not come back — is one row of the notification centre
(the Inbox screen, and `/inbox` in Telegram). `GET /api/notifications?view=all|unseen|problems|needs_you`
lists them newest first, with `before=<id>` and `limit=` for paging, and answers
`{"entries", "next_before", "summary"}`; the summary is `{"unseen", "needs_you"}`, also at
`GET /api/notifications/summary` and in `/api/status`. `POST /api/notifications/seen` takes one of
`{"ids": [...]}`, `{"all": true}` or `{"session_id": "..."}`, and `DELETE /api/notifications/{id}`
removes one. Each new or repeated notification is also a `notify` event on `/api/events`, and each
change of what was seen a `notify.seen` event, so a client can keep its badge without polling. A
quiet notification is kept as a record and never counts as unseen.

Where a notification goes is decided once, on the host, from the `[notifications]` section of
`config.toml`: a matrix of categories against the channels (the app, push, the desktop launcher,
Telegram; each cell `on`, `off` or `urgent`), quiet hours in your time zone, muted projects, and what
you are looking at — nothing buzzes for the session on your screen, and nothing is pushed while a
window has your attention. What Telegram already delivered is not pushed again; a session kept off
Telegram stays off it; with no bot bound, Telegram plays no part. The reason for each channel is kept
in the entry's `delivered`. A question or a permission request can be answered from its notification:
`POST /api/notifications/{id}/act` with `{"action": "allow" | "deny" | "answer:<n>" | "open"}` (or
`{"action": "answer", "value": "..."}`); the first answer wins and a second one gets 409 with the
first one's outcome. `GET /api/notifications/preferences` returns the section with the configuration
revision, `PUT` saves it (`{"preferences", "base_revision"}`, 409 when stale), and
`POST /api/notifications/test` sends one notification through every channel there is and reports
each outcome.

An agent can notify you itself with the `Notify` tool (a title, a body, a level, a link, and a key that
updates the earlier notification with the same key instead of adding one). It goes through the same
routing, and each session may send `notify_tool_per_session` of them per `notify_tool_window_minutes`
(5 per 10 minutes) and `notify_tool_urgent_per_hour` urgent ones (2), because urgent passes through
quiet hours. Subagents and a project's staff do not have the tool: their work reaches you through the
agent or orchestrator that gave it to them.

**A project's board.** Every task whose project is set is on that project's board, and a task an
agent adds is drawn on the board of the project it works in. `GET /api/projects/{id}/board` answers
`{"project", "tasks", "needs_you", "counts", "staff"}`: each task carries its four-part `brief`
(`objective`, `deliverable`, `boundaries`, `done_when`) and its `assignee` with that staff member's
live status; `needs_you` is the project's open requests routed to the operator, read from the
requests themselves each time, so an answer from anywhere ends one. `POST` to the same address adds a
task with `{"title", "brief", "assignee_staff_id", "depends_on", "priority"}`; `PUT /api/board/{id}`
also takes `assignee_staff_id` (`""` unassigns), `brief` (the parts sent) and `depends_on`; `POST
/api/board/{id}/accept` moves a task from review to done, and on a task with a staff branch it is the
merge below (**409** when it is not in review, or the merge is refused); `GET /api/board?project=<id>`
is one project's tasks. A project's staff
and orchestrator see its whole board, move only their own tasks and never to done; an ordinary agent
keeps its own board, without the team's tasks. Each change is a `task.created`, `task.moved`,
`task.assigned` or `task.accepted` event naming its `actor`.

**Reviewing and merging a staff branch.** The orchestrator proposes, you merge. `GET
/api/board/{id}/review` reads, without changing anything, what merging the task's branch into its
folder's current branch would bring: `commits` (at most 50), `files` with their added and removed
lines, a bounded `patch`, the dry run's `conflicts` (`git merge-tree`), whether the folder is clean and
still on the branch the work was cut from, the staff member's verification `receipts`, and `blockers`,
each a `code` and a sentence, when Merge cannot be pressed. `POST /api/board/{id}/merge` merges it as a
merge commit, finishes the task and publishes `task.accepted`; the staff worktree is removed when its
member has nothing else to do in that folder, and the branch is deleted only once it is merged. A
conflict is never left in the folder: the merge is aborted, the task is marked `conflict` and
`task.merge_failed` wakes the orchestrator. `POST /api/board/{id}/reject` with `{"note"}` sends the work
back to its member. Nothing is pushed.

**A project's team at work.** `POST /api/staff/{id}/assign` with `{"task_id"}` gives a staff member a
task from its project's board; the task needs all four parts of its brief (objective, deliverable,
boundaries, done-when). It starts at once or waits in the project's launch queue, and the answer
says which: `{"state": "started" | "queued", "position", "reason", "detail"}`. A launch waits while
the project's concurrency is taken, while the member is busy with another task, while the task's
dependencies are open, for a few seconds between two launches of one project, and — for a
command-line member, which is a terminal session — while the machine already runs as many terminal
sessions as its cap allows or the terminals service is not there. Each member in
`GET /api/projects/{id}/staff` carries what it waits for under `queued`, and the listing the whole
queue under `queue`. `POST /api/staff/{id}/tell` (`{"text", "mode": "queue" | "steer" |
"interrupt"}`) answers with the message's receipt, and `GET /api/staff/{id}/messages` lists what was
sent, newest first, each with its delivery state; `…/interrupt`, `…/pause` (finish the turn, commit
what is uncommitted, start nothing new) and `…/release` (`{"keep_worktree"}`) control the live
session. What staff ask — a question, or a call the policy refused — is a request:
`GET /api/asks?project=<id>&routed_to=operator` lists the ones waiting for you, and
`POST /api/asks/{id}/answer` (`{"allow"}`, `{"selected": [...]}` or `{"text"}`, the id or its
six-character short form) answers one; the first answer wins, and a second gets 409 naming who was
first. A request the orchestrator leaves unanswered for ten minutes comes to you.

**A project's orchestrator.** `POST /api/projects/{id}/orchestrator` (`{"model", "autonomy",
"concurrency_cap"}`, each optional) switches it on: a chat of its own that runs the team and does none
of the work — its tools are the brief, the folders, the journal, the team, the board, a read-only
`Peek` into the files, `AskOperator` and `ProjectReport`, and nothing that writes a file or runs a
command. It runs the team with the same limits as your own routes: it hires (`Hire`, only on an
executor that can start here), changes and dismisses staff, hands out tasks (`Assign` refuses a brief
without all four parts), talks to them (`Tell`), reads what they did in bounded pages (`ReadStaff`),
and interrupts, pauses or releases them. It answers their requests within the project's autonomy:
under `ask` its answer to a question is only a suggestion to you and permissions are yours; under
`normal` it grants only by quoting a line of the brief's "allowed without the operator"; under `full`
it grants with a stated reason, and a command-line agent still starts in its usual permission mode.
It can always deny, or pass a request to you with its suggestion. `PATCH` the same address changes its model, autonomy or concurrency, `DELETE` switches it off
(its chat stays), and `POST …/orchestrator/replace` (`{"reason"}`) gives it a fresh chat that names
the one it replaces. It sleeps between turns and is woken by its project's events, gathered for twenty
seconds (at once for a question, a permission, an error or a stuck report); every turn begins with the
project as it is now, which `GET /api/projects/{id}/state` shows as it sees it. Its questions never
pause it: they are requests you answer like a staff member's, and the answer wakes it. Its model is
the project's own choice, else the default for project orchestrators in Settings → Models, else the
strongest preset; the model chip in its chat changes the project's choice. In `GET /api/sessions`
such a project carries `orchestrator: {"enabled", "session_id", "staff", "working", "needs_you"}`
(null for a project without one), and `GET /api/sessions/{id}` names what a session is to its
project: `orchestrator_of` (the project's id) or `staff` (`{"id", "session_id"}`).

**Your rules for a project.** A standing instruction — "whenever X, do Y", "never Z" — is a rule, not
a line of the brief: `POST /api/projects/{id}/journal` with `{"text", "kind": "rule"}` makes one (at
most 600 characters; at most ten rules and 1500 characters in force at once, so all of them are always
shown), and `POST /api/projects/{id}/journal/{entry}/lift` (`{"why"}`, optional) takes one out of
force. The orchestrator records what you tell it in its chat the same way, with `Journal(kind="rule")`.
Every rule in force is shown whole in the orchestrator's state block every turn and in the brief of
every task a member is handed, and the members at work on a task are told when a rule is made or
lifted. `GET …/journal` carries the rules in force as `rules` beside every page, and the entry of a
lifted rule has `lifted: true`.
It asks in batches — `AskOperator(questions=[{title, text, options, multi, …}])` — and takes back what
no longer matters with `WithdrawQuestions(ids, reason)`. What waits for you is a list in the Questions
tab of the panel beside its chat (a sheet behind the header's button on a phone), staff waiting on a
permission or an escalated question above its own questions; the chat itself shows one line, "N
questions waiting". Answer some, leave others — half-answered cards are drafts kept on the device — and
Send carries them together: `GET /api/questions?project=<id>` is the list, `POST
/api/projects/{id}/asks/answer` takes `[{"ask_id", "selected"?, "text"?, "note"?, "allow"?,
"always"?}]` and answers each item on its own (a request answered elsewhere first comes back as a
`conflict`), and the answers that won wake the orchestrator once, together. The main chat's tab is the
same list for every orchestrated project, grouped by project (`GET /api/questions`, `POST
/api/asks/answer`).
It sets its own alarms with `WakeMe` (in some minutes, at a moment, or on a cron at most every ten
minutes) and is woken with the note, even mid-turn; you can leave it one too. `GET|POST
/api/projects/{id}/wakeups` (`{"note", "in_minutes" | "at" | "cron"}`) and `DELETE …/wakeups/{id}` are
the Wake-ups panel of the project. A wake-up belongs to the project: a replaced orchestrator, or one
switched off and on again, gets the ones set before.

A watch is "when this happens, do that", set by the orchestrator (`Watch`) or by you from the same
panel. It waits for a member to finish a turn, ask, need a permission, crash or go silent; a task to
move; a terminal to print a pattern (matched by the terminal daemon, so it never polls); a new commit
on a branch; or a webhook — a pull request, a CI result, or any payload matching a pattern. Then it
wakes the orchestrator, tells a member something, or notifies you. Every watch has a cooldown (at
least a minute), may be set to fire once, and switches itself off, with a journal entry, after twelve
fires in an hour; nothing the orchestrator does itself fires one. `GET|POST
/api/projects/{id}/watches` (`{"when", "then", "cooldown_minutes", "once", "note"}`), `PATCH …/{id}`
(`{"enabled", "note", "cooldown_minutes"}`) and `DELETE …/{id}`. Every accepted webhook is published
as `webhook.received`; a provider with `deliver = "events"` in its `[webhooks.<name>]` section starts no
run of its own and is there only for the watches.

**A project in Telegram.** With a bot bound, a project whose orchestrator is on gets a forum topic under
the project's name. You write there and the orchestrator receives it; its own turns do not stream into
the topic and its replies stay in the app. The topic shows only the orchestrator's reports and
notifications and every request of the project that waits on you — its questions, the folders it asks
for, the staff requests it escalated — with buttons that answer them (the first answer wins, wherever it
was given; a late tap is told who was first) and a reply that answers in words. A request to act on the
host is answered in the app only. Staff, subagents and the notification router never post there.
Replacing the orchestrator keeps the topic, switching it off closes it, renaming the project renames it.
Without a forum (private mode) the same posts come to the private chat under `[project name]`, and a
reply to one goes to that project's orchestrator rather than to the chat's current session. Without a
bot, nothing of this exists and the app has it all.

**What a project spends.** `GET /api/projects/{id}/usage` answers `{"staff", "orchestrator", "other",
"total"}`, each with `today` (the operator's day), `week` (the last 7 days) and `all`, as `{"usd",
"tokens", "unpriced"}`. A Daedalus member is charged for its sessions and their subagents, counted once;
a command-line member for the latest usage its CLI reported, with `subscription` holding the share of the
subscription window used. Calls nobody priced are counted in `unpriced`, never as zero dollars. The team
rows, the project's column and the top of its journal show it, and the orchestrator's state block reads
the same numbers.

**Files between the operator, the orchestrators and staff.** A path means something in one place
only — an orchestrator's inbox in the agent's container does not exist on the host where a
command-line member works — so files travel by *handle*, `att:<id>`. A file you attach in the main chat
or a project orchestrator's chat is kept by the host (the bytes in the blob store) and the model is
told its handle; the main orchestrator reads its files with `Files` and passes them on with
`Delegate(files=…)` or `CreateProject(files=…)`, and the project gets the same handle. A project
orchestrator reads one with `Peek(path="att:…")` (`Peek(op="files")` lists them) and hands it to a
member with `Assign(files=…)` or `Tell(files=…)`, which also take paths in the project's folders. The
host copies each file into the member's own folder, `.agents/inbox/<task>/` (a `.gitignore` of `*`
there keeps it out of git), before the brief that names that path: directly for a member of the
agent's own environment, through the host terminal's `fs.write` for a member on the host — which
writes nowhere but such an inbox. A member names its results in `Report(artifacts=…)`; files among
them are copied back and become the project's handles, which the orchestrator can pass on and put in
`ProjectReport(files=…)` for you and the main orchestrator. Every handle in a chat is a file card you
can download or preview (`GET /api/files?ids=…`, `GET /api/files/{id}/download`); `GET /api/files/{id}`
lists where a file may be used and every time it moved, with who, where, its size and its hash. One
file is at most 50 MB, one hand-over at most 20 files.

**The main orchestrator.** One chat, the home of the app's orchestration mode (the rail's
Orchestration item; on a phone, the first row of that mode's list), where you say "in Bakery, add a
gluten-free menu": it hands the work to that project's orchestrator as a *dispatch* and follows it. It
never touches files, staff or a board; its tools are `Projects`, `Delegate`, `Progress`, `Cancel`,
`CreateProject`, `Answer` and `Files`, beside `Notify`, `StaySilent` and the history tools. A dispatch wakes the
project's orchestrator at once and is closed by exactly one `ProjectReport` with its id (done or
blocked); a progress report is a message on it. The main orchestrator is woken only by those reports
and by a dispatch that went quiet for `dispatcher.stalled_minutes` (30) while nobody in its project
worked, and it tells you in a line or two — pushed to your phone when you are not looking at its chat.
A question a project puts to you about a dispatch (`AskOperator(dispatch_id=…)`) is shown in the main
chat as well as the project's, as one request answered once wherever you answer it; its model is not
woken for it. It answers one for you only when your latest message says the answer (`Answer` quotes
your words), and never a permission. Its model is Settings → Models → Main orchestrator, a mid-tier
preset unless you choose one; the model chip in its chat writes that setting. `GET /api/main` is the
chat as the app draws it (the session, the dispatches, the questions), `POST /api/main` opens it the
first time and `POST /api/main/replace` gives it a fresh chat; `GET /api/dispatches/{id}` and `POST
/api/dispatches/{id}/cancel` (`{"reason"}`) are a dispatch's card. With a Telegram bot it lives in the
General topic of a forum — plain text there is for it, and a reply to another session's post in
General still goes to that session — or, without a forum, it speaks in the private chat under "🧭 Main";
a reply to one of its posts reaches it and `/main` makes it the chat's session. Its questions are
posted there once, with buttons, and never in the project's topic.

`CreateProject` makes a project only after you confirm it on a card in the main chat — a misheard
name never becomes a folder. A container folder must lie under `dispatcher.container_roots` (by default
the folders your container projects already live in); a host folder is looked at on your machine
through the host terminal, and a card for one is answered in the app alone. On "Create" the folders
are made if asked, the project's orchestrator is switched on and handed dispatch #1: survey the folders
and write the brief. Until that dispatch is done — or you press "Finish setup" (`POST
/api/projects/{id}/setup/finish`) — every question the project asks is shown in the main chat too.

A command-line member runs its CLI in a terminal of its own, which you can open like any other. The
launch answers the CLI's folder-trust question on screen before the task is given, and a CLI that
cannot get ready — signed out, or stuck on a screen it does not recognise within `ready_timeout_s` —
shows as an error with that screen, its terminal left open for you. A working CLI that goes quiet has
its screen read: an idle prompt seen twice ends the turn, anything less shows as silence, never as
a failure. Its team tools post to the terminal service's hook listener and are answered there. The
CLIs keep running when the host restarts, and the host takes them up again where they were. A member
is busy only while its session works a task that is still being worked: once the task is done, in
review or dropped, its next task goes into the same session as its next message, when the session
stands in the task's folder, and starts a new session otherwise. A row left grey over an idle prompt,
or working on a task that is over, is settled at start and by the team's ticker.

Claude Code is the first CLI that works as staff. Each launch gets its own settings overlay (hooks
through the terminal service, the team tools allowed), a short statement of how to talk to the team
in its system prompt, and a `daedalus-team` skill, all in the launch's directory and never in your
own configuration or the project. Its permission requests and questions go to the orchestrator or to
you as requests, answered through the held hook while Claude's own dialog stays on screen for you to
answer there too. Messages go one at a time, only when the CLI can take them and never into a dialog,
and each shows how far it got (`GET /api/staff/{id}/messages`); a turn that ends without a report is
passed on with its last words. `GET /api/staff/{id}/session`, `…/transcript`, `…/events` and
`…/changes` are what the staff view reads, and `POST /api/staff/{id}/messages` and `…/seen` its
composer and its "read". The self-check after an update runs one short session, with one tiny prompt
on the cheapest model.

Codex and OpenCode work as staff through their own servers rather than through their screens. Codex
runs its app server as a second terminal beside the TUI; the host starts the thread on it with the
member's instructions and the TUI attaches to that thread, so every message the host sends by the
server shows in the TUI, and approvals and questions come to the host as the server's requests.
OpenCode's TUI carries its server inside it, on a loopback port of the launch and behind a password
made for that launch; messages go to it under ids the host chooses and come back as its events. The
team tools, the instructions and the skill are launch configuration in both, never your own.

pi works as staff through a small extension the launch loads with `-e`: it reports the session's
events, takes the host's messages on a socket in the launch's directory (a steer goes in after the
running tool call), gives the model the two team tools and answers pi's folder-trust question. pi asks
for no permissions. In the container it runs on the Node the Harnesses screen installs.

Grok Build works as staff through an agent file the launch hands it with `--agent`: the file carries
the hooks and the team tools for that session only, so nothing is written into `~/.grok`, and your
own Claude hooks are kept out of it. Its permission prompts come to the orchestrator or to you and
are answered with the dialog's own keys; a steer stops the running turn first, because Grok would
otherwise only queue it behind that turn.

Push reaches a phone or a browser with the app closed once the app is served from a public https
address (`MINIAPP_PUBLIC_URL`). Turn it on per device in Settings → Notifications; inside Telegram the
bot is the push instead, and an iPhone or iPad gets it only for the app added to the Home Screen.
The host signs and encrypts every message itself (VAPID keys made once and kept in the database).
`GET /api/push/config` gives the key a browser subscribes with, `POST /api/push/subscriptions` takes
what `PushSubscription.toJSON()` returns plus a `device` name, `GET` lists the devices and
`DELETE /api/push/subscriptions/{id}` removes one; a device that fails for a week is dropped. A
permission request or a short question carries Allow/Deny (or its options) on the notification where
the platform shows buttons; a host-level permission never does and is answered in the app. When a
pushed request is answered anywhere else, the other devices are told to close it.
