"""A live link's visitor's turn: the link's policy chooses its models, its calls count against the link's
limit, and a spent limit stops it; the operator's own turns in the same session are untouched.

The ``policy``, ``spent_usd`` and ``spent_weekly_pct`` columns of ``session_shares`` are added here by
hand (:func:`_link`), because the migration that adds them lands beside this code rather than with it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import Session

from daedalus.config import ProviderConfig, Settings, keyproxy_base
from daedalus.host import share_roles, share_spend, share_turns
from daedalus.host.session_runner import SessionManager
from daedalus.providers.accounts import AccountRouter
from daedalus.providers.openai_compat import UsageRecord
from daedalus.stores.database import Database
from daedalus.stores.sqlite import SqliteSessionStore
from tests.support.models import DEFAULT_PRESET, FALLBACK_PRESET, VISION_PRESET, model_config
from tests.unit.test_session_runner import ScriptedProvider, _wait_finished


async def _link(db: Database, session_id: str, share_id: str, policy: dict[str, Any], *, spent_usd: float = 0.0) -> None:
    present = {row[1] for row in await db.fetchall("PRAGMA table_info(session_shares)")}
    for name, kind in (("policy", "TEXT NOT NULL DEFAULT '{}'"), ("spent_usd", "REAL NOT NULL DEFAULT 0"), ("spent_weekly_pct", "REAL NOT NULL DEFAULT 0")):
        if name not in present:
            await db.execute(f"ALTER TABLE session_shares ADD COLUMN {name} {kind}")
    await db.execute("INSERT INTO session_shares(id, session_id, token, created_at, policy, spent_usd) VALUES (?,?,?,?,?,?)",
                     (share_id, session_id, f"token-{share_id}", datetime.now(UTC).isoformat(), json.dumps(policy), spent_usd))


async def _session(db: Database, session_id: str) -> None:
    await SqliteSessionStore(db).create(Session(id=session_id, tenant_id="t", title="x"))


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider, asked: list[str | None]) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()

    def rungs_for(config: Any, preset_id: str | None = None) -> list[Any]:
        asked.append(preset_id)
        return [(provider, "scripted-model")]

    manager.providers.rungs_for = rungs_for  # type: ignore[method-assign]
    return manager


# -- the policy's reading ---------------------------------------------------------------


def test_a_rule_allows_its_value_and_its_entries_and_nothing_else() -> None:
    rule = {"preset": "a", "effort": "low", "account": "", "allowed": [{"preset": "b", "accounts": [], "efforts": ["high"]}]}
    assert share_turns.allows(rule, "a", "low", "")
    assert share_turns.allows(rule, "b", "high", "")
    assert share_turns.allows(rule, "b", "high", "any-account")
    assert not share_turns.allows(rule, "b", "xhigh", "")
    assert not share_turns.allows(rule, "c", "low", "")
    assert not share_turns.allows({**rule, "allowed": []}, "b", "high", "")
    # A pick the policy no longer allows falls back to the value at once.
    assert share_turns.choice(rule, {"preset": "b", "effort": "high"})["preset"] == "b"
    assert share_turns.choice(rule, {"preset": "c", "effort": "high"}) == {"preset": "a", "effort": "low", "account": ""}


def test_the_choice_is_put_over_the_session_overrides() -> None:
    presets = {"a": object()}
    assert share_turns.applied({"preset": "x", "provider": "p"}, {"preset": "a", "effort": "high", "account": ""}, presets) == {
        "preset": "a", "provider": None, "model_name": None, "thinking_enabled": True, "reasoning_effort": "high"}
    assert share_turns.applied({}, {"preset": "gone", "effort": "none", "account": ""}, presets) == {"thinking_enabled": False, "reasoning_effort": None}
    assert share_turns.role_of({}) == "main"
    assert share_turns.role_of({"subagent_of": "s"}) == "subagents"
    assert share_turns.role_of({"subagent_of": "s", "quick_agent": {"call": "c"}}) == "quick"


async def test_the_request_mark_is_used_up_by_the_run_it_starts() -> None:
    with share_turns.visiting("s1"):
        assert share_turns.starting_share(None) == "s1"
        # The run's task copies the context after this point; a run it starts from the queue is not the visitor's.
        assert share_turns.starting_share(None) == ""
    assert share_turns.starting_share({"daedalus.visitor": {"share": "s2", "name": "v"}}) == "s2"
    assert share_turns.starting_share({}) == ""


# -- the turn --------------------------------------------------------------------------


async def test_a_visitor_turn_runs_on_the_link_and_the_operator_turn_does_not(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "for the visitor"}, {"text": "for the operator"}])
    asked: list[str | None] = []
    manager = await _manager(settings, db, provider, asked)
    sid = (await manager.create_session("shared")).session.id
    await _link(db, sid, "s1", {"roles": {"main": {"preset": FALLBACK_PRESET, "effort": "high", "account": "", "allowed": []},
                                          "vision": {"preset": VISION_PRESET, "effort": "low", "account": "", "allowed": []}}})
    finished = asyncio.ensure_future(_wait_finished(manager))
    with share_turns.visiting("s1"):
        await manager.submit(sid, "hello", visitor={"share": "s1", "name": "guest"})
    await finished
    state = await manager.get_state(sid)
    assert state is not None and state.share_id == "s1"
    assert asked[-1] == FALLBACK_PRESET
    assert state.link_choices["vision"] == {"preset": VISION_PRESET, "effort": "low", "account": ""}

    finished = asyncio.ensure_future(_wait_finished(manager))
    await manager.submit(sid, "my own turn")
    await finished
    assert state.share_id == "" and state.link_choices == {}
    assert asked[-1] is None  # the session's own model: the installation's default
    assert manager.link_of(sid) == ""
    await manager.close()


async def test_a_visitor_pick_stays_beside_the_link(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "picked"}])
    asked: list[str | None] = []
    manager = await _manager(settings, db, provider, asked)
    sid = (await manager.create_session("shared")).session.id
    rule = {"preset": DEFAULT_PRESET, "effort": "", "account": "", "allowed": [{"preset": FALLBACK_PRESET, "accounts": [], "efforts": []}]}
    await _link(db, sid, "s1", {"roles": {"main": rule}})
    assert (await manager.set_link_pick(sid, "s1", rule, preset=FALLBACK_PRESET))["preset"] == FALLBACK_PRESET
    with pytest.raises(ValueError):
        await manager.set_link_pick(sid, "s1", rule, preset=VISION_PRESET)
    # The session's own model is not what the visitor chose.
    assert (await manager.live.load(sid)).get("preset") in (None, "")
    finished = asyncio.ensure_future(_wait_finished(manager))
    with share_turns.visiting("s1"):
        await manager.submit(sid, "hello", visitor={"share": "s1", "name": "guest"})
    await finished
    assert asked[-1] == FALLBACK_PRESET
    await manager.close()


async def test_a_spent_link_stops_the_visitor_turn_and_never_the_operator(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "the visitor's"}, {"text": "the operator's"}])
    asked: list[str | None] = []
    manager = await _manager(settings, db, provider, asked)
    stops: list[tuple[str, str]] = []

    async def sink(session_id: str, event: Any) -> None:
        if event.type.value == "error" and event.payload.get("kind") == "share_budget":
            stops.append((session_id, str(event.payload.get("message"))))

    manager.add_sink(sink)
    sid = (await manager.create_session("shared")).session.id
    await _link(db, sid, "s1", {"budget_usd": 1.0}, spent_usd=1.5)
    assert await share_spend.exhausted(SimpleNamespace(db=db), "s1") == "usd"
    finished = asyncio.ensure_future(_wait_finished(manager))
    with share_turns.visiting("s1"):
        await manager.submit(sid, "spend", visitor={"share": "s1", "name": "guest"})
    await finished
    assert stops == [(sid, share_spend.STOPPED["usd"])]

    finished = asyncio.ensure_future(_wait_finished(manager))
    await manager.submit(sid, "my own turn")
    await finished
    assert len(stops) == 1
    await manager.close()


async def test_the_admission_refuses_a_call_of_a_spent_link_and_only_that(db: Database) -> None:
    from protocore.contracts.llm import LLMObservabilityContext, LLMProviderError, LLMRequest

    from daedalus.host.inference_admission import HostInferenceAdmission

    sid = "sess"
    await _session(db, sid)
    await _link(db, sid, "s1", {"budget_weekly_pct": 2.0})
    await db.execute("UPDATE session_shares SET spent_weekly_pct = 2.5 WHERE id = 's1'")
    manager = SimpleNamespace(db=db, link_of=lambda session_id: "s1" if session_id == "visited" else "")
    admission = HostInferenceAdmission.__new__(HostInferenceAdmission)
    admission.manager = manager  # type: ignore[assignment]

    def request(session_id: str) -> LLMRequest:
        return LLMRequest(model="m", messages=[], observability=LLMObservabilityContext(session_id=session_id))

    with pytest.raises(LLMProviderError, match="weekly limit"):
        await admission._refuse_spent_link(request("visited"))
    await admission._refuse_spent_link(request("the operator's"))


# -- the spend -------------------------------------------------------------------------


def _record(provider_id: str, *, cost: float | None, tokens: tuple[int, int] = (100, 50), account: str = "", session_id: str = "s") -> UsageRecord:
    return UsageRecord(provider_id=provider_id, model="m", purpose="stream", raw={}, normalized={"input_tokens": tokens[0], "output_tokens": tokens[1]},
                       cost_usd=cost, duration_ms=1, run_id="r", session_id=session_id, account=account)


def _plans() -> Any:
    return SimpleNamespace(providers={
        "metered": ProviderConfig(name="Metered", base_url="https://api.example.org/v1"),
        "plan": ProviderConfig(name="Plan", base_url=keyproxy_base() + "/claude/v1"),
    })


async def test_metered_calls_count_in_dollars(db: Database) -> None:
    sid = "sess"
    await _session(db, sid)
    await _link(db, sid, "s1", {"budget_usd": 0.5})
    assert await share_spend.charge(db, _plans(), "s1", _record("metered", cost=0.3)) == (0.3, 0.0)
    assert await share_spend.exhausted(SimpleNamespace(db=db), "s1") is None
    await share_spend.charge(db, _plans(), "s1", _record("metered", cost=0.3))
    assert await share_spend.exhausted(SimpleNamespace(db=db), "s1") == "usd"


async def test_a_subscription_call_is_charged_its_tokens_share_of_the_window(db: Database) -> None:
    sid = "sess"
    now = datetime.now(UTC)
    await _session(db, sid)
    await _link(db, sid, "s1", {"budget_weekly_pct": 1.0})
    plans = _plans()
    # No readings yet: the call is not counted, which the docstring and the log say.
    assert await share_spend.charge(db, plans, "s1", _record("plan", cost=None)) == (0.0, 0.0)
    older, newer = now - timedelta(hours=2), now - timedelta(hours=1)
    for at, used in ((older, 10.0), (newer, 12.0)):
        await db.execute("INSERT INTO quota_samples(account, window_name, sampled_at, used_percent, window_seconds, source) VALUES (?,?,?,?,?,?)",
                         ("claude", "7d", at.isoformat(), used, 7 * 86400, "keyproxy"))
    # Between the readings the account served 2000 tokens, ours and everyone else's: 2 points over 2000 tokens.
    await db.execute("INSERT INTO usage_events(at, provider_id, model, purpose, session_id, input_tokens, output_tokens, account, raw) VALUES (?,?,?,?,?,?,?,?,'{}')",
                     ((older + timedelta(minutes=30)).isoformat(), "plan", "m", "stream", "other", 1500, 500, ""))
    assert await share_spend.weekly_price(db, plans, "claude", "") == pytest.approx(0.001)
    usd, pct = await share_spend.charge(db, plans, "s1", _record("plan", cost=None, tokens=(400, 100)))
    assert usd == 0.0 and pct == pytest.approx(0.5)
    assert await share_spend.exhausted(SimpleNamespace(db=db), "s1") is None
    await share_spend.charge(db, plans, "s1", _record("plan", cost=None, tokens=(400, 100)))
    assert await share_spend.exhausted(SimpleNamespace(db=db), "s1") == "weekly_pct"
    # A second account's windows are its own.
    assert share_spend.quota_account("claude", "abc") == "claude@abc" and share_spend.quota_account("claude", "default") == "claude"


async def test_the_sink_counts_only_a_visitor_turn() -> None:
    charged: list[str] = []

    class Inner:
        recorded = 0

        async def record(self, record: UsageRecord) -> None:
            Inner.recorded += 1

    manager = SimpleNamespace(link_of=lambda sid: "s1" if sid == "visited" else "", db=None, config=None)
    sink = share_spend.LinkUsageSink(Inner(), manager)

    async def charge(db: Any, config: Any, share: str, record: UsageRecord) -> tuple[float, float]:
        charged.append(share)
        return 0.0, 0.0

    original = share_spend.charge
    share_spend.charge = charge  # type: ignore[assignment]
    try:
        await sink.record(_record("metered", cost=1.0, session_id="visited"))
        await sink.record(_record("metered", cost=1.0, session_id="the operator's"))
    finally:
        share_spend.charge = original  # type: ignore[assignment]
    assert Inner.recorded == 2 and charged == ["s1"]


def test_a_link_holds_the_call_to_its_account() -> None:
    store = SimpleNamespace(several=lambda upstream: True, account=lambda upstream, a: object() if a in ("default", "second") else None,
                            chain=lambda upstream, preferred, session_id: ["default", "second"])
    router = AccountRouter(store, lambda provider, model: "")  # type: ignore[arg-type]
    endpoint = SimpleNamespace(id="plan", base_url=keyproxy_base() + "/claude/v1")
    assert router.chain(endpoint, "m", "s") == ["default", "second"]  # type: ignore[arg-type]
    router.forced = lambda session_id, upstream: "second" if session_id == "visited" else ""
    assert router.chain(endpoint, "m", "visited") == ["second"]  # type: ignore[arg-type]
    assert router.chain(endpoint, "m", "s") == ["default", "second"]  # type: ignore[arg-type]


# -- the roles -------------------------------------------------------------------------


async def test_describe_lists_the_enforced_roles_with_what_the_session_uses(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.available = lambda: ["deepseek", "openrouter"]  # type: ignore[method-assign]
    sid = (await manager.create_session("shared")).session.id
    roles = await share_roles.describe(SimpleNamespace(manager=manager), sid)  # type: ignore[arg-type]
    ids = [role["id"] for role in roles]
    assert ids[0] == "main" and "vision" in ids and set(ids) <= {"main", "subagents", "quick", "vision"}
    main = roles[0]
    assert main["pickable"] is True and main["current"]["preset"] == DEFAULT_PRESET
    assert {p["id"] for p in main["presets"]} == {DEFAULT_PRESET, FALLBACK_PRESET, VISION_PRESET}
    entry = next(p for p in main["presets"] if p["id"] == VISION_PRESET)
    assert entry["billing"] == "metered" and entry["efforts"] == [] and entry["accounts"] == []
    vision = next(role for role in roles if role["id"] == "vision")
    assert vision["pickable"] is False and vision["current"]["preset"] == VISION_PRESET
    assert {p["id"] for p in vision["presets"]} == {FALLBACK_PRESET, VISION_PRESET}
    await manager.close()


async def test_a_helper_started_in_a_visitor_turn_is_the_visitor_s_and_runs_on_the_subagent_rule(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    leader = await manager.create_session("leader")
    child = await manager.create_session("[sub] helper", metadata={"subagent_of": leader.session.id})
    await _link(db, leader.session.id, "s1", {"roles": {"subagents": {"preset": VISION_PRESET, "effort": "low", "account": "", "allowed": []}}})
    blocker = asyncio.Event()
    leader.task = asyncio.ensure_future(blocker.wait())
    try:
        # The leader runs the operator's turn: a helper it starts is the operator's.
        assert manager._share_of_run(child, None) == ""
        leader.share_id = "s1"
        child.share_id = manager._share_of_run(child, None)
        assert child.share_id == "s1"
        overrides = await manager._link_overrides(child, {})
        assert overrides["preset"] == VISION_PRESET and overrides["reasoning_effort"] == "low"
        # The leader's own conversation has no rule in this link: untouched.
        assert await manager._link_overrides(leader, {"preset": DEFAULT_PRESET}) == {"preset": DEFAULT_PRESET}
    finally:
        blocker.set()
        await leader.task
    await manager.close()


# -- the sets --------------------------------------------------------------------------


def test_a_set_may_hold_the_value_s_own_preset_with_several_efforts() -> None:
    from daedalus.extensions.share_policy import allows as door_allows
    from daedalus.extensions.share_policy import validate

    roles = [{"id": "main", "presets": [{"id": "a", "efforts": ["none", "low", "medium", "high"], "accounts": [{"id": "one"}, {"id": "two"}]}]},
             {"id": "subagents", "presets": [{"id": "a", "efforts": ["low", "high"], "accounts": []}, {"id": "b", "efforts": [], "accounts": []}]}]
    raw = {"roles": {"main": {"preset": "a", "effort": "high", "account": "one", "allowed": [{"preset": "a", "accounts": ["one"], "efforts": ["low", "medium", "high"]}]},
                     "subagents": {"preset": "a", "effort": "low", "account": "", "allowed": [{"preset": "a", "accounts": [], "efforts": ["low", "high"]}, {"preset": "b", "accounts": [], "efforts": []}]}}}
    kept = validate(raw, roles, "host")
    assert kept["roles"] == raw["roles"]
    # The door, judging a visitor's pick: another effort of the value's own preset is in, one left out is not.
    assert door_allows(kept, "main", "a", "low", None) and door_allows(kept, "main", "a", "medium", "one")
    assert not door_allows(kept, "main", "a", "none", None) and not door_allows(kept, "main", "a", "low", "two")
    # The runtime, choosing what the turn runs on.
    rule = kept["roles"]["main"]
    assert share_turns.choice(rule, {"preset": "a", "effort": "low", "account": "one"}) == {"preset": "a", "effort": "low", "account": "one"}
    assert share_turns.choice(rule, {"preset": "a", "effort": "none", "account": "one"}) == {"preset": "a", "effort": "high", "account": "one"}
    # A set as links made before the value could be in it wrote it: an empty list is still the value alone.
    assert share_turns.members({"preset": "a", "allowed": []}) == ["a"]
    assert share_turns.members(kept["roles"]["subagents"]) == ["a", "b"]


def test_the_agent_picks_a_helper_s_model_from_the_link_s_set() -> None:
    rule = {"preset": "a", "effort": "low", "account": "acc-1",
            "allowed": [{"preset": "a", "accounts": [], "efforts": ["low", "high"]}, {"preset": "b", "accounts": ["acc-2", "acc-3"], "efforts": ["medium"]}]}
    assert share_turns.agent_pick(rule, None, None) == {"preset": "a", "effort": "low", "account": "acc-1"}
    assert share_turns.agent_pick(rule, "a", "high") == {"preset": "a", "effort": "high", "account": "acc-1"}
    assert share_turns.agent_pick(rule, "b", None) == {"preset": "b", "effort": "medium", "account": "acc-2"}
    with pytest.raises(ValueError, match="'c' is not one this live link lets helpers run on; allowed: a, b"):
        share_turns.agent_pick(rule, "c", None)
    with pytest.raises(ValueError, match="effort 'xhigh' is not one .* allowed: high, low"):
        share_turns.agent_pick(rule, "a", "xhigh")
    with pytest.raises(ValueError, match="effort 'high'"):
        share_turns.agent_pick(rule, "b", "high")
    # The links made before sets: the value alone, any other model refused.
    with pytest.raises(ValueError, match="allowed: a"):
        share_turns.agent_pick({"preset": "a", "effort": "", "account": "", "allowed": []}, "b", None)


def _local_config() -> Any:
    from daedalus.config import ModelPresetConfig, RuntimeConfig

    runtime = RuntimeConfig(
        providers={"local": ProviderConfig(kind="openai_compat", base_url="http://127.0.0.1:9/v1")},
        presets={pid: ModelPresetConfig(provider="local", model=pid.split(".")[1]) for pid in ("local.a", "local.b", "local.c")},
        model={"preset": "local.a"},
    )
    runtime.subagents.quick_models = ["local.c", "local.a"]
    return runtime


async def test_a_subagent_of_a_visitor_turn_starts_on_the_link_s_set_and_runs_on_it(settings: Settings, db: Database) -> None:
    from daedalus.extensions.subagents import Subagents
    from tests.unit.test_subagents import _capture

    manager = SessionManager(settings, _local_config(), db=db)
    await manager.start()
    subs = Subagents(SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={}))  # type: ignore[arg-type]
    _capture(manager)
    leader = await manager.create_session("lead")
    lid = leader.session.id
    await _link(db, lid, "s1", {"roles": {
        "subagents": {"preset": "local.b", "effort": "low", "account": "", "allowed": [{"preset": "local.a", "accounts": [], "efforts": ["high"]}]},
        "quick": {"preset": "local.a", "effort": "", "account": "", "allowed": []},
    }})
    blocker = asyncio.Event()
    leader.task = asyncio.ensure_future(blocker.wait())
    try:
        # The operator's own turn: the installation's list, every model.
        assert manager.subagent_allowance(leader.metadata, {}, session_id=lid).presets == ("local.a", "local.b", "local.c")
        leader.share_id = "s1"
        await manager._link_overrides(leader, {})
        assert manager.subagent_allowance(leader.metadata, {}, session_id=lid).presets == ("local.b", "local.a")
        assert await subs.service("models", leader_id=lid) == ["local.b", "local.a"]
        assert manager.quick_models(leader.metadata, session_id=lid) == ["local.a"]
        notes = manager.notes_for(leader, overrides={})
        assert "Models SubAgent accepts in this turn (preset ids, the first is its default): local.b, local.a" in notes
        assert "Models QuickAgent accepts (preset ids, the first is its default): local.a" in notes

        # Outside the set: refused, as the installation's own list refuses, and no helper is made.
        before = len(await subs.children(lid))
        with pytest.raises(ValueError, match="'local.c' is not one this live link lets helpers run on"):
            await subs.spawn(leader_id=lid, task="t", model="local.c")
        with pytest.raises(ValueError, match="effort 'medium'"):
            await subs.spawn(leader_id=lid, task="t", model="local.a", effort="medium")
        assert len(await subs.children(lid)) == before

        # Inside it: the helper runs on the model it was started on, with the effort the set names.
        started = await subs.spawn(leader_id=lid, task="t", model="local.a")
        child = await manager.get_state(started["session_id"])
        assert child is not None and child.metadata[share_turns.PICKS_KEY] == {"s1": {"preset": "local.a", "effort": "high", "account": ""}}
        child.share_id = manager._share_of_run(child, None)
        assert child.share_id == "s1"
        overrides = await manager._link_overrides(child, await manager.live.load(child.session.id))
        assert overrides["preset"] == "local.a" and overrides["reasoning_effort"] == "high" and overrides["thinking_enabled"] is True
        assert child.link_choices["subagents"] == {"preset": "local.a", "effort": "high", "account": ""}

        # Naming none is the set's value, at its effort.
        plain = await manager.get_state((await subs.spawn(leader_id=lid, task="t"))["session_id"])
        assert plain is not None
        plain.share_id = "s1"
        overrides = await manager._link_overrides(plain, await manager.live.load(plain.session.id))
        assert overrides["preset"] == "local.b" and overrides["reasoning_effort"] == "low"

        # A quick model outside the link's set is refused the same way.
        with pytest.raises(ValueError, match="'local.c' is not a quick model here"):
            await subs.quick(leader_id=lid, task="t", model="local.c")
    finally:
        blocker.set()
        await leader.task
    # The operator's next turn has no link: the installation's list again.
    leader.share_id = ""
    await manager._link_overrides(leader, {})
    assert manager.subagent_allowance(leader.metadata, {}, session_id=lid).presets == ("local.a", "local.b", "local.c")
    await manager.close()


async def test_a_visitor_switches_the_effort_of_the_conversation_s_own_model(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "low"}])
    asked: list[str | None] = []
    manager = await _manager(settings, db, provider, asked)
    sid = (await manager.create_session("shared")).session.id
    rule = {"preset": DEFAULT_PRESET, "effort": "high", "account": "", "allowed": [{"preset": DEFAULT_PRESET, "accounts": [], "efforts": ["low", "medium", "high"]}]}
    await _link(db, sid, "s1", {"roles": {"main": rule}})
    assert await manager.set_link_pick(sid, "s1", rule, effort="low") == {"preset": DEFAULT_PRESET, "effort": "low", "account": ""}
    with pytest.raises(ValueError):
        await manager.set_link_pick(sid, "s1", rule, effort="xhigh")
    state = await manager.get_state(sid)
    assert state is not None
    finished = asyncio.ensure_future(_wait_finished(manager))
    with share_turns.visiting("s1"):
        await manager.submit(sid, "hello", visitor={"share": "s1", "name": "guest"})
    await finished
    assert asked[-1] == DEFAULT_PRESET and state.link_choices["main"] == {"preset": DEFAULT_PRESET, "effort": "low", "account": ""}
    applied = share_turns.applied({}, state.link_choices["main"], manager.config.presets)
    assert applied["reasoning_effort"] == "low" and applied["thinking_enabled"] is True
    await manager.close()


async def test_describe_says_who_chooses_and_the_helpers_present_set(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, _local_config(), db=db)
    await manager.start()
    manager.providers.available = lambda: ["local"]  # type: ignore[method-assign]
    sid = (await manager.create_session("shared")).session.id
    await manager.set_subagent_models(sid, "chosen", ["local.b", "local.c"])
    roles = {role["id"]: role for role in await share_roles.describe(SimpleNamespace(manager=manager), sid)}  # type: ignore[arg-type]
    assert roles["main"]["chooser"] == "visitor" and roles["main"]["pickable"] is True and roles["main"]["current"]["allowed"] == []
    helpers = roles["subagents"]
    assert helpers["chooser"] == "agent" and helpers["pickable"] is False
    assert helpers["current"]["preset"] == "local.b"
    assert helpers["current"]["allowed"] == [{"preset": "local.b", "accounts": [], "efforts": []}, {"preset": "local.c", "accounts": [], "efforts": []}]
    quick = roles["quick"]
    assert quick["chooser"] == "agent" and quick["current"]["preset"] == "local.c" and [entry["preset"] for entry in quick["current"]["allowed"]] == ["local.c", "local.a"]
    await manager.close()
