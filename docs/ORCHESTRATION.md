# Orchestration: from a goal to an accepted result

A project's orchestrator (the coordinator) plans the work, hands tasks to a small team, checks what
comes back and asks you only what it has to. This page walks the path once, then explains the parts
you will meet on the way.

<p align="center">
  <img src="screenshots/project-focus.png" width="85%" alt="A project in focus mode: the coordinator's chat beside the board" />
</p>

## The first result, step by step

1. **Add a project.** Pick a folder (a repository or a directory of documents), write the goal, a
   constraint or two, the first task and how you will know it is done. Saving records the goal and
   its criteria; it starts nothing and spends nothing.
2. **Switch the orchestrator on.** Choose its model (the app checks the model answers before the
   switch is saved) and its autonomy — see below. The coordinator's chat opens.
3. **Hire a member.** A Daedalus agent, or a command-line agent such as Claude Code or Codex. Choose
   how it works in the folder: its own Git worktree, the shared folder, or read only.
4. **Say what you want in the coordinator's chat.** It assigns the task; the member works and hands
   in a report.
5. **Review.** Open the task: the member's original report, the checks it ran, the files it handed
   in (pictures show inline), and for a branch the diff. Record evidence for each criterion and give
   your verdict. A passing `Verify` run on the branch's current commit counts as evidence with one
   click.
6. **Merge and accept.** A branch is merged from the review card once the verdict is in — a folder
   without CI needs none, and required checks join the gate only when you name them. Accepting the
   result closes the task.

## Autonomy decides what the coordinator may do alone

| Autonomy | The coordinator… |
| --- | --- |
| **ask first** | brings you every decision; to assign and run a task it needs your permission for that task |
| **normal** (default) | plans, assigns, runs and reviews the project's tasks by itself; anything outside the brief comes to you |
| **full** | also answers its team's requests itself, always with a stated reason |

Under normal and full autonomy the permission is the setting itself: there is no per-task
permission step. Switching a project to *ask first* withdraws it at once.

## What needs you

One list per project — *Needs decision* — holds every question, review and missing input, each
once, with a link to where it came from. The sidebar and the phone show the same count.

## Changing the goal

Edit **Goals** or **Done when** in the project's Brief. The change starts from the current words.
You may tick open tasks the new goal changes: those and the tasks that depend on them go back to be
planned again, and their running workers stop. Tick nothing to keep the board as it is.

## The team

- **Own worktree** — the member works on a branch of its own; the board merges it when you accept.
  A project may name a **setup command** (`uv sync --frozen && npm ci`) that runs in each new
  worktree before the member starts; if it fails, the task stays unstarted with the last lines of
  its output on the card. Files git ignores do not come with a new worktree, so a repository may list
  the ones a worktree needs (`.env`, a local MCP configuration, `secrets/`) in a `.worktreeinclude` at
  its top, in `.gitignore` syntax. They are copied from the folder into each new worktree before the
  setup command: only files the branch ignores too, never over a file already there, with their
  permissions, never through a symbolic link, at most 200 files and 32 MiB. A host folder reached
  through the terminal bridge gets no copy, since the bridge writes no files.
- **Shared folder** — the member works directly in the folder. Where the machine can contain a
  command-line writer (delegated cgroup v2), it does; elsewhere the member runs as an ordinary
  process.
- **Read only** — a command-line member starts in its CLI's own no-write mode (Claude and Grok in
  plan mode, Codex in its read-only sandbox, Cursor in ask mode). A CLI without such a mode cannot
  be read only, and the launch says so.

Services a member starts stop when its session ends.

**Member runtimes** in a project's settings is an ordered route, for example Claude Code, then
Codex, then a Daedalus member, each with an optional model. A member keeps the runtime it was hired
on as its first; the route supplies the fallbacks. A launch moves to the next runtime only when one
is refused before it starts for a reason that is certain: a usage window the CLI reported as spent,
no sign-in, a CLI that is not installed, or every provider of a Daedalus preset out of quota. A
launch that was accepted, or whose outcome was lost, never moves on, and a task that still has a
live member on another runtime needs the reviewed runtime handoff instead. The coordinator's answer,
the journal and the card say what happened ("Claude Code: usage window exhausted … → Codex
accepted"), and the member's card shows that it runs elsewhere until its own runtime takes a launch
again. An empty route launches every member on its own runtime alone.

**Competing attempts.** For a card where the right approach is unclear, the coordinator can race two
or three attempts at it with its `Explore` tool and keep the best. Each attempt is a one-off member
working a copy of the card in a worktree and on a branch of its own. Attempts of a Daedalus member
continue that member's conversation: their requests start with the same bytes as its own, so the
provider serves the shared history from its cache and each attempt pays only for its own tail. A
command-line attempt is a fresh member of the runtime and model the coordinator names, with an
optional approach of its own. The exploration reserves its budget (by default
`orchestrator.explore_usd_per_attempt` for each attempt) against the project's goal budget before
anything starts, and is refused when the budget cannot hold it; the attempts have
`orchestrator.explore_deadline_minutes` to report. Their reports do not wake the coordinator one by
one: once every attempt has reported, or the deadline or budget passes, it is woken once with the
attempts side by side — what each changed, the checks it reported, what it cost — and picks one or
none. The kept attempt's card goes on to review and the merge like any card, and the cards that
waited on the original wait on it; the others are dismissed, their worktrees removed and their
branches parked under `refs/daedalus/explored/`. Everything is recorded before it is launched, so a
restart reports an attempt whose launch it interrupted instead of launching it again. The card
shows its attempts with their state, cost and the one kept.

## Folders on the host

When Daedalus runs in a container, a project folder may live on the host machine instead. Every
agent — Daedalus agents, the coordinator and command-line members — works there through the host
terminal daemon (`deploy/host-terminal.sh install`). While the daemon is not answering, a session in
such a folder is refused with the command that starts it.

## When something goes wrong

- A start that was interrupted, or a stop that was not confirmed, shows on the board with what to
  check and a button to release the task. The technical record is under *Details*.
- **Execution details** on a task say why a worker was stopped (for example, its memory limit) and
  **Copy diagnostics** puts the host's redacted fault list on the clipboard for a bug report.
- A goal's budget sits in the project's settings and header, and an accepted task shows what its
  result cost; an unknown price is shown as unknown, never as zero.
