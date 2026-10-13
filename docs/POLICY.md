# What the agent may do

Every tool call is judged from its arguments before it runs — a shell command is parsed into its
simple commands first, so `cd x && rm -rf /` is seen as `rm -rf /` — and the answer is allow, **ask**
or **deny**. A denial is final. An *ask* is a denial you can lift: the refusal carries a key, and
*Allow once* in the app or `/allow <key>` in the chat lets that one exact call through, once.

The built-in rules are in the repository (`daedalus/host/policy.py`), so they change only through a
reviewed change: fork bombs, `mkfs`/`shutdown`, `dd` onto a raw device, a recursive delete or `chmod`
of a system path, writing into one, pushing from the operator's checkouts or running any git there
that would change them (a worktree, a branch, a commit, a fetch). Your own rules in
`config.toml` can add denials and questions and can never lift a built-in one.

Two more exist **only on a native install**, where the agent is a process of your own user rather
than something in a container:

- **the installation's own files are refused, to read as well as to write** — the provider keys, the
  state database, the secret that opens the restart channel, the launcher's executable and the
  runtime it runs out of. Through `Exec` as well: the rule reads the paths in the command, so `cat`,
  `cp` and a redirection are the same refusal as the file tools;
- **a path in your home folder, outside every project and outside the installation, asks.** Inside a
  project, a workspace or the checkouts it does not — that is where the work is.

Beside the policy: a **project** contains every path a session *resolves* (`..`, an absolute path
elsewhere and a symlink out of the tree are one refusal, checked on the real path). That is the file
tools, the file browser, the preview, the download and `SendFile` — the one point a path becomes a
place. It is not the body of a shell command: `Exec` runs in the project folder and what it reaches
from there is what the sandbox allows, and where no sandbox is on, what the policy rules allow. On a
native install those rules refuse the installation's own files outright and ask before a path in your
home folder outside every project is touched; in a container they do not fire at all, and the
container's edge is the boundary instead — so a session of one project can read another project's
folder through `Exec` unless a sandbox is switched on. Tools an MCP server provides are the server's
own and pass through none of this: a filesystem server pointed at a folder outside the project
reaches it. The **egress allowlist** turns an unknown host into a question and logs every host either
way; the **spend caps**
are the supervisor's, from its own environment, and the agent cannot edit them; `GOVERNANCE.md` — the
rules the agent always sees — is a protected path it cannot write, and a read-only mount on top of
that in a container. `daedalus doctor` prints what this installation's boundary actually is in one
line, and the desktop launcher's status page prints the same one.

## Action rules

The policy decides what a call may *touch*. Settings → Rules decides how much of what the agent *does
in the world* it decides on its own. Every tool call is sorted into the categories it acts in
(`daedalus/host/action_rules.py` holds the table, and a new tool that acts on the world gets a line
there):

| Category | What falls in it |
|---|---|
| `message` | sending to people: an MCP tool that sends, replies or emails; `gh issue comment`, `sendmail`; a request to a chat or mail webhook; a browser click the daemon reads as sending |
| `publish` | `git push`, `gh pr create`/`merge`, `gh release create`, `npm publish` and the like, `docker push`; `PagePublish` |
| `payment` | a browser action on a checkout or payment step (the daemon's `purchase`) |
| `delete` | `rm`, `git reset --hard`, `git clean -f`, a forced push, `DROP TABLE`; `PageDelete`, `PageRemove`, the calendar's deletes; an MCP tool that deletes |
| `external_write` | an MCP tool that creates, updates or writes; `curl` with a body or a writing method to a remote host; a browser upload, consent or cross-site form |
| `self_change` | `SelfPropose`, `SelfApply`, `SelfRebuild`, `SelfRollback`, `SettingsChange` |

`Notify` and `SendFile` are in none: both reach only you. Each category has one of four levels:

- **auto** — the agent does it;
- **on command** — only when your request asked for it, which the review checks;
- **ask** — you approve it first, exactly like a policy question: a key, *Allow once*, *Allow similar*;
- **hand off** — the agent does not do it; it stops and asks you to do it yourself. You see a card
  (*Done* closes it, nothing grants it) and the agent's own question in the chat.

A call in two categories takes the stricter level. The rules **only ever tighten**: they apply to what
the policy already allowed, so a refusal stays a refusal and a question stays a question whatever the
level — `auto` for payments does not lift the browser's built-in question about a purchase; a site
rule under `[[browser.rules]]` does, and the review then stands behind it. The defaults keep what the
agent did before the rules existed, except paying, which asks.

**Auto-review.** Before an action of a reviewed category runs at *auto* — and every action at *on
command* — a small model (`review_preset`, empty is a middle preset of the table) reads your recent
messages, the session's brief and the action, and answers *allow*, *block* or *ask*. A block comes back
to the agent as a tool error with the reason and puts a notice in the Inbox; an ask is an ordinary
approval request. The review **fails closed**: a timeout (`review_timeout_seconds`), an error or an
answer that is neither word asks you. It never sees a secret: everything it reads goes through the
redactor first, so it reads `«secret:…»` placeholders, not values. Every decision is logged and shown
under Settings → Rules for ninety days. It is on by default for `message`, `publish` and `payment`.

**Pages that talk to the agent.** Every fetched page, browser read, search result and MCP reply is
already fenced as data. Its text is also scanned for what reads like instructions to an AI agent —
"ignore your previous instructions", a fake system message, a request to send keys somewhere, to keep
something from you — and a hit puts a warning above the fence that names what was found without
quoting it. `injection_model = true` has a small model look at every fetched page the patterns pass;
the browser has its own monitor for that (`[browser] injection_monitor`).

The section is `[action_rules]` in `config.toml`; the agent can never change it, the way it can never
change `[policy]`.
