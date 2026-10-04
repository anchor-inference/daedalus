# Contributing

Daedalus is one person's agent that changes itself; contributions from people are welcome on the same terms
the agent gets.

## How a change lands

1. Fork, branch, change. Keep the hand-written style: long lines are fine, formatters are not run.
2. `uv run pytest -q` must pass; `uv run ruff check daedalus tests launcher` must be clean; `cd miniapp && npm run build` for app changes.
3. A change to the host must sit on an execution path: a module nothing imports is refused by the preflight
   (`tests/unit/test_reachability.py`). Say in the pull request which tool, hook, extension or startup step runs the code.
4. Describe the change on its own terms. No session ids, no tool trailers, no references to private planning.
5. One pull request, one change. A large change says what it replaces.

## Where things are

- `daedalus/host/` — the session runner, the engine factory, prompts, skills, the policy.
- `daedalus/tools/` — every tool the agent can call; a new tool is a module with a `TOOLS` list.
- `daedalus/extensions/` — subsystems installed at start-up, named in `EXTENSIONS`.
- `miniapp/` — the app (Vite + React + TypeScript).
- `skills/` — skills (`<id>/SKILL.md`); `personas/` — stances a subagent can take.
- `bench/` — recorded tasks for `daedalus bench`; `daedalus/bench/harbor.py` — the Harbor adapter.
- `launcher/` — the supervisor; it is outside the agent's reach in the container.

## Tests

Unit tests live in `tests/unit`, smoke tests in `tests/smoke` (the preflight runs them). A test that passes when the
feature is broken is worse than none: assert the behaviour, not the configuration.

For bugs, use the GitHub bug report form and the evidence and state definitions in [issue triage](docs/ISSUE_TRIAGE.md).
Report security vulnerabilities privately as described in [the security policy](SECURITY.md).
