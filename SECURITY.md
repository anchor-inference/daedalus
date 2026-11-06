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
  Raising a public tunnel (`ngrok`, `cloudflared`, `tailscale funnel`, `ssh -R` and the like) is refused
  outright: the operator opens tunnels from settings, and the doctor and the Inbox report a process
  that looks like one ([docs/POLICY.md](docs/POLICY.md)).
- **A shared page holds nothing of the app's.** A shared service runs on an origin of its own, or with
  no origin at all, never on the app's; and the session cookie counts only on a request the app itself
  made, because a port or a sibling subdomain is another origin but not another site, and a browser
  attaches the cookie there. A custom page, by contrast, is the operator's own and has the whole API.
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
  It runs under limits on address space, file size and processor time. Where that sandbox cannot be made, or the
  chat works in a folder on the host or a project's folder other chats read, `all` is not offered. Once a guest could
  write in a folder, its notes are no longer read as the agent's rules.
- A guest's turn has no secrets at any setting, spends no grant the operator gave, raises no approval card (what
  would ask is refused), starts nothing that outlives it, and is not resumed or retried after it stops. Each of its
  requests leaves out what the host added to earlier messages (memory, the folder's notes, the board, project facts,
  side questions) and what a memory tool returned, in a view of the history; the stored history is not changed.

## What it does not protect against

A direct guest's words and the answers to them stay in that chat's history. Later turns read them fenced and
marked, and compaction marks them, but the chat's own agent has read a stranger, and a marked summary is still a
summary. Every message of the chat, what the agent wrote included, is open to a direct guest by design, and one model's
judgement is all that stands between those messages and the guest: an answer that quoted memory or a note in the chat's
own words is a chat message like any other. `all` has no bound on processes or on total disk. An agent run by the
operator can still reach the door's token with a path built at run time: the sealed set reads the words of a command.

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
