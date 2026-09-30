# Development

## Layout

```
daedalus/
  host/         sessions, engine wiring, prompts, tool policy, hooks, skills store, checkpoints
  providers/    OpenAI-compatible adapter, fallback chain, pricing, registry
  tools/        one tool per module, PascalCase names
  stores/       SQLite stores, blob store, durable memory
  security/     redaction of secrets in what the model and the chat see
  transport/    Telegram (aiogram 3): topics, rich messages, voice, files
  extensions/   HTTP API + app, self-development, scheduler, loops, subagents,
                services, board, peers, notifications, heartbeat, balance, voice, MCP
  bench/        headless task runner and the Harbor adapter
launcher/       the supervisor (PID 1, never edited by the agent)
miniapp/        Vite + React app (Telegram Mini App and browser); src/router.ts, shell.tsx,
                dialogs.tsx, store.ts, format.ts and one file per screen under src/screens/
skills/         SKILL.md skills the agent can load
personas/       the persona the prompt is built from
deploy/         Dockerfile, compose, key proxy, SearXNG settings, env examples
desktop/        the launcher: one binary that runs the stack on a personal machine — the setup
                page, the app window, the portable runtime native mode downloads (outside data/)
tests/          unit and integration tests; tests/browser drives the built app with a real mouse
docs/           design and decisions (2026-09-06, historical), screenshots, diagrams
```

## Evidence

`uv run python -m daedalus --state-dir <dir> bench bench/selfcheck.json --preset <preset>` runs recorded tasks
headless and writes one record per task (verdict, turns, tokens, cost, wall time) plus its trajectory. The same
loop runs under [Harbor](https://harborframework.com) against Terminal-Bench, Aider Polyglot, SWE-bench and the
rest of its adapters: `harbor run -d <dataset> -a daedalus.bench.harbor:DaedalusAgent -m <preset>` with
`BENCH_STATE_DIR` naming a state directory of its own. Pin a provider's `temperature` in `config.toml` for runs
that should be comparable. A task that declares GPUs aborts the whole Harbor job on a machine without one, and
the trials already running with it: exclude such tasks with `-x <org>/<task>` — a registry dataset names its
tasks with the organisation, so a bare task name matches nothing (`grep -l 'gpus = [1-9]'` over the dataset's
`task.toml` files lists them). Benchmark sessions run without the memory tools, so nothing carries from
one task to the next.
