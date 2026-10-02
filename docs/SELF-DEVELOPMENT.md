# Self-development

<p align="center"><img src="diagrams/selfdev.png" alt="Self-development: worktree → edit → pull request → your approval in the chat → merge → rebuild → rollback on a failed preflight" width="100%" /></p>

The PR text passes a public-text gate (nothing about your machine leaks into a public repository), the diff is checked for references it must not carry, and `GOVERNANCE.md` — the rules the agent always sees and can never edit — is mounted read-only. Approval is manual by default; `/approval auto` hands it over when you trust it.

**Not every installation does this.** `[self_change] mode` is `off`, `local` or `server`, and `auto` — the default — works out which one this installation can honour when it starts:

| mode | what it means | what it needs |
|---|---|---|
| `server` | worktree → pull request → your approval → merge → rebuild, as above | a GitHub token, an `origin` on both checkouts, and a way to deliver a build (the supervisor socket or the compose `rebuilder`) |
| `local` | the agent edits the checkout this installation runs from; there is no fork and no PR, and the change applies after a restart | a writable git checkout of the host and the core |
| `off` | the agent does not change its own code | — |

**Local mode, step by step.** This is what a desktop install does, and it is the owner's rule that the
desktop version can improve itself too. The agent works in a worktree exactly as above and runs the same
checks; instead of `SelfPropose` it calls `SelfApply`, which fast-forwards its commits onto the checkout's
own branch — one readable line of history, no remote, and its `Co-authored-by: Daedalus` trailer intact,
because nothing here is published. The app then shows **"Changes are ready — restart to apply"** with the
summary and a **Restart** button; the launcher's status page shows the same and its **Restart to apply**
goes the same way. The restart is not a leap of faith: the supervisor checks out that commit into a
detached worktree of its own, runs `uv sync` (only if `uv.lock` or `pyproject.toml` changed, in either
repository), `compileall`, `daedalus check` and the smoke tests there — in a virtualenv of its own, so a
change that is refused has touched nothing the running bot imports — and stops the running bot only once
they pass. The launcher's plain **Stop** and **Start**, and closing the window and opening it again, are a
different thing: they bring the stack back up on whatever the checkout holds, with none of that run. They
are how you start over, not how you apply a change. A change that fails is taken back out of the checkout and the app says why. A
change that passes the checks but cannot stay up — three starts dying within ten minutes — puts the last
known-good commit back by itself, and the app says that too; the commit is still in the checkout's
history, and on the agent's branch in its own repository. A change to the `Dockerfile` or the system packages is
applied as far as a restart can take it and says plainly that the rest needs a new image.

**Where the worktrees come from.** Not from your checkout. The agent has a repository of its own beside
its worktrees (`/srv/worktrees/repositories/bot.git` and `core.git` in a container), filled once by
fetching from your checkout — a read — and fetching from the checkout's `origin` after that. Its
worktrees, branches and commits live there, so nothing a session does writes your checkout's `.git`: the
sandbox binds that directory read-only over everything else it opens, and the policy refuses any git
command that would change the checkout. The one write into your checkout is `SelfApply` in local mode,
made by the app itself, which fetches the agent's branch and fast-forwards yours onto it. Worktrees cut
the old way, from the checkout, are moved into the agent's repository on the next start with their
branches and uncommitted edits intact; the entries they leave in your checkout's `.git/worktrees` are
removed by `git worktree prune` run on the machine, where their container paths do not exist.

The same gates decide in both modes: a changed host module needs a passing `Verify` receipt that covers
the bytes in the branch, an `execution_path` that names the code running it, and a summary that says what
a large change replaces. What local mode drops is the review — there is no reviewer and nothing is
published — so the restart is where you see the change, and the rollback is what catches what you did not.

What follows the mode: the `Self*` tools (absent in `off`, `SelfWorkspace` and `SelfApply` in `local`, the pull-request four in `server`), the self-development extension, `/api/proposals`, `POST /api/self/restart`, the Changes screen and the restart banner in the app, the self-development part of the system prompt, and the doctor's GitHub checks. `GET /api/capabilities` and `daedalus doctor` both say which mode is running and why. Setting the mode explicitly overrides the resolution; the doctor then warns about whatever the chosen mode is missing. A change of mode takes effect on the next restart.
