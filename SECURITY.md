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
- **A shared page holds nothing of the app's.** A shared service runs on an origin of its own, or with
  no origin at all, never on the app's; and the session cookie counts only on a request the app itself
  made, because a port or a sibling subdomain is another origin but not another site, and a browser
  attaches the cookie there. A custom page, by contrast, is the operator's own and has the whole API.
- **Secrets are masked** in tool output, logs, transcripts, proposal cards and pull requests.
- **The sandbox fails closed.** With `tools.exec.sandbox = "workspace"`, a container that cannot create
  namespaces refuses commands instead of running them bare.

A link to another person's agent (the guest door, `deploy/guestd`) is the one place a stranger talks to
this installation. It is a process of its own with its own port and database, and it has no route to the app:
the app dials it over a unix socket. A stand-in link answers from a tools-free model call that holds only a
briefing the operator approved, so private data is absent rather than filtered. A direct link is the
operator's informed choice to let a guest reach a chat's live agent; its turn runs alone, marked as a guest's,
with the link's tools, no secrets, and nothing that outlives the turn.

## What it does not protect against

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
