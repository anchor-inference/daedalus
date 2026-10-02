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
