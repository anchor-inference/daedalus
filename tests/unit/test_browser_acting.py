"""The browser's acting side as a model meets it: several steps in one call, each through every wall;
a click at a point where the operator allows it; scrolling a pane or to a text; a clear word when the
daemon is older than the host; and the soft notes for an agent going round in circles."""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

from typing import Any

from daedalus.browser.agent import STEPS_MAX, LoopHints, Step, merge_diffs
from daedalus.browser.model import InvalidRequest
from tests.support.fake_browserd import Element
from tests.unit.test_browser_tools import Rig, base, daemon, rig  # noqa: F401


def form(rig: Rig) -> None:
    rig.daemon.page("https://shop.test/contact", title="Contact", heading="Contact us", elements={
        "e40": Element("e40", "textbox", "Your name", tag="input"),
        "e41": Element("e41", "checkbox", "Copy to me", tag="input", type="checkbox"),
        "e42": Element("e42", "button", "Send message", box={"x": 400.0, "y": 500.0, "w": 100.0, "h": 40.0}),
        "e43": Element("e43", "link", "Home", tag="a", goes_to="https://shop.test/"),
        "e44": Element("e44", "button", "Does nothing"),
        "e45": Element("e45", "list", "Reviews", tag="div"),
    })


async def opened(rig: Rig, url: str = "https://shop.test/contact") -> str:
    form(rig)
    sid = await rig.session()
    text, failed = await rig.call(sid, "BrowserOpen", url=url)
    assert not failed, text
    return sid


def actions(rig: Rig) -> list[dict[str, Any]]:
    return [e["data"] for e in rig.daemon.events if e["type"] == "action"]


async def acts(rig: Rig, sid: str) -> list[dict[str, Any]]:
    audit = await rig.app.extensions["browser"].audit_log(f"s-{sid}")
    return [e["detail"] for e in reversed(audit) if e["action"] == "act"]


async def test_steps_run_in_order_with_a_line_of_the_audit_each_and_one_diff(rig: Rig) -> None:
    sid = await opened(rig)
    text, failed = await rig.call(sid, "BrowserAct", steps=[
        {"action": "type", "ref": "e40", "element": "the name field", "text": "Ada"},
        {"action": "check", "ref": "e41", "element": "the copy-to-me box"},
    ])
    assert not failed, text
    assert text.startswith("Did 2 of 2 steps:") and "1. Done: type" in text and "2. Done: check" in text
    # One diff for the two, fenced as the page's, and the fence said once.
    assert text.count("What changed on the page") == 1 and text.count("[end of page content]") == 1
    assert [a["kind"] for a in actions(rig)] == ["type", "check"]
    rows = await acts(rig, sid)
    assert [(r["action"], r["step"]) for r in rows] == [("type", "1/2"), ("check", "2/2")]


async def test_steps_stop_after_a_navigation_since_the_next_refs_were_planned_for_the_old_page(rig: Rig) -> None:
    sid = await opened(rig)
    text, failed = await rig.call(sid, "BrowserAct", steps=[
        {"action": "type", "ref": "e40", "element": "the name field", "text": "Ada"},
        {"action": "click", "ref": "e43", "element": "the Home link"},
        {"action": "click", "ref": "e44", "element": "a button"},
    ])
    assert not failed, text
    assert "Did 2 of 3 steps" in text and "Stopped after step 2: the tab went to https://shop.test/" in text and "steps 3–3 were not tried" in text
    assert [a["kind"] for a in actions(rig)] == ["type", "click"]


async def test_each_step_is_asked_about_as_one_action_and_the_steps_stop_at_the_question(rig: Rig) -> None:
    sid = await opened(rig)
    # The key a lone click on Send would be asked with.
    alone, failed = await rig.call(sid, "BrowserAct", action="click", ref="e42", element="the Send button")
    assert failed and "Approval key: " in alone
    key = alone.split("Approval key: ")[1].split(".")[0]
    text, failed = await rig.call(sid, "BrowserAct", steps=[
        {"action": "type", "ref": "e40", "element": "the name field", "text": "Ada"},
        {"action": "click", "ref": "e42", "element": "the Send button"},
        {"action": "click", "ref": "e44", "element": "a button"},
    ])
    assert failed and "Did 1 of 3 steps" in text and "2. click on “the Send button” was not done" in text
    assert f"Approval key: {key}" in text and "Steps 3–3 were not tried." in text
    assert [a["kind"] for a in actions(rig)] == ["type"]
    refused = (await acts(rig, sid))[-1]
    assert refused["step"] == "2/3" and refused["decision"] == "asked" and "send" in refused["sensitive"]
    # The operator's yes lets exactly that step through, in steps as alone.
    await rig.manager.grant(sid, key, via="app")
    text, failed = await rig.call(sid, "BrowserAct", steps=[{"action": "click", "ref": "e42", "element": "the Send button"}])
    assert not failed and "Did 1 of 1 steps" in text
    assert (await acts(rig, sid))[-1]["grant"] == key


async def test_a_failed_step_ends_the_steps_and_says_what_was_done(rig: Rig) -> None:
    sid = await opened(rig)
    text, failed = await rig.call(sid, "BrowserAct", steps=[
        {"action": "check", "ref": "e41", "element": "the copy-to-me box"},
        {"action": "click", "ref": "e99", "element": "a button that went"},
        {"action": "click", "ref": "e44", "element": "a button"},
    ])
    assert failed and "Did 1 of 3 steps" in text and "e99 is not on the page any more" in text and "Steps 3–3 were not tried." in text
    assert [a["kind"] for a in actions(rig)] == ["check"]


async def test_steps_are_checked_whole_before_any_is_done(rig: Rig) -> None:
    sid = await opened(rig)
    one = {"action": "check", "ref": "e41", "element": "the box"}
    for steps, said in (
        ([one] * (STEPS_MAX + 1), f"at most {STEPS_MAX} actions"),
        ([one, {"action": "click", "element": "no ref"}], "step 2: click needs the ref"),
        ([one, {"action": "click", "ref": "e44", "element": "x", "tab": "t1"}], "step 2: unknown key tab"),
        ([one, "click e44"], "step 2: each step is an object"),
        ([], "steps is a list of 1 to 5 actions"),
    ):
        text, failed = await rig.call(sid, "BrowserAct", steps=steps)
        assert failed and said in text, (steps, text)
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e44", element="x", steps=[one])
    assert failed and "not both" in text
    assert actions(rig) == []


async def test_a_point_is_off_until_the_operator_allows_it_and_then_walled_like_a_ref(rig: Rig) -> None:
    sid = await opened(rig)
    text, failed = await rig.call(sid, "BrowserAct", action="click", x=150, y=210, element="the name field")
    assert failed and "Settings → Browser" in text and actions(rig) == []
    rig.manager.config.browser.point_clicks = True
    try:
        text, failed = await rig.call(sid, "BrowserAct", action="click", x=150, y=210, element="the name field")
        assert not failed and "(at e" in text, text
        assert actions(rig)[-1]["kind"] == "click"
        # What is at the point is classified: a Send button there is asked about, not clicked.
        text, failed = await rig.call(sid, "BrowserAct", action="click", x=450, y=520, element="the Send button")
        assert failed and "needs the operator's approval" in text
        assert (await acts(rig, sid))[-1]["ref"] == "e42"
        text, failed = await rig.call(sid, "BrowserAct", action="type", x=450, y=520, element="a field", text="hi")
        assert failed and "need a ref" in text
        text, failed = await rig.call(sid, "BrowserAct", action="click", x=450, element="half a point")
        assert failed and "both x and y" in text
    finally:
        rig.manager.config.browser.point_clicks = False


async def test_scrolling_a_pane_and_to_a_text(rig: Rig) -> None:
    sid = await opened(rig)
    text, failed = await rig.call(sid, "BrowserAct", action="scroll", ref="e45", direction="down", element="the reviews list")
    assert not failed and "Scrolled the pane e45 by 480 px." in text and "1.0 screens above, 3.0 screens below" in text
    text, failed = await rig.call(sid, "BrowserAct", action="scroll", text="Send message", element="the send button")
    assert not failed and "Scrolled to e42" in text
    text, failed = await rig.call(sid, "BrowserAct", action="scroll", text="Send", ref="e45", element="x")
    assert failed and "neither a ref nor a direction" in text
    text, failed = await rig.call(sid, "BrowserAct", action="scroll", direction="sideways", element="x")
    assert failed and "direction is one of up, down, left, right" in text


async def test_an_older_daemon_is_named_as_such_not_blamed_on_the_model(rig: Rig) -> None:
    sid = await opened(rig)
    rig.daemon.legacy = True
    rig.manager.config.browser.point_clicks = True
    try:
        text, failed = await rig.call(sid, "BrowserAct", action="click", x=150, y=210, element="the name field")
        assert failed and "cannot act at a point yet" in text
        text, failed = await rig.call(sid, "BrowserAct", action="scroll", text="Send", element="the send button")
        assert failed and "cannot scroll to a text or sideways yet" in text
        text, failed = await rig.call(sid, "BrowserText", find="Send")
        assert failed and "cannot search a page yet" in text
        assert actions(rig) == []
    finally:
        rig.manager.config.browser.point_clicks = False


async def test_a_click_that_changes_nothing_again_and_again_gets_a_note_never_a_refusal(rig: Rig) -> None:
    sid = await opened(rig)
    for n in range(3):
        text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e44", element=f"the button, try {n}")
        assert not failed
    assert "Note: that was click on e44 3 times on this page, and the page did not change once." in text
    assert text.index("Done: click") < text.index("Note:")
    text, failed = await rig.call(sid, "BrowserSnapshot")
    assert "Note:" not in text
    text, failed = await rig.call(sid, "BrowserSnapshot")
    assert not failed and "exactly as it was at your last read" in text and text.rstrip().endswith("move on.")
    for _ in range(3):
        text, failed = await rig.call(sid, "BrowserNavigate", url="https://shop.test/contact")
    assert not failed and "opened https://shop.test/contact 3 times" in text


def test_the_hints_keep_to_what_the_call_did() -> None:
    hints = LoopHints()
    assert not hints.acted("a", "click", "e1", "u", True)
    assert not hints.acted("a", "click", "e1", "u", True)
    assert not hints.acted("a", "click", "e1", "u", False)  # it changed things before
    assert hints.acted("a", "click", "e1", "u", True).startswith("Note: you have done click on e1 4 times")
    assert not hints.acted("b", "click", "e1", "u", False)  # another owner's own count
    assert not hints.read("a", "snapshot", "u", "one") and hints.read("a", "snapshot", "u", "one")
    assert not hints.read("a", "snapshot", "u", "two")
    for n in range(LoopHints.OWNERS + 5):
        hints.went(f"o{n}", "u")
    assert len(hints._seen) == LoopHints.OWNERS


def test_diffs_of_several_steps_net_out() -> None:
    merged = merge_diffs(["+ menu \"File\"\n- button \"Open\"", "- menu \"File\"\n+ dialog \"Saved\"", "+ dialog \"Saved\""])
    assert merged.splitlines() == ['- button "Open"', '+ dialog "Saved"']
    assert merge_diffs(["", ""]) == ""


def test_a_step_is_read_as_one_call_is() -> None:
    step = Step.parse({"action": "click", "x": 3, "y": 4.5, "element": "the map"})
    assert step.pointed and step.target == "@3,4"
    try:
        Step.parse({"action": "click", "x": 3, "y": 4, "ref": "e1", "element": "x"})
    except InvalidRequest as exc:
        assert "not both" in exc.message
    else:
        raise AssertionError("a point and a ref together were taken")


async def test_a_site_search_is_not_a_sign_in_but_enter_beside_a_password_is(rig: Rig) -> None:
    sid = await opened(rig, "https://shop.test/")
    text, failed = await rig.call(sid, "BrowserAct", action="type", ref="e1", element="the search box", text="trail shoes", submit=True)
    assert not failed and "Done: type" in text
    await rig.call(sid, "BrowserNavigate", url="https://login.test/")
    text, failed = await rig.call(sid, "BrowserAct", action="type", ref="e30", element="the email field", text="someone@example.com", submit=True)
    assert failed and "needs the operator's approval" in text and "credentials" in (await acts(rig, sid))[-1]["sensitive"]


async def test_a_made_up_ref_is_answered_with_where_refs_come_from(rig: Rig) -> None:
    sid = await opened(rig)
    for arguments in ({"action": "click", "ref": "text=43", "element": "the price"}, {"action": "click", "ref": "css=div", "element": "a div"}):
        text, failed = await rig.call(sid, "BrowserAct", **arguments)
        assert failed and "is not a ref" in text and "BrowserText(find=" in text and "latest BrowserSnapshot" in text
    text, failed = await rig.call(sid, "BrowserSnapshot", scope="navigation")
    assert failed and "'navigation' is not a ref" in text
    text, failed = await rig.call(sid, "BrowserAct", action="click", element="the add button")
    assert failed and "BrowserText(find=" in text
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="f1", element="the shipping frame")
    assert failed and "f1 names a frame, not an element in it" in text
    assert actions(rig) == []


async def test_a_run_of_calls_that_change_nothing_is_told_to_stop_guessing(rig: Rig) -> None:
    sid = await opened(rig)
    await rig.call(sid, "BrowserSnapshot")
    said = []
    for ref in ("text=1", "text=2", "body", "css=a", "e98"):
        text, _ = await rig.call(sid, "BrowserAct", action="click", ref=ref, element="the thing without a ref")
        said.append(text)
    assert all("Stop guessing" not in t for t in said[:4])
    assert "your last 5 browser calls changed nothing on the page. Stop guessing" in said[4] and "say plainly what is missing" in said[4]
    # A change on the page starts the count again.
    await rig.call(sid, "BrowserAct", action="type", ref="e40", element="the name field", text="Ada")
    text, _ = await rig.call(sid, "BrowserAct", action="click", ref="text=3", element="x")
    assert "Stop guessing" not in text


def test_the_prompt_lets_the_gate_ask_and_the_scheme_refusal_names_what_reads_a_page() -> None:
    from daedalus.host import prompts
    from daedalus.host.policy import Policy

    assert "Do not stop to ask the operator first" in prompts.BROWSER and "asked about before it happens" not in prompts.BROWSER
    assert "say plainly what is missing" in prompts.BROWSER
    decision = Policy().evaluate("BrowserNavigate", {"url": "view-source:https://shop.test/"})
    assert decision.rule == "browser.scheme" and "BrowserText(find=" in decision.reason


async def test_answering_a_dialog_says_what_the_answer_changed_on_the_page() -> None:
    """"Accepted" alone left the agent to take a whole snapshot to learn whether the delete it confirmed
    had happened; the daemon returns the change and the reply carries it, fenced as page content."""
    from daedalus.browser.agent import BrowserAgent

    class Service:
        async def call(self, group: str, method: str, params: dict, **_: object) -> dict:
            assert method == "dialog.answer" and params["accept"] is True
            return {"diff": '- row "Invoice 42"'}

    agent = object.__new__(BrowserAgent)
    agent.service = Service()  # type: ignore[attr-defined]

    async def group(caller: object) -> dict:
        return {"id": "g1"}

    async def tab(group: dict, tab: object) -> dict:
        return {"id": "t1", "url": "https://shop.example/orders"}

    async def audit(*_: object) -> None:
        return None

    agent._group = group  # type: ignore[method-assign]
    agent._tab = tab  # type: ignore[method-assign]
    agent._audit = audit  # type: ignore[method-assign]
    agent._origin = lambda caller: {"actor": "agent"}  # type: ignore[method-assign]
    said = await agent.dialog(object(), accept=True)  # type: ignore[arg-type]
    assert said.startswith("The dialog was accepted.") and '- row "Invoice 42"' in said and "shop.example" in said
