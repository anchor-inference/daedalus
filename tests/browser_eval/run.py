"""Run the browser evaluation: real models driving Daedalus's own browser tools through local fixture sites.

What is measured is the product: the tool names, descriptions and schemas a session is given
(``daedalus.tools.browser``), the browser rules a session reads (``group_instructions("browser")``),
and every call going through ``BrowserAgent`` — the one code path a session and a command-line staff
member share — into the browser service and a real ``browserd`` with a real Chromium. The only thing
that is not the product is the loop around the model: a plain chat-completions loop, because the
session machinery would add a database of sessions, a bus and a policy engine that have nothing to
say about how well a model finds a button. Two things are fixed on purpose: ``BrowserLook`` answers
as it does on an installation without a vision model, so a task is solved by the outline and the
actions and not by a second model's eyes; and every sensitive-action question is answered yes at
once, as an operator's standing grant would, and counted.

The fixture sites are on this machine's loopback, which the network wall refuses by default. They
are let through the way the integration tests do it: the host's wall rules name their ports as the
agent's services, exactly what the host does for the agent's own preview servers. The defaults are
not touched.

This is not part of the test suite and never runs in it: it spends money and reaches the model
providers. Run it by hand from the repository root, under a memory cap::

    export DAEDALUS_BROWSERD_BIN=<path to a built browserd>
    export BROWSERD_CHROMIUM=<a Chromium, e.g. Playwright's chrome-linux64/chrome>
    export KEYPROXY_BASE_URL=http://<host>:<port>      # the key proxy, as the host reaches it
    systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 \\
      .venv/bin/python -m tests.browser_eval.run --models deepseek:deepseek-flash --out <dir>

Where the kernel refuses Chromium's sandbox to an unprivileged user (Ubuntu's AppArmor restriction
on user namespaces), the daemon says so on the first ``BrowserOpen``; run the same command in a
container that has a Chromium, as the browser daemon's own tests do: ``--security-opt
seccomp=unconfined``, ``--memory 6g``, the checkout mounted read-only, and ``BROWSERD_CHROMIUM``
pointing at the container's Chromium. The sandbox stays on either way.

``--probe`` needs no model: it opens every task's page and writes what ``BrowserSnapshot`` and
``BrowserText`` show under ``<dir>/probe``, which is how a fixture is checked after a change.

``--models`` takes ``<upstream>:<model>`` pairs, comma separated; the upstream is the key proxy's
name for the provider (``deepseek``, ``openrouter``, and the command-line subscriptions ``claude``,
``codex``, ``grok``). Keys never pass through here: the proxy holds them. ``--cap`` stops a model's
run when its spend reaches that many dollars. ``--tasks`` picks tasks by id; ``--repeats`` runs
each more than once. The results go to ``<dir>/results.json`` and ``<dir>/summary.md``, with each
episode's transcript under ``<dir>/transcripts``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from daedalus.browser.agent import BrowserAgent, Caller, SensitiveAsk
from daedalus.browser.model import Forbidden, Owner
from daedalus.browser.service import Browsers
from daedalus.config import BrowserConfig, ExecToolsConfig, keyproxy_base
from daedalus.host.prompts import PERSONA, group_instructions
from daedalus.stores.database import Database
from daedalus.tools._common import FRAME_CHARS, clip
from daedalus.tools.browser import TOOLS
from tests.browser_eval.site import Sites, State
from tests.browser_eval.tasks import BY_ID, TASKS, Outcome, Task

SUBSCRIPTIONS = ("claude", "codex", "grok")
"""Upstreams billed as a command-line subscription, not per token: their dollars are zero."""

PRICES = {
    # ($ per million input tokens missed by the cache, cached, output). OpenRouter reports what it
    # charged, so it needs no row; these are for providers that report tokens only.
    "deepseek:deepseek-flash": (0.30, 0.006, 1.20),
    "deepseek:deepseek-v4-pro": (0.60, 0.012, 2.40),
}

EVAL_RULES = (
    "This run is an evaluation: the browser tools are the only tools you have. Do the task in the browser; when it is "
    "done, or when you have the answer, reply with it in a sentence or two and stop. Do not ask questions back."
)

MODEL_TIMEOUT_S = 240.0
TASK_TIMEOUT_S = 900.0


# -- the tools as a session is given them -------------------------------------------------------------


def tool_schemas() -> list[dict[str, Any]]:
    """The browser tools exactly as the product declares them, in the chat-completions shape."""
    out = []
    for cls in TOOLS:
        definition = cls().definition
        params = definition.parameters
        schema: dict[str, Any] = {"type": "object", "properties": dict(params.properties), "required": list(params.required)}
        out.append({"type": "function", "function": {"name": definition.name, "description": definition.description, "parameters": schema}})
    return out


def system_prompt() -> str:
    return f"{PERSONA}\n{group_instructions('browser', selfdev_mode='off')}\n{EVAL_RULES}"


RESULT_LIMIT = ExecToolsConfig().max_output_chars
"""What one tool call returns at most, as a session with the default ``[tools.exec]`` has it."""


# -- what a tool result says went wrong ---------------------------------------------------------------


ERROR_KINDS = (
    ("no vision", ("no vision model",)),
    ("scheme refused", ("URLs are not opened in this browser",)),
    ("stale ref", ("is not on the page any more",)),
    ("covered", ("is covered by",)),
    ("dialog open", ("Answer it with BrowserDialog",)),
    ("timeout", ("timed out", "timeout", "did not happen")),
    ("not found", ("no such", "not found", "there is no", "no download named", "no size on the page")),
    ("invalid call", ("needs ", "is one of", "is required", "invalid JSON")),
    ("sensitive ask", ("asked the operator",)),
    ("network wall", ("network wall",)),
)


def error_kind(text: str) -> str:
    low = text.lower()
    for kind, marks in ERROR_KINDS:
        if any(m.lower() in low for m in marks):
            return kind
    return "other"


REF = re.compile(r"\[ref=([A-Za-z0-9]+)\]")


def invented(arguments: dict[str, Any], seen: set[str]) -> bool:
    """Whether a call named a ref no result ever showed: a selector, a guess, a ref of an element the
    outline did not list. The tools answer it as a stale ref, which is a different failure."""
    refs = [str(arguments.get(k)) for k in ("ref", "to_ref", "scope") if arguments.get(k)]
    return any(r not in seen for r in refs)


def truncated(text: str) -> bool:
    return "The outline was cut" in text or "The text was cut at" in text or "one call returns at most this much" in text


# -- the caller's world: its files, its operator ------------------------------------------------------


class Everyone:
    async def exists(self, owner: Owner) -> bool:
        return True

    async def label(self, owner: Owner) -> str:
        return "evaluation"


class Workspace:
    """An episode's workspace: uploads read from it, downloads land in it and are kept for the checker."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.saved: dict[str, bytes] = {}

    async def read(self, path: str) -> tuple[str, bytes]:
        target = (self.root / path).resolve()
        if self.root.resolve() not in target.parents or not target.is_file():
            raise Forbidden(f"{path} is not a file in your workspace")
        return target.name, target.read_bytes()

    async def save(self, name: str, data: bytes, to: str | None) -> str:
        target = (self.root / (to or f"downloads/{Path(name).name}")).resolve()
        if self.root.resolve() not in target.parents:
            raise Forbidden(f"{to} is outside your workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.saved[target.name] = data
        return f"Saved {target.name} ({len(data)} bytes) to {target.relative_to(self.root)}."


# -- the model ------------------------------------------------------------------------------------------


@dataclass
class Model:
    upstream: str
    name: str
    cap: float
    spent: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.upstream}:{self.name}"

    @property
    def url(self) -> str:
        base = keyproxy_base()
        if self.upstream in SUBSCRIPTIONS:
            return f"{base}/{self.upstream}/v1/chat/completions"
        return f"{base}/{self.upstream}/chat/completions"

    def body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.name, "messages": messages, "tools": tools, "max_tokens": 8000}
        if self.upstream == "openrouter":
            body["usage"] = {"include": True}
        return body

    def cost(self, usage: dict[str, Any]) -> float:
        if self.upstream in SUBSCRIPTIONS:
            return 0.0
        if usage.get("cost") is not None:
            return float(usage["cost"])
        price = PRICES.get(self.label)
        if price is None:
            return 0.0
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = cached_tokens(usage)
        return ((prompt - hit) * price[0] + hit * price[1] + int(usage.get("completion_tokens") or 0) * price[2]) / 1e6


def cached_tokens(usage: dict[str, Any]) -> int:
    if usage.get("prompt_cache_hit_tokens") is not None:
        return int(usage["prompt_cache_hit_tokens"])
    return int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)


class OutOfBudget(Exception):
    pass


async def complete(http: httpx.AsyncClient, model: Model, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """One chat completion, retried on the provider's transient answers."""
    if model.upstream not in SUBSCRIPTIONS and model.spent >= model.cap:
        raise OutOfBudget(f"{model.label} reached its cap of ${model.cap:.2f}")
    last = ""
    for attempt in range(4):
        try:
            # The key proxy reports a call it forwards to the running bot's ledger unless it is told
            # the caller meters it itself; an evaluation must not show up in the operator's usage.
            response = await http.post(model.url, json=model.body(messages, tools), headers={"x-daedalus-metered": "1"}, timeout=MODEL_TIMEOUT_S)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                data = response.json()
                if data.get("choices"):
                    return data
                last = f"no choices: {str(data)[:300]}"
            else:
                last = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code in (400, 401, 402, 403, 404):
                    break
        await asyncio.sleep(2 + attempt * 4)
    raise RuntimeError(f"{model.label}: {last}")


# -- one episode ----------------------------------------------------------------------------------------


@dataclass
class Episode:
    task: str
    model: str
    repeat: int
    success: bool = False
    why: str = ""
    answer: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    truncated_results: int = 0
    asks: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    stopped: str = ""
    """Why the loop ended when it was not the model's own final answer: steps, time, budget, error."""
    covers: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    """What the fixture's server saw, for reading a failure: the records and the fields posted."""


class Runner:
    def __init__(self, service: Browsers, sites: Sites, state: State, out: Path, max_steps: int) -> None:
        self.service = service
        self.agent = BrowserAgent(service)
        self.sites = sites
        self.state = state
        self.out = out
        self.max_steps = max_steps
        self.tools = tool_schemas()
        self.system = system_prompt()
        self.lock = asyncio.Lock()

    async def episode(self, http: httpx.AsyncClient, model: Model, task: Task, repeat: int) -> Episode:
        # Each episode is its own owner, so it has its own profile: no cookie, consent or tab carries
        # over from the task before it.
        owner_id = f"eval-{task.id}-{model.upstream}-{repeat}-{int(time.time() * 1000) % 10_000_000}"
        owner = Owner("session", owner_id, session_id=owner_id)
        work = Path(tempfile.mkdtemp(prefix="beval-ws-"))
        files = Workspace(work)
        result = Episode(task.id, model.label, repeat, covers=list(task.covers))

        async def gate(ask: SensitiveAsk) -> tuple[bool, str]:
            # The operator says yes to everything: the evaluation measures how the agent gets the task
            # done, and counts the questions a real run would have put to the operator.
            result.asks.append(",".join(ask.kinds) or ask.decision.rule)
            return True, ""

        caller = Caller(owner=owner, actor=f"agent:{owner_id}", gate=gate, files=files, look=None, budget=max(4000, RESULT_LIMIT - FRAME_CHARS - 600))
        self.state.forget(task.id)
        url = f"{self.sites.main}/{task.page}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": f"{task.instruction}\n\nThe site is at {url}"},
        ]
        tools_used: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        seen: set[str] = set()
        transcript = self.out / "transcripts" / f"{model.upstream}-{model.name.replace('/', '_')}" / f"{task.id}-{repeat}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        log = transcript.open("w")
        started = time.monotonic()
        try:
            async with asyncio.timeout(TASK_TIMEOUT_S):
                for _ in range(self.max_steps):
                    data = await complete(http, model, messages, self.tools)
                    usage = data.get("usage") or {}
                    cost = model.cost(usage)
                    async with self.lock:
                        model.spent += cost
                    result.cost += cost
                    result.model_calls += 1
                    result.tokens_in += int(usage.get("prompt_tokens") or 0)
                    result.tokens_cached += cached_tokens(usage)
                    result.tokens_out += int(usage.get("completion_tokens") or 0)
                    result.tokens_reasoning += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
                    message = data["choices"][0].get("message") or {}
                    calls = message.get("tool_calls") or []
                    kept: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
                    if message.get("reasoning_content"):
                        # DeepSeek's thinking mode refuses a follow-up whose assistant turn lost it.
                        kept["reasoning_content"] = message["reasoning_content"]
                    if calls:
                        kept["tool_calls"] = calls
                    messages.append(kept)
                    log.write(json.dumps({"assistant": kept, "usage": usage}) + "\n")
                    if not calls:
                        result.answer = str(message.get("content") or "")
                        break
                    for call in calls:
                        name = str((call.get("function") or {}).get("name") or "")
                        raw = (call.get("function") or {}).get("arguments") or "{}"
                        arguments: Any = None
                        try:
                            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                            if not isinstance(arguments, dict):
                                raise ValueError("not an object")
                        except ValueError:
                            text, failed = f"{name}: invalid JSON arguments: {str(raw)[:200]}", True
                        else:
                            text, failed = await self.agent.run(name, {k: v for k, v in arguments.items() if v is not None}, caller)
                        text = clip(text, RESULT_LIMIT, note="one call returns at most this much")
                        tools_used[name] += 1
                        result.tool_calls += 1
                        if failed:
                            kind = error_kind(text)
                            if kind == "stale ref" and isinstance(arguments, dict):
                                if invented(arguments, seen):
                                    kind = "invented ref"
                                elif any(re.fullmatch(r"f\d+", str(arguments.get(k) or "")) for k in ("ref", "scope")):
                                    # The outline lists another site's frame as f<k> and no tool takes it.
                                    kind = "frame ref"
                            errors[kind] += 1
                        seen.update(REF.findall(text))
                        if truncated(text):
                            result.truncated_results += 1
                        messages.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": text})
                        log.write(json.dumps({"tool": name, "arguments": raw, "failed": failed, "result": text}) + "\n")
                else:
                    result.stopped = f"used all {self.max_steps} steps"
        except TimeoutError:
            result.stopped = f"took longer than {TASK_TIMEOUT_S:.0f} s"
        except OutOfBudget as exc:
            result.stopped = str(exc)
        except Exception as exc:  # noqa: BLE001 — one episode's failure is its row, not the run's end
            result.stopped = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            log.close()
            result.seconds = round(time.monotonic() - started, 1)
            await self.close_browsers(owner)
            # A page's last fetch may still be on its way when the model answers.
            await asyncio.sleep(0.3)
            submissions, events = self.state.of(task.id)
            outcome = Outcome(answer=result.answer, submissions=submissions, events=events, saved=dict(files.saved))
            result.success, result.why = task.check(outcome)
            result.seen = [f"{e['name']} {json.dumps(e['data'], ensure_ascii=False)}" for e in events] + [f"POST {json.dumps(p, ensure_ascii=False)}" for p in submissions]
            if result.stopped and not result.success:
                result.why = f"{result.stopped}; {result.why}"
            result.tools = dict(tools_used)
            result.errors = dict(errors)
            result.cost = round(result.cost, 6)
            shutil.rmtree(work, ignore_errors=True)
        return result

    async def close_browsers(self, owner: Owner) -> None:
        """Close the episode's groups and the Chromium behind them. A closed group leaves its browser
        running until the idle close, and the next episode's own profile would find every browser the
        cap allows still busy."""
        rows = await self.service.db.fetchall("SELECT id, status, env, browser_id FROM browser_groups WHERE owner_kind = ? AND owner_id = ?", (owner.kind, owner.id))
        for row in rows:
            if row["status"] == "open":
                with contextlib.suppress(Exception):
                    await self.service.close_group(row["id"], actor="system", reason="closed")
            if row["browser_id"]:
                with contextlib.suppress(Exception):
                    await self.service.close_browser(row["env"], row["browser_id"], actor="system")


# -- the report -----------------------------------------------------------------------------------------


def summary(episodes: list[Episode], models: list[Model], tasks: list[Task], started: str) -> str:
    lines = [f"# Browser evaluation — {started}", ""]
    lines.append("| model | success | tool calls / task | model calls / task | tokens in (cached) / out per task | cost | wall s / task |")
    lines.append("|---|---|---|---|---|---|---|")
    for model in models:
        mine = [e for e in episodes if e.model == model.label]
        if not mine:
            continue
        n = len(mine)
        ok = sum(e.success for e in mine)
        lines.append(
            f"| {model.label} | {ok}/{n} ({ok / n:.0%}) | {sum(e.tool_calls for e in mine) / n:.1f} | {sum(e.model_calls for e in mine) / n:.1f} | "
            f"{sum(e.tokens_in for e in mine) / n / 1000:.1f}k ({sum(e.tokens_cached for e in mine) / n / 1000:.1f}k) / {sum(e.tokens_out for e in mine) / n / 1000:.1f}k | "
            f"${sum(e.cost for e in mine):.3f} | {sum(e.seconds for e in mine) / n:.0f} |"
        )
    lines += ["", "## Error kinds in tool results", "", "| model | " + " | ".join(k for k, _ in ERROR_KINDS) + " | invented ref | frame ref | other | truncated | operator asks |", "|---" * (len(ERROR_KINDS) + 7) + "|"]
    for model in models:
        mine = [e for e in episodes if e.model == model.label]
        if not mine:
            continue
        total: Counter[str] = Counter()
        for e in mine:
            total.update(e.errors)
        lines.append(f"| {model.label} | " + " | ".join(str(total.get(k, 0)) for k, _ in ERROR_KINDS) + f" | {total.get('invented ref', 0)} | {total.get('frame ref', 0)} | {total.get('other', 0)} | {sum(e.truncated_results for e in mine)} | {sum(len(e.asks) for e in mine)} |")
    labels = [m.label for m in models if any(e.model == m.label for e in episodes)]
    lines += ["", "## Per task", "", "| task | " + " | ".join(labels) + " | points at |", "|---" * (len(labels) + 2) + "|"]
    for task in tasks:
        cells = []
        for label in labels:
            mine = [e for e in episodes if e.model == label and e.task == task.id]
            cells.append(" ".join(("✓" if e.success else "✗") + f"{e.tool_calls}" for e in mine) or "—")
        lines.append(f"| {task.id} | " + " | ".join(cells) + f" | {'; '.join(task.covers)} |")
    lines += ["", "## Failures", ""]
    for e in episodes:
        if not e.success:
            errs = ", ".join(f"{k}×{v}" for k, v in e.errors.items()) or "no tool errors"
            lines.append(f"- **{e.task}** · {e.model} · {e.tool_calls} calls · {errs} — {e.why}. Answer: {e.answer[:200]!r}")
    return "\n".join(lines) + "\n"


async def probe(runner: Runner, tasks: list[Task], out: Path) -> None:
    """What the agent sees first on each task's page, without a model: the fixtures' own check."""
    owner = Owner("session", "eval-probe", session_id="eval-probe")

    async def gate(ask: SensitiveAsk) -> tuple[bool, str]:
        return True, ""

    caller = Caller(owner=owner, actor="agent:eval-probe", gate=gate, files=Workspace(out), budget=max(4000, RESULT_LIMIT - FRAME_CHARS - 600))
    pages = out / "probe"
    pages.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        opened, failed = await runner.agent.run("BrowserOpen", {"url": f"{runner.sites.main}/{task.page}"}, caller)
        if failed:
            print(f"{task.id:18} could not open: {opened}")
            continue
        await asyncio.sleep(1.0)
        snapshot, _ = await runner.agent.run("BrowserSnapshot", {}, caller)
        text, _ = await runner.agent.run("BrowserText", {}, caller)
        (pages / f"{task.id}.txt").write_text(f"{snapshot}\n\n--- BrowserText ---\n{text}\n")
        print(f"{task.id:18} snapshot {len(snapshot):6} chars, text {len(text):6} chars{' (cut)' if truncated(snapshot) or truncated(text) else ''}")
    await runner.close_browsers(owner)


# -- the whole run --------------------------------------------------------------------------------------


def free_port() -> int:
    """A free port, never one of the range the agent hands its own preview servers."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not 8100 <= port <= 8119:
            return port


async def start_daemon(binary: str, base: Path) -> tuple[asyncio.subprocess.Process, Path]:
    run, state = base / "run", base / "state"
    log = (base / "browserd.log").open("wb")
    proc = await asyncio.create_subprocess_exec(binary, "serve", "--env", "host", "--run-dir", str(run), "--state-dir", str(state), stdout=log, stderr=log)
    async with asyncio.timeout(30):
        while not (run / "endpoint").is_file():
            if proc.returncode is not None:
                raise RuntimeError(f"browserd exited with {proc.returncode}; see {base / 'browserd.log'}")
            await asyncio.sleep(0.05)
    return proc, run


async def deepseek_balance(http: httpx.AsyncClient) -> str:
    with contextlib.suppress(Exception):
        data = (await http.get(f"{keyproxy_base()}/deepseek/user/balance", timeout=20)).json()
        return str((data.get("balance_infos") or [{}])[0].get("total_balance"))
    return "?"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", default="", help="upstream:model pairs, comma separated")
    parser.add_argument("--probe", action="store_true", help="no model: open each task's page and write what BrowserSnapshot shows")
    parser.add_argument("--out", required=True, type=Path, help="where results.json, summary.md and the transcripts go")
    parser.add_argument("--tasks", default="", help="task ids, comma separated (default: all)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cap", type=float, default=1.0, help="dollars each model may spend")
    parser.add_argument("--concurrency", type=int, default=2, help="episodes at once per model; the daemon runs two browsers by default")
    parser.add_argument("--max-steps", type=int, default=30, help="model calls an episode may make")
    args = parser.parse_args(argv)

    binary = os.environ.get("DAEDALUS_BROWSERD_BIN", "")
    if not binary or not Path(binary).is_file():
        print("set DAEDALUS_BROWSERD_BIN to a built browserd", file=sys.stderr)
        return 2
    tasks = [BY_ID[t] for t in args.tasks.split(",") if t] if args.tasks else list(TASKS)
    if not args.models and not args.probe:
        print("name --models, or --probe to look at the pages without a model", file=sys.stderr)
        return 2
    models = []
    for pair in [p for p in args.models.split(",") if p]:
        upstream, _, name = pair.partition(":")
        models.append(Model(upstream.strip(), name.strip(), cap=args.cap))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M")

    state = State()
    sites = Sites(state)
    sites.start(free_port(), free_port())
    base = Path(tempfile.mkdtemp(prefix="beval-"))
    proc, run = await start_daemon(binary, base)
    db = Database(base / "state.db")
    await db.open()
    # The fixture's two ports are let through as the agent's services, as the integration tests do;
    # every other port of this machine is still asked about, as it is natively.
    rules = {"sealed_ports": [], "services_ports": [[p, p] for p in sites.ports], "local_sites": "ask", "lan_allow": []}
    service = Browsers(db, run_dirs={"container": None, "host": run}, config=lambda: BrowserConfig(env="host", running_cap=max(2, args.concurrency)), owners=Everyone(), wall=lambda env: rules)  # type: ignore[arg-type]
    await service.start()
    episodes: list[Episode] = []
    try:
        if not await service.wait_available("host", timeout=30):
            print("the browser daemon never became available", file=sys.stderr)
            return 1
        runner = Runner(service, sites, state, out, args.max_steps)
        if args.probe:
            await probe(runner, tasks, out)
            return 0
        async with httpx.AsyncClient() as http:
            before = await deepseek_balance(http) if any(m.upstream == "deepseek" for m in models) else ""
            for model in models:
                limit = asyncio.Semaphore(max(1, args.concurrency))

                async def one(task: Task, repeat: int, model: Model = model, limit: asyncio.Semaphore = limit) -> Episode:
                    async with limit:
                        episode = await runner.episode(http, model, task, repeat)
                    mark = "ok  " if episode.success else "FAIL"
                    print(f"{mark} {model.label:40} {task.id:18} calls={episode.tool_calls:2} ${episode.cost:.4f} {episode.seconds:5.0f}s {'' if episode.success else episode.why[:110]}", flush=True)
                    return episode

                episodes += await asyncio.gather(*(one(t, r) for r in range(args.repeats) for t in tasks))
                (out / "results.json").write_text(json.dumps({"started": started, "episodes": [asdict(e) for e in episodes]}, indent=1, ensure_ascii=False))
            after = await deepseek_balance(http) if before else ""
    finally:
        await service.close()
        await db.close()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 10)
        sites.close()
        shutil.rmtree(base, ignore_errors=True)

    spend = {m.label: round(m.spent, 4) for m in models}
    report = summary(episodes, models, tasks, started)
    if before:
        report += f"\nDeepSeek balance before ${before}, after ${after}.\n"
    report += f"\nSpend by model: {json.dumps(spend)}\n"
    (out / "results.json").write_text(json.dumps({"started": started, "spend": spend, "deepseek_balance": [before, after], "episodes": [asdict(e) for e in episodes]}, indent=1, ensure_ascii=False))
    (out / "summary.md").write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
