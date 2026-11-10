# Security

Daedalus runs an agent with unrestricted tools inside its own container. The container is the boundary;
everything below describes what stands between the agent and the rest of your machine, and how to report
a hole in it.

## What protects the operator

- **Keys never enter the agent container.** Provider keys live in `../daedalus-secrets/keyproxy.env`;
  the key proxy injects them, meters every call, and refuses calls above the daily cap.
- **Self-change is reviewed.** The agent edits its own code in a git worktree and opens a pull request;
  nothing reaches `main` without the operator. The supervisor (outside the agent's reach) runs the
  preflight and rolls back a build that fails it.
- **A policy sits in front of every tool call** (`daedalus/host/policy.py`): commands that act on the
  machine, the operator's paths or the operator's checkouts are refused; a forced push, deleting a
  workspace or a host outside the egress allowlist wait for the operator's approval key. The built-in
  rules change only through a pull request; the operator's rules can tighten them, never loosen them.
  Raising a public tunnel (`ngrok`, `cloudflared`, `tailscale funnel`, `tunneld`, `ssh -R` and the like) is refused
  outright: the operator opens tunnels from settings, and the doctor and the Inbox report a process
  that looks like one ([docs/POLICY.md](docs/POLICY.md)).
- **A shared page holds nothing of the app's.** A shared service runs on an origin of its own, or with
  no origin at all, never on the app's; and the session cookie counts only on a request the app itself
  made, because a port or a sibling subdomain is another origin but not another site, and a browser
  attaches the cookie there. A custom page, by contrast, is the operator's own and has the whole API.
- **`WebFetch` reads the public internet only.** Every address a name resolves to must be public, the connection goes
  to the address that was checked, and every redirect is checked again, by the policy as well, so a page cannot turn
  the fetch towards the router, the cloud's metadata address or the app's own API under its public name. The one
  exception is the installation's own shared links (`/s/`, `/c/`), whose name may resolve inside the home network.
- **Secrets are masked** in tool output, logs, transcripts, proposal cards and pull requests.
- **The sandbox fails closed.** With `tools.exec.sandbox = "workspace"`, a container that cannot create
  namespaces refuses commands instead of running them bare.

A link to another person's agent (the guest door, `deploy/guestd`) is the one place a stranger talks to this
installation, so it is built on what that stranger cannot reach rather than on what they are asked not to do.

- The door is a process with no network. A small front holds the published port, bounds each connection and passes
  requests down a unix socket. The app dials the door over another socket, with a token only the app can write, and
  the door cannot call the app. A stand-in's model call goes to a listener of the key proxy that takes one chat
  completion on chosen upstreams and nothing else: no streaming, no tools, a small output bound, no dot segments in
  the path, and no header of the door's own, so a call cannot hide from the ledger. The door holds no credential;
  the proxy adds it, for a key, a prepaid plan or a CLI login alike. Search backends are never among the upstreams.
- That listener serves only the models the app lists, on the upstream it lists them for, by a table the app writes
  into its state folder, which the door does not mount. A model the operator limited to some OpenRouter providers is
  served only within them, and one with no such choice takes no provider object. A change holds from the next call
  with nothing restarted, and with no table the listener serves nothing. So a taken door can still spend on a listed
  model and cannot pick a dearer one, and a link whose preset was removed stops being answered.
- A stand-in link answers from a tools-free model call that holds only a briefing the operator approved, so private
  data is absent rather than filtered. The Codex route cannot carry an output bound: its backend refuses the field,
  so a reply there is bounded by the plan and not by the request.
- A plan or a CLI login counts no dollars, so a link on one is bounded by its turn limits and nothing else. A metered
  model with no known price is not offered at all.
- A direct link is the operator's informed choice to let a guest reach a chat's live agent. Its turn runs alone,
  marked as a guest's, and never as a queued or steered message. Every tool setting is a list of what is allowed.
  `read` is the file reading tools held to the chat's folder. `all` adds writing there, web search and foreground
  commands. A guest's command runs in a sandbox of its own whatever the operator chose for theirs: an empty root
  with the system binaries and that folder, no network, a cleared environment, and nothing left running at its end.
- What a guest's command can use is bounded as well as what it can see. Each one has a cgroup: 1,024 threads and
  8 GiB of memory, all of it counted across its processes, with no swap, and all guests together hold 2,048 threads,
  12 GiB and half the processors, so more links do not multiply the numbers. Its writes go to a tmpfs of 4 GiB and
  500,000 files, shared by the folder and `/tmp`, that lies over the real folder: nothing reaches the disk while it
  runs. When it has ended, its changes are copied in if the folder then holds no more than 8 GiB and 1,000,000 files
  and the disk keeps 10 GiB and 1,000,000 inodes free, and are dropped whole if not. A command that is killed keeps
  nothing. The copy runs as root over what a stranger made, so it follows no link and drops setuid. Each limit that
  is reached is said in the command's output. A process is also limited to a 2 GiB file and ten minutes of processor.
- Where that sandbox or those limits cannot be made, or the chat works in a folder on the host or a project's folder
  other chats read, `all` is not offered. They need the agent's container: a native installation has no such limits,
  so it has no `all`. Once a guest could write in a folder, its notes are no longer read as the agent's rules.
- A guest's turn has no secrets at any setting, spends no grant the operator gave, raises no approval card (what
  would ask is refused), starts nothing that outlives it, and is not resumed or retried after it stops. Each of its
  requests leaves out what the host added to earlier messages (memory, the folder's notes, the board, project facts,
  side questions) and what a memory tool returned, in a view of the history; the stored history is not changed.

This agent can also be the guest of someone else's door (`AgentTalk`), which is the same stranger reaching a run that
holds everything. Reading the reply is the point, so the rule here is that a run which has heard a stranger keeps no
means of acting on its own.

- A link in a chat is inert. It is recognised by its shape, without a request, and opens only when the agent calls
  `open` with the whole address as the operator wrote it in that chat: one from a reply, a page or a file does not open.
  Only the operator's own turns may use the tool, never a schedule, a loop or a subagent.
- The client dials https to an address on the public internet, checked after resolving and connected to as checked,
  follows no redirect, and bounds what it reads. Everything a door sends is data: only named fields are read, its
  sentences reach nobody, and the state of a conversation is told to the agent in this code's words from an
  enumerated value. A conversation's token is in one column, goes out in one header, reaches neither the model, the
  app nor a log, belongs to one chat, and is emptied when the conversation closes.
- A reply reaches the model inside a fence with a nonce of its own and is stripped of what draws as nothing. From then
  until the operator's next message the run is touched: reads and the conversation go on; commands, file changes and
  web requests each wait on a card of their own, past any grant made earlier and any standing allow, and it offers none,
  in the app or in Telegram;
  and everything else is refused, memory, schedules, notes, the board, other agents and whatever tool is added next.
  The touch survives a restart, the turn an approval continues and a recovery. Autopilot does not continue such a run.
  The memory pass, the learning record, compaction and the identifier index all treat its words as a stranger's.
- What goes out: the first message of a conversation waits for the operator, as does a later one sent after the run
  read or ran anything since a reply, one that looks like a secret, and one the automatic review did not pass. Its card
  reaches the app and Telegram, with Send and Don't send. Allowing it sends the text as it was held, and no model says
  it again; a long one is cut on the card, which says so, and is
  whole in the chat's sheet. The review setting per conversation is `first`, `all` or `off`.
- A reply that comes after the turn waits in the chat and costs nothing. Waking the chat on a reply is the operator's
  switch per conversation, with a number of turns and dollars, and no tool can reach it. Such a turn is a guest's
  turn: no secrets, no grants, no cards of the policy's, a few model calls, `AgentTalk` on this conversation alone,
  one send, and a view of the history holding only this conversation's own runs, without what the host added to them.
- Sends per conversation are capped, a reply that repeats itself turns waking off, a conversation nobody spoke in for a
  day is ended on both sides, and the far door keeps its own limits on its own side.

## What it does not protect against

A run that heard a stranger still holds what it held before: the operator's request, this chat's memory block and
whatever it read earlier in the turn, and a reply can ask for those. Nothing on the wire tells a leak from an answer,
so past the first message of a `first` conversation a message built only from what is already in the run goes out
with nobody reading it first. The review is a model, and so is the agent. `all` is the setting that closes this. A
reply stays in the chat's history, fenced, and the operator's next message opens a run with full tools over it: a
message that plants a plan for later is the same threat as a web page that does. What such a run writes next is not
marked as a stranger's, and what is read aloud to the operator can talk them into something. A wake's history holds
the whole of each run that took part, the operator's own opening words among them, so a run that did other work
before it opened a link shows that work to a turn whose reply goes to a stranger. A proxy set for the web tools takes the name past the address check.

A direct guest's words and the answers to them stay in that chat's history. Later turns read them fenced and
marked, and compaction marks them, but the chat's own agent has read a stranger, and a marked summary is still a
summary. Every message of the chat, what the agent wrote included, is open to a direct guest by design, and one model's
judgement is all that stands between those messages and the guest: an answer that quoted memory or a note in the chat's
own words is a chat message like any other. `all` bounds what a command holds, not how it spends that: inside its
limits it can read the disk hard, and it runs as root without capabilities, so counters the kernel keeps per user
(inotify instances, say) are shared with root's other processes for as long as it runs. `Write` and `Edit` work on the
real folder and pass none of the quota; what they add is bounded by what a turn's model can send. An agent run by the
operator can still reach the door's token with a path built at run time: the sealed set reads the words of a command.

On a native install the same door runs as the operator, on the operator's machine, and a tunnel can lead to it.
What the launcher puts around it depends on the system (`desktop/README.md`, "What the door gives up here"):
bubblewrap on Linux, a seatbelt on a Mac that may be only hygiene, and on Windows nothing but the door's own refusal
to dial out or start a program, which is a promise, not a wall: code running inside it could undo it. What a tunnel
reaches is decided by a router that knows two ports, so the app and its API are not behind it however a request is
written, and no tunnel client opens a control port on this machine. A visitor there counts as local nowhere: a
request needs a loopback peer, no sign of having been forwarded, and a name only this machine answers to.
Agent-written pages under `/s/` share an origin with `/a/` on a tunnel's one hostname; the door keeps nothing a
browser could give away, and refuses requests made by a page. Shared services share that origin with each other,
so a public service's page can use a visitor's phone pass or key cookie for another. A phone's code travels over
plain http on a home network: it opens one service for one phone by that door only, is spent once within ten
minutes, and has a table of its own so that it cannot be taken for a way into the app.

A canvas shows agent-written pages as live frames, under `/f/` on the same door and nowhere else: on the app's
origin the route does not exist, an https app with no services address shows a card instead of the page, and so
does a native Windows host, which cannot open the page's files without following links.
A frame's address is signed with one key for all canvases (`canvas_frame_key` in the database), names one frame
and a random secret that changes whenever the page is shown again, taken off the board or retired, expires in
twelve hours, and names the app as the only page that may frame it. The door strips no cookie on `/f/`; the route
reads none and the page could not use one. Every answer is sandboxed without `allow-same-origin`, with no dialogs,
popups, downloads or navigation of the window around it, so the page runs with no origin, no cookie and no storage
of anyone's. Its content security policy lets it load its own address prefix on the door (not the rest of the door,
where shared services and other frames live) and only the origins the operator allowed: "this time" holds only
while the board that allowed it stays open, "always" until it is taken back. Anything else is refused by the
browser and reported to the board. Files are opened from `/` down without following any link, so a folder swapped
for a link after the page was shown reaches nothing; a file with a second hard link is refused too.

The board believes a frame's words only under a nonce the door gave the document its iframe loaded, confirmed by
the host once, and takes a picked element only as the answer to a click it saw on the glass it lays over a frame
in comment mode. What a page sends into the chat is fenced as the page's data, the server accepts only selectors
of the bridge's own grammar, and the message goes only if it is still exactly what the operator saw. A still copy
for a shared view is taken only while the canvas is shared, from the page's markup and never from what was typed
into it, and is deleted when the share ends.

What a page can still do: read every file under its root (its own folder unless the agent names a wider one
inside the workspace, never the workspace itself) and send it to an allowed origin; navigate its own frame
somewhere else, which the board notices and answers by blanking the frame and retiring its address, though a
request already sent is sent, and a navigation answered with 204 leaves the page in place and is not noticed at
all, so what it carried in its address is gone unseen; open a WebRTC connection, which no policy governs in the browsers of today; and
resolve names in ways a browser may still prefetch despite the page being told not to.

**What the installation keeps on a native install.** A provider's token, the launcher's token, the keys in
`keyproxy.env`, the Telegram bot token and API hash in the launcher's `.env`, and the state folder with the database
that holds the app's API token are all files of this user. The policy refuses a command that names them, by matching
words. On Linux with the sandbox on, a command's filesystem holds none of them: the state folder, the keys' folder and
the folders with copies of them (the backups, the update's) are covered by empty ones, which a save by rename does not
undo, and `.env` by an empty file, which holds because the launcher writes it in place. Links are followed first, so a
folder moved elsewhere is covered where it is. A terminal the agent drives in its sandbox is shown the same nothing once
its terminal daemon is new enough to hide what the app names; an older one hides only its own folders until it is
updated. On a Mac, on Windows, or with the sandbox off, there is no such wall: a command that builds the path reads
them all, and the key proxy's card and the public address form say so. A team member's CLI and a terminal outside the
sandbox read them everywhere. In the container the keys are in the key proxy's container and there is no `.env` to
read; the state volume is mounted, and a command there can read it. The agent with such a token could raise a tunnel
of its own, which the watch would report. With a key it could spend from anywhere, past the proxy's budget and ledger.
With the API token it could call the app as the operator. The key proxy's own port stays open to a command in
every case: it can use a key it cannot read, as far as the proxy's budget lets it.

- A model that is determined to misuse the container's own resources (CPU, disk, the agent's GitHub
  organisation) can do so within the policy's limits; the policy catches the accidental and the lazy case.
- Anything you mount into the container is the agent's. Mount only what it should have.
- A service opened directly on its own port, on the same host name as the app, receives the app's
  cookies, because cookies ignore ports. Natively the two names differ for that reason. On a bare
  address, open shared services through the door, which strips them.
- Telegram is the operator channel: whoever controls the operator's Telegram account controls the agent.

## Reporting

Open a private security advisory on the GitHub repository, or write to the address on the maintainer's
profile. Include the version (the commit on `main`), what you did, what happened, and what you expected.
Please do not open a public issue for a vulnerability before it is fixed.
