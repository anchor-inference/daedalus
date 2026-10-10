# Configuration

Secrets and machine facts live in `.env` (see `deploy/env.example`). Everything you may change at runtime lives in `config.toml` on the state volume and is edited from the app's Settings: presets and providers, the working rules of the system prompt, spend limits and balance thresholds, compaction, the scheduler, speech-to-text, the vision model, the web-search backend, and MCP servers:

```toml
[mcp.servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/srv/workspaces"]
description = "read and write files under the workspaces directory"

[mcp.servers.remote]
transport = "http"
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ..." }
```

Every session starts with MCP servers off; the agent enables one with `McpEnable`, you toggle them in the app.

Beyond the app's settings, `config.toml` holds the guard rails:

```toml
[limits]
max_run_minutes = 0          # cap on active minutes per run (0 = none)
max_run_tokens = 0           # cap on tokens per run, every call counted (0 = none)

[tools.exec]
max_output_chars = 60000     # the most one tool call returns to the model
sandbox = "off"              # off | workspace — bubblewrap: the filesystem read-only except this
                             # session's own directory, a private /tmp, its own PID namespace.
                             # Defaults to "workspace" on a native install where bubblewrap can
                             # actually run — installed is not enough, the machine has to allow the
                             # namespaces — and "off" everywhere else, a container included.

[tools.jobs]                 # what the watcher says about a background job that still runs; its
quiet_minutes = 15           # end is always reported. One "possibly stuck" note when its log has
max_hours = 6                # not grown this long, one "overdue" note past this age; 0 = never.
                             # Services (Exec service=true) and JobWait waits are spared both.

[tools.results]              # what happens to results the agent has moved past
fresh_count = 6              # the newest results, always shown whole
stale_max_chars = 2000       # head kept of an older result longer than this
trim_batch_chars = 40000     # trimmable excess that must build up before any trimming happens

[tools.groups.browser]       # how a group of tools reaches a run: eager (always in the tool list),
load = "lazy"                # auto (while it fits), lazy (one catalogue line; the agent loads it with
                             # ToolSearch). Lazy by default: browser, self_development, scheduling,
                             # loop, docs, learning, mcp_oauth. Settings → Tools shows each group's
                             # cost and how often it was used; a session can choose its own mode.

[policy]                     # tool policy on top of the built-in rules (daedalus/host/policy.py)
egress_allow = []            # hosts the agent may reach without asking; empty = every host, logged
[[policy.rules]]
tool = "Exec"
pattern = "\\bpip install\\b(?!.*--user)"
action = "deny"              # deny | ask; an allow only lifts an ask, never a built-in denial
note = "no global installs"

[hooks]                      # operator scripts: JSON on stdin, exit 2 refuses (pre_tool), JSON on stdout rewrites
pre_tool = ""
post_tool = ""
run_finished = ""

[compaction]
preset = ""                  # a cheaper preset for the summariser; empty = the session's model
                             # a model that always reasons (Grok) takes 70-90 s per part, so a fast
                             # non-reasoning preset here also keeps the session from waiting on it
call_timeout_seconds = 90.0  # one summariser call; a call that runs out is retried once with twice this

[self_change]
mode = "auto"                # auto | off | local | server — see docs/SELF-DEVELOPMENT.md.
                             # auto resolves at startup from the prerequisites actually present

[board]
wip_limit = 3                # tasks one agent (with its subagents) may hold in 'doing' at once
stale_hours = 6              # a 'doing' task whose session went quiet this long is handed back

[ops]
provider_retry_max_attempts = 6      # runs driven again after the provider failed one; 0 leaves it failed
provider_retry_base_seconds = 30.0   # the first wait; it doubles up to provider_retry_max_seconds
provider_retry_max_seconds = 600.0

[memory]
extract_after_run = false    # store durable facts after a completed run (a paid call)

[modes.review]               # your own mode; the built-in ones (quick, deep, careful, plan) stay unless you redefine the whole table
tools_only = ["Read", "Find", "Search", "WebFetch", "AskUser"]
prompt = "Mode: review. Read and report; change nothing."
```

A refused call comes back to the agent as an error. `refused by policy` is final; `needs the operator's approval`
carries a key you grant once with `/allow <key>` in chat or the *Allow once* button in the app.
