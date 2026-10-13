"""A recording the operator edited into a skill, and BrowserReplay following it.

The editor can only keep, drop and change the recording's own steps: a parameter in place of what was
typed, an operator secret's placeholder in a sign-in's field and never a value, an address only on the
site the recording went to. The replay finds each element again by its selector, then its role and
name, then its words, acts through the agent's own path, and stops at the first step it cannot find,
naming it and what was left for the model to finish."""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import json
from typing import Any

import pytest

from daedalus.browser.model import InvalidRequest
from daedalus.browser.replay import SCRIPT_FILE, build_script, describe, params_of
from daedalus.host.skills import assess
from tests.unit.test_browser_service import wait_until
from tests.unit.test_browser_tools import Rig, base, daemon, rig  # noqa: F401
from tests.unit.test_browser_workflows import open_driven, workflows


async def give_back(rig: Rig, group: str) -> None:
    """The operator hands the browser back to the agent, which a replay needs."""
    rig.daemon.set_control(group, "agent")
    service = rig.app.extensions["browser"]

    async def owner() -> str:
        return str((await service.get_row(group))["control_owner"])

    await wait_until(owner, "agent", timeout=5)

RECORDING: dict[str, Any] = {
    "id": "w1",
    "state": "stopped",
    "start_url": "https://shop.test/",
    "steps": [
        {"n": 1, "action": "type", "element": {"role": "searchbox", "name": "Search", "selector": "#q"}, "slot": "search", "value": "2026-10", "submit": True},
        {"n": 2, "action": "arrive", "to": "https://shop.test/results?q={q}", "title": "Results"},
        {"n": 3, "action": "scroll", "direction": "down", "count": 3},
        {"n": 4, "action": "handoff", "reason": "login", "element": {"role": "textbox", "name": "Email"},
         "fields": [{"role": "textbox", "name": "Email", "selector": "#email", "secret": "name"},
                    {"role": "textbox", "name": "Password", "selector": "#pw", "secret": "password"}]},
        {"n": 5, "action": "tab", "to": "https://shop.test/other", "title": "Other"},
        {"n": 6, "action": "click", "element": {"role": "button", "name": "Show", "selector": "form > button", "text": "Show"}},
    ],
}


def test_the_editor_names_parameters_drops_steps_and_binds_secrets_by_placeholder_only() -> None:
    edits = [
        {"n": 1, "text": "{month}"},
        {"n": 4, "signin": "secrets", "secrets": ["«secret:shop_user»", "«secret:shop_password»"]},
        {"n": 5},
        {"n": 6},
    ]
    script = build_script(RECORDING, edits, name="monthly-report", description="Open the monthly report on the shop")
    assert [s["n"] for s in script["steps"]] == [1, 4, 6]  # the scroll was deleted; a tab switch is never kept
    assert script["params"] == ["month"] and script["steps"][0]["text"] == "{month}" and script["steps"][0]["submit"]
    fields = script["steps"][1]["fields"]
    assert [(f["selector"], f["use"]) for f in fields] == [("#email", "«secret:shop_user»"), ("#pw", "«secret:shop_password»")]
    assert script["steps"][2]["element"]["selector"] == "form > button"
    # Without an edit, a kept value stays as it was typed; a value never kept is the recording's blank.
    kept = build_script(RECORDING, [{"n": 1}, {"n": 6}], name="x", description="y")
    assert kept["steps"][0]["text"] == "2026-10" and kept["params"] == []
    blank = build_script({**RECORDING, "steps": [{**RECORDING["steps"][0], "value": None}]}, [{"n": 1}], name="x", description="y")
    assert blank["steps"][0]["text"] == "{search}" and blank["params"] == ["search"]

    for wrong, said in [
        ([{"n": 4, "signin": "secrets", "secrets": ["hunter2", "«secret:pw»"]}], "never a value"),
        ([{"n": 4, "signin": "secrets", "secrets": ["«secret:pw»"]}], "one operator secret for each"),
        ([{"n": 4, "signin": "teleport"}], "a sign-in is done by"),
        ([{"n": 9}], "not a step of this recording"),
        ([{"n": 1}, {"n": 1}], "not a step of this recording"),
        ([{"n": 2}], "keep at least one step"),
        ([{"n": 2, "to": "https://evil.test/"}, {"n": 6}], "stays on shop.test"),
    ]:
        with pytest.raises(InvalidRequest) as refused:
            build_script(RECORDING, wrong, name="x", description="y")
        assert said in refused.value.message


def test_the_steps_read_in_words_and_name_their_parameters() -> None:
    script = build_script(RECORDING, [{"n": 1, "text": "{month} {year}"}, {"n": 4}, {"n": 6}], name="x", description="y")
    assert params_of(script["steps"]) == ["month", "year"]
    assert [describe(s) for s in script["steps"]] == [
        'Type "{month} {year}" into the searchbox "Search", then Enter.',
        "Sign-in (login): the operator signs in.",
        'Click the button "Show".',
    ]


async def test_a_recording_saved_as_a_skill_is_replayed_and_stops_where_the_page_changed(rig: Rig) -> None:
    sid, group = await open_driven(rig)
    found = workflows(rig)
    started = await found.start(group)
    rig.daemon.record_step(group, action="type", element={"role": "searchbox", "name": "Search", "selector": "input#q"}, slot="search", submit=True)
    rig.daemon.record_step(group, action="click", element={"role": "link", "name": "Running shoes", "selector": "a.shoes-old"})
    rig.daemon.record_step(group, action="arrive", to="https://shop.test/shoes", title="Running shoes")
    rig.daemon.record_step(group, action="click", element={"role": "button", "name": "Add to cart", "selector": "#add"})
    rig.daemon.record_step(group, action="click", element={"role": "button", "name": "Buy now", "selector": "#buy"})
    stopped = await found.stop(group)
    assert stopped["id"] == started["id"]
    rig.daemon.pages["https://shop.test/"].elements["e1"].selector = "input#q"

    edits = [{"n": 1, "text": "{query}"}, {"n": 2}, {"n": 3}, {"n": 4}]
    saved = await found.make_skill(stopped["id"], rig.app.extensions["browser_agent"].recorded, edits, name="Buy Shoes", description="Find shoes on the shop and add them to the cart")
    assert saved["skill"] == "buy-shoes" and saved["params"] == ["query"] and saved["steps"] == 4
    assert (await found.get(stopped["id"]))["skill"] == "buy-shoes"
    directory = rig.app.manager.settings.state_dir / "skill-drafts" / "buy-shoes"
    markdown = (directory / "SKILL.md").read_text(encoding="utf-8")
    assert assess("buy-shoes", markdown, [])["valid"], assess("buy-shoes", markdown, [])["errors"]
    assert "params: query" in markdown and "requires: browser" in markdown and 'BrowserReplay(skill="buy-shoes", params={"query": …})' in markdown
    script = json.loads((directory / SCRIPT_FILE).read_text(encoding="utf-8"))
    assert [s["action"] for s in script["steps"]] == ["type", "click", "arrive", "click"]

    await give_back(rig, group)
    text, failed = await rig.call(sid, "BrowserReplay")
    assert not failed and "- buy-shoes: Find shoes on the shop" in text and "(params: query)" in text
    text, failed = await rig.call(sid, "BrowserReplay", skill="buy-shoes")
    assert failed and "give query in params" in text

    text, failed = await rig.call(sid, "BrowserReplay", skill="buy-shoes", params={"query": "red shoes"})
    assert not failed, text
    assert "4 of 4 steps done" in text
    assert "found by its role and name: the recorded selector no longer matches" in text  # the link's selector went stale
    acts = [p for m, p in rig.daemon.calls if m == "page.act" and not p.get("dry_run")]
    assert [(a["action"], a.get("text")) for a in acts[-3:]] == [("type", "red shoes"), ("click", None), ("click", None)]

    # The page lost the button: the replay stops there and hands the rest to the model.
    del rig.daemon.pages["https://shop.test/shoes"].elements["e10"]
    text, failed = await rig.call(sid, "BrowserReplay", skill="buy-shoes", params={"query": "red shoes"})
    assert failed and "Stopped at step 4" in text and "Take a BrowserSnapshot" in text and "2 of 4" not in text
    assert "3 of 4 steps done" in text
    audit = await rig.app.extensions["browser"].audit_log(group)
    assert "workflow_skill" in {e["action"] for e in audit}


async def test_a_sign_in_the_operator_does_pauses_the_replay_with_the_way_on(rig: Rig) -> None:
    sid, group = await open_driven(rig)
    found = workflows(rig)
    await found.start(group)
    rig.daemon.record_step(group, action="handoff", reason="login", element={"role": "textbox", "name": "Email"})
    rig.daemon.record_step(group, action="click", element={"role": "link", "name": "Running shoes"})
    stopped = await found.stop(group)
    await found.make_skill(stopped["id"], rig.app.extensions["browser_agent"].recorded, [{"n": 1}, {"n": 2}], name="signed", description="Sign in and open the running shoes")
    await give_back(rig, group)
    text, failed = await rig.call(sid, "BrowserReplay", skill="signed")
    assert not failed and "Paused at step 1" in text and 'BrowserReplay(skill="signed", from_step=2)' in text
    assert "What was left:\n2. Click the link" in text


async def test_a_sign_in_bound_to_operator_secrets_types_their_values_and_the_files_hold_only_placeholders(rig: Rig) -> None:
    value = "correct-horse-battery-staple"
    sid, group = await open_driven(rig)
    await rig.manager.secrets.put("session", sid, "shop_user", "someone")
    await rig.manager.secrets.put("session", sid, "shop_password", value)
    found = workflows(rig)
    await found.start(group)
    rig.daemon.record_step(group, action="navigate", to="https://login.test/")
    rig.daemon.record_step(group, action="handoff", reason="login", element={"role": "textbox", "name": "Email"}, fields=[
        {"role": "textbox", "name": "Email", "secret": "name"}, {"role": "textbox", "name": "Password", "secret": "password"},
    ])
    stopped = await found.stop(group)
    edits = [{"n": 1}, {"n": 2, "signin": "secrets", "secrets": ["«secret:shop_user»", "«secret:shop_password»"]}]
    await found.make_skill(stopped["id"], rig.app.extensions["browser_agent"].recorded, edits, name="shop-login", description="Sign in to the shop with the saved secrets")
    directory = rig.app.manager.settings.state_dir / "skill-drafts" / "shop-login"
    for path in directory.iterdir():
        assert value not in path.read_text(encoding="utf-8")
    await give_back(rig, group)
    text, failed = await rig.call(sid, "BrowserReplay", skill="shop-login")
    # The name goes in as the operator's value; sending the sign-in is asked about as the agent's own
    # submit would be: a replay is no way around the questions.
    typed = [p for m, p in rig.daemon.calls if m == "page.act" and not p.get("dry_run")]
    assert [(t["text"], t.get("operator_secret")) for t in typed] == [("someone", True)]
    assert failed and "needs the operator's approval" in text and "Stopped at step 2" in text
    asked = [p for m, p in rig.daemon.calls if m == "page.act" and p.get("dry_run")][-1]
    assert asked["text"] == value and asked["submit"] is True
    assert value not in text
