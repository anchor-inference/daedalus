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
trim_min_share = 0.25        # a trim must save this share of the request: a cut far back makes
                             # the provider write the whole conversation into its cache again
trim_window_share = 0.075    # and this share of the model's window (4 characters a token), so a long
                             # window trims seldom and in large steps

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

[action_rules]               # how much of what it does in the world the agent decides alone (docs/POLICY.md)
levels = { message = "auto", publish = "auto", payment = "ask", delete = "auto", external_write = "auto", self_change = "auto" }
                             # auto | on_command | ask | handoff; only ever tightens the policy
review = ["message", "publish", "payment"]  # reviewed by a small model before they run at auto;
                             # on_command is always reviewed; a review that cannot answer asks you
review_preset = ""           # empty = a middle preset of the table
review_timeout_seconds = 20.0
injection_model = false      # also ask a small model whether a fetched page talks to the agent

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

[navigation]                 # where the app puts each destination; Settings -> Navigation edits it
rail = ["inbox", "terminals", "page:dosing"]  # the desktop rail, top to bottom
more = ["usage"]             # under the rail's More menu, in order within its group
drawer = ["inbox", "terminals", "board"]  # the phone drawer; non-empty is the whole drawer, the rest
                             # waits in the phone's More sheet
hidden = ["voice"]           # never shown on any device; wins over the lists above
icons = { terminals = "bolt" }  # a built-in icon name in place of the item's own
                             # Every list empty is the shipped layout. An item is a screen name or
                             # page:<slug>; a place listed nowhere keeps its usual spot, and an id
                             # the app does not know (a removed page) is ignored where it is drawn.

[agent_settings]             # what the agent's SettingsChange may apply without asking
auto_apply = ["navigation", "answer_language", "compaction"]
                             # every other section waits for your Apply on a card in the chat and in
                             # the notifications; this section itself always does. Credentials, policy,
                             # action_rules, self_change, ops, webhooks, the sandbox and the browser's network wall are
                             # never the agent's to change (daedalus/host/agent_settings.py)
proposal_hours = 24          # a change nobody answered expires and the agent is told

[modes.review]               # your own mode; the built-in ones (quick, deep, careful, plan) stay unless you redefine the whole table
tools_only = ["Read", "Find", "Search", "WebFetch", "AskUser"]
prompt = "Mode: review. Read and report; change nothing."
```

A refused call comes back to the agent as an error. `refused by policy` is final; `needs the operator's approval`
carries a key you grant once with `/allow <key>` in chat or the *Allow once* button in the app.
