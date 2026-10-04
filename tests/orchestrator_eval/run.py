"""Replay recorded orchestrator episodes against a real model and score what it did.

Each scenario (``scenarios.py``) is an episode of a real project, rewritten about an invented one: the
project's state as it was, the recent turns, and what woke the orchestrator — an operator's message or
a batch of the team's events. The stand (``stand.py``) is the product's orchestrator of that project:
its standing brief (``prompts.orchestrator_sections``), its tools as they are declared, the state block
and the wake-up lines rendered by the product, every tool call carried out by the product's
operations on a database of its own. Only the loop around the model is not the product: a plain
chat-completions loop, the way ``tests/browser_eval`` drives its models, because the session runner
would add compaction, budgets and a transcript that say nothing about the orchestrator's judgement.

A turn ends when the model answers without a tool call or calls StaySilent. Then the host does what
it does at the end of an orchestrator's turn in the build under test, and whatever that publishes
which would wake the orchestrator starts the next turn, up to the scenario's number of turns. That is
how "decides in the same turn or the next one" is measured.

The checks are deterministic: they read the tool calls and the board, the requirements, the
acceptance and the open results the episode left behind. No model judges another. Sampling is the
provider's default (not greedy), so a scenario runs a few times and the pass rate is the result.

This is not part of the test suite and never runs in it: it spends money and reaches the model
providers. Run it by hand from the repository root, under a memory cap::

    export KEYPROXY_BASE_URL=http://<host>:<port>      # the key proxy, as this machine reaches it
    systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \\
      .venv/bin/python -m tests.orchestrator_eval.run --models codex:gpt-6-luna,deepseek:deepseek-flash \\
      --repeats 3 --out <dir>

``--probe`` needs no model: it plays every scenario's setup and writes the first message the
orchestrator would read — the wake-up, the operator's words, the state block — under ``<dir>/probe``,
which is how a scenario is checked after a change to it or to the product.

``--models`` takes ``<upstream>:<model>`` pairs, as ``tests/browser_eval`` does; ``--scenarios`` picks
some by id; ``--cap`` stops a model's run at that many dollars; ``--parallel`` is how many episodes run
at once. The results go to ``<dir>/results.json`` and ``<dir>/summary.md``, each episode's transcript
under ``<dir>/transcripts``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from protocore.contracts.tools import ToolContext

from daedalus.config import keyproxy_base
from daedalus.host import prompts
from daedalus.tools.orchestrator import TOOLS
from daedalus.tools.quiet import stay_silent
from tests.orchestrator_eval.scenarios import BY_ID, SCENARIOS, Call, Record, Scenario
from tests.orchestrator_eval.stand import Stand

SUBSCRIPTIONS = ("claude", "codex", "grok")
"""Upstreams with subscription access whose cost cannot be attributed to one replay."""

PRICES = {
    # ($ per million input tokens missed by the cache, cached, output), for providers that report tokens only.
    "deepseek:deepseek-flash": (0.30, 0.006, 1.20),
    "deepseek:deepseek-v4-pro": (0.60, 0.012, 2.40),
    "opencode:deepseek-v4.1-flash": (0.30, 0.006, 1.20),
}

MODEL_TIMEOUT_S = 240.0
EPISODE_TIMEOUT_S = 900.0
MAX_STEPS = 16
"""Model calls in one turn at most: an orchestrator that needs more for one wake-up is lost."""
RESULT_LIMIT = 20_000


# -- the tools as an orchestrator is given them ---------------------------------------------------------


def tools() -> dict[str, Any]:
    """The orchestrator's tools by name, as the product declares them, and StaySilent."""
    found = {}
    for made in [*TOOLS, stay_silent]:
        instance = made()
        found[instance.name] = instance
    return found


def schemas(named: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for instance in named.values():
        definition = instance.definition
        params = definition.parameters
        schema: dict[str, Any] = {"type": "object", "properties": dict(params.properties), "required": list(params.required)}
        out.append({"type": "function", "function": {"name": definition.name, "description": definition.description, "parameters": schema}})
    return out


def system_prompt() -> str:
    return "\n".join(prompts.orchestrator_sections(answer_language="auto", governance=""))


# -- the model ---------------------------------------------------------------------------------------------


@dataclass
class Model:
    upstream: str
    name: str
    cap: float
    spent: float = 0.0
    unknown_cost: bool = False
    session: str = field(default_factory=lambda: uuid.uuid4().hex)

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

    def headers(self) -> dict[str, str]:
        headers = {"x-daedalus-metered": "1"}
        if self.upstream == "opencode":
            # The OpenCode gateway routes by a session of the caller's; one per model run is enough.
            headers["x-opencode-session"] = self.session
        return headers

    def cost(self, usage: dict[str, Any]) -> tuple[float | None, str]:
        if self.upstream in SUBSCRIPTIONS:
            return None, "subscription"
        if usage.get("cost") is not None:
            return float(usage["cost"]), "provider_reported"
        price = PRICES.get(self.label)
        if price is None or usage.get("prompt_tokens") is None or usage.get("completion_tokens") is None:
            return None, "unpriced"
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = cached_tokens(usage)
        return ((prompt - hit) * price[0] + hit * price[1] + int(usage.get("completion_tokens") or 0) * price[2]) / 1e6, "local_estimate"


def cached_tokens(usage: dict[str, Any]) -> int:
    if usage.get("prompt_cache_hit_tokens") is not None:
        return int(usage["prompt_cache_hit_tokens"])
    return int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)


class OutOfBudget(Exception):
    pass


async def complete(http: httpx.AsyncClient, model: Model, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """One chat completion, retried on the provider's transient answers."""
    if model.upstream not in SUBSCRIPTIONS and model.unknown_cost:
        raise OutOfBudget(f"{model.label} has unknown cost; its spend cap cannot be checked")
    if model.upstream not in SUBSCRIPTIONS and model.spent >= model.cap:
        raise OutOfBudget(f"{model.label} reached its cap of ${model.cap:.2f}")
    last = ""
    for attempt in range(4):
        try:
            # The key proxy reports a call it forwards to the running bot's ledger unless it is told the
            # caller meters it itself; an evaluation must not show up in the operator's usage.
            response = await http.post(model.url, json=model.body(messages, tools), headers=model.headers(), timeout=MODEL_TIMEOUT_S)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                data = response.json()
                if data.get("choices"):
                    return data  # type: ignore[no-any-return]
                last = f"no choices: {str(data)[:300]}"
            else:
                last = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code in (400, 401, 402, 403, 404):
                    break
        await asyncio.sleep(2 + attempt * 4)
    raise RuntimeError(f"{model.label}: {last}")


# -- one episode --------------------------------------------------------------------------------------------


@dataclass
class Episode:
    scenario: str
    model: str
    repeat: int
    success: bool = False
    why: str = ""
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    refused: int = 0
    asked_operator: int = 0
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    cost: float | None = None
    cost_basis: str = "not_observed"
    seconds: float = 0.0
    stopped: str = ""

    def record_cost(self, cost: float | None, basis: str) -> None:
        """Keep an unknown call from turning into a priced total on a later response."""
        if self.model_calls == 0:
            self.cost = cost
            self.cost_basis = basis
        else:
            self.cost = None if cost is None or self.cost is None else self.cost + cost
            if basis == "unpriced":
                self.cost_basis = "unpriced"
            elif self.cost_basis != basis and self.cost_basis != "unpriced":
                self.cost_basis = "mixed"
        self.model_calls += 1


class Runner:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.named = tools()
        self.schemas = schemas(self.named)
        self.system = system_prompt()
        self.lock = asyncio.Lock()

    async def call_tool(self, stand: Stand, call_id: str, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name == "StaySilent":
            return "ok: nothing is sent", False
        instance = self.named.get(name)
        if instance is None:
            return f"unknown tool {name}", True
        context = ToolContext(tenant_id="eval", run_id="eval", session_id=stand.session_id, metadata={"tool_call_id": call_id})
        try:
            result = await instance.invoke(context, arguments)
        except Exception as exc:  # noqa: BLE001 — a tool that raises is a failed call, as the engine reports it
            return f"{name} failed: {type(exc).__name__}: {exc}", True
        text = str(result.content)
        if len(text) > RESULT_LIMIT:
            text = text[:RESULT_LIMIT] + "\n[cut]"
        return text, bool(result.is_error)

    async def episode(self, http: httpx.AsyncClient, model: Model, scenario: Scenario, repeat: int) -> Episode:
        result = Episode(scenario.id, model.label, repeat)
        if model.upstream in SUBSCRIPTIONS:
            result.cost_basis = "subscription"
        root = Path(tempfile.mkdtemp(prefix="oeval-"))
        transcript = self.out / "transcripts" / model.label.replace("/", "_").replace(":", "-") / f"{scenario.id}-{repeat}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        log = transcript.open("w")
        record = Record()
        started = time.monotonic()
        stand: Stand | None = None
        tools_used: Counter[str] = Counter()
        try:
            stand = await Stand.open(root)
            opening = await scenario.setup(stand)
            news = await stand.news()
            first = "\n\n".join(part for part in (news, opening.message) if part)
            messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
            for role, text in opening.history:
                messages.append({"role": role, "content": text})
            pending = first
            steered = False
            async with asyncio.timeout(EPISODE_TIMEOUT_S):
                while pending and result.turns < scenario.turns:
                    result.turns += 1
                    turn_started = datetime.now(UTC).isoformat()
                    messages.append({"role": "user", "content": pending + "\n\n" + await stand.turn_context()})
                    log.write(json.dumps({"turn": result.turns, "user": messages[-1]["content"]}, ensure_ascii=False) + "\n")
                    silent = False
                    for _ in range(MAX_STEPS):
                        data = await complete(http, model, messages, self.schemas)
                        usage = data.get("usage") or {}
                        cost, basis = model.cost(usage)
                        async with self.lock:
                            if basis == "unpriced":
                                model.unknown_cost = True
                            if cost is not None:
                                model.spent += cost
                        result.record_cost(cost, basis)
                        result.tokens_in += int(usage.get("prompt_tokens") or 0)
                        result.tokens_cached += cached_tokens(usage)
                        result.tokens_out += int(usage.get("completion_tokens") or 0)
                        message = data["choices"][0].get("message") or {}
                        calls = message.get("tool_calls") or []
                        kept: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
                        if message.get("reasoning_content"):
                            # DeepSeek's thinking mode refuses a follow-up whose assistant turn lost it.
                            kept["reasoning_content"] = message["reasoning_content"]
                        if calls:
                            kept["tool_calls"] = calls
                        messages.append(kept)
                        log.write(json.dumps({"assistant": kept, "usage": usage}, ensure_ascii=False) + "\n")
                        if kept["content"]:
                            record.texts.append((result.turns, str(kept["content"])))
                        if not calls:
                            break
                        steer = opening.steer if result.turns == 1 and not steered else ""
                        for call in calls:
                            name = str((call.get("function") or {}).get("name") or "")
                            raw = (call.get("function") or {}).get("arguments") or "{}"
                            try:
                                arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                                if not isinstance(arguments, dict):
                                    raise ValueError("not an object")
                            except ValueError:
                                text, failed, arguments = f"{name}: invalid JSON arguments: {str(raw)[:200]}", True, {}
                            else:
                                arguments = {k: v for k, v in arguments.items() if v is not None}
                                text, failed = await self.call_tool(stand, str(call.get("id") or ""), name, arguments)
                            tools_used[name] += 1
                            result.tool_calls += 1
                            result.refused += int(failed)
                            if name == "AskOperator" and not failed:
                                result.asked_operator += len(arguments.get("questions") or []) or 1
                            silent = silent or name == "StaySilent"
                            record.calls.append(Call(result.turns, name, arguments, text, failed))
                            messages.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": text})
                            log.write(json.dumps({"tool": name, "arguments": arguments, "failed": failed, "result": text}, ensure_ascii=False) + "\n")
                        if steer:
                            # The operator wrote while the turn was under way: the core puts the
                            # message before the next model call.
                            steered = True
                            messages.append({"role": "user", "content": await stand.steered(steer)})
                            log.write(json.dumps({"steer": messages[-1]["content"]}, ensure_ascii=False) + "\n")
                        if silent:
                            break
                    else:
                        result.stopped = f"turn {result.turns} used all {MAX_STEPS} steps"
                    await stand.turn_ended(turn_started)
                    pending = await stand.news()
                    if opening.steer and not steered:
                        # The turn ended before the message could be placed: it opens the next one.
                        steered = True
                        pending = "\n\n".join(part for part in (pending, opening.steer) if part)
        except TimeoutError:
            result.stopped = f"took longer than {EPISODE_TIMEOUT_S:.0f} s"
        except OutOfBudget as exc:
            result.stopped = str(exc)
        except Exception as exc:  # noqa: BLE001 — one episode's failure is its row, not the run's end
            result.stopped = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            log.close()
            result.seconds = round(time.monotonic() - started, 1)
            if stand is not None:
                try:
                    record.delivered = await stand.delivered()
                    result.success, result.why = await scenario.check(stand, record)
                except Exception as exc:  # noqa: BLE001 — a check that cannot read the result fails it
                    result.success, result.why = False, f"the check failed: {type(exc).__name__}: {exc}"
                with open(transcript, "a") as tail:
                    tail.write(json.dumps({"check": result.success, "why": result.why}, ensure_ascii=False) + "\n")
                await stand.close()
            if result.stopped and not result.success:
                result.why = f"{result.stopped}; {result.why}"
            result.tools = dict(tools_used)
            if result.cost is not None:
                result.cost = round(result.cost, 6)
            shutil.rmtree(root, ignore_errors=True)
        return result


async def probe(scenarios: list[Scenario], out: Path) -> None:
    target = out / "probe"
    target.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        root = Path(tempfile.mkdtemp(prefix="oeval-"))
        stand = await Stand.open(root)
        try:
            opening = await scenario.setup(stand)
            news = await stand.news()
            first = "\n\n".join(part for part in (news, opening.message) if part)
            history = "".join(f"[{role}] {text}\n\n" for role, text in opening.history)
            (target / f"{scenario.id}.txt").write_text(f"{history}[user] {first}\n\n{await stand.turn_context()}\n")
            print(f"{scenario.id}: {len(first)} characters of trigger", flush=True)
        finally:
            await stand.close()
            shutil.rmtree(root, ignore_errors=True)


# -- the run --------------------------------------------------------------------------------------------------


def parse_models(text: str, cap: float) -> list[Model]:
    models = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        upstream, _, name = part.partition(":")
        if not name:
            raise SystemExit(f"--models takes <upstream>:<model> pairs, not {part!r}")
        models.append(Model(upstream, name, cap))
    return models


def summary(results: list[Episode], models: list[Model], scenarios: list[Scenario]) -> str:
    lines = ["# Orchestrator replay", "", f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC", ""]
    header = "| scenario | " + " | ".join(m.label for m in models) + " |"
    lines += [header, "|---|" + "---|" * len(models)]
    for scenario in scenarios:
        row = [f"{scenario.id}{' (counterexample)' if scenario.counterexample else ''}"]
        for model in models:
            mine = [r for r in results if r.scenario == scenario.id and r.model == model.label]
            row.append(f"{sum(r.success for r in mine)}/{len(mine)}" if mine else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "| model | passed | turns/episode | tool calls/episode | refused calls | operator questions | tokens in / out | cost |", "|---|---|---|---|---|---|---|---|"]
    for model in models:
        mine = [r for r in results if r.model == model.label]
        if not mine:
            continue
        n = len(mine)
        if model.upstream in SUBSCRIPTIONS:
            total_cost = "SUBSCRIPTION (per-replay USD unavailable)"
        elif any(r.cost is None for r in mine):
            total_cost = "UNKNOWN"
        else:
            sources = {r.cost_basis for r in mine}
            basis = ("provider reported" if sources == {"provider_reported"} else
                     "local estimate" if sources == {"local_estimate"} else "mixed reported and estimated")
            total_cost = f"${sum(r.cost or 0 for r in mine):.3f} ({basis})"
        lines.append(
            f"| {model.label} | {sum(r.success for r in mine)}/{n} | {sum(r.turns for r in mine) / n:.2f} | {sum(r.tool_calls for r in mine) / n:.1f} | "
            f"{sum(r.refused for r in mine)} | {sum(r.asked_operator for r in mine)} | {sum(r.tokens_in for r in mine) // 1000}k / {sum(r.tokens_out for r in mine) // 1000}k | {total_cost} |"
        )
    lines += ["", "Local estimates use the rate table in this runner; verify its prices before comparing providers.",
              "", "## Failures", ""]
    for r in results:
        if not r.success:
            lines.append(f"- {r.scenario} · {r.model} · #{r.repeat}: {r.why}")
    return "\n".join(lines) + "\n"


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--models", default="")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--scenarios", default="")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cap", type=float, default=1.0)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("daedalus").setLevel(logging.WARNING)
    wanted = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in BY_ID]
    if unknown:
        raise SystemExit(f"no scenario {', '.join(unknown)}; there are {', '.join(BY_ID)}")
    scenarios = [BY_ID[s] for s in wanted] if wanted else list(SCENARIOS)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.probe:
        await probe(scenarios, out)
        return 0
    models = parse_models(args.models, args.cap)
    if not models:
        raise SystemExit("--models names at least one <upstream>:<model>, or --probe runs no model")
    runner = Runner(out)
    gate = asyncio.Semaphore(max(1, args.parallel))
    results: list[Episode] = []

    async def one(http: httpx.AsyncClient, model: Model, scenario: Scenario, repeat: int) -> None:
        async with gate:
            episode = await runner.episode(http, model, scenario, repeat)
        results.append(episode)
        mark = "pass" if episode.success else "FAIL"
        print(f"{mark} {scenario.id} {model.label} #{repeat} ({episode.turns} turns, {episode.tool_calls} calls, {episode.seconds}s): {episode.why}", flush=True)

    async with httpx.AsyncClient() as http:
        await asyncio.gather(*(one(http, m, s, r) for r in range(1, args.repeats + 1) for m in models for s in scenarios))
    results.sort(key=lambda r: (r.model, r.scenario, r.repeat))
    (out / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=1, ensure_ascii=False))
    (out / "summary.md").write_text(summary(results, models, scenarios))
    print((out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
