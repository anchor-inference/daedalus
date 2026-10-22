"""The operator's own Chrome as the ``user`` environment, in a real session manager against two
in-process daemons (the agent's browser and the user daemon): the operator alone switches a
conversation there, each new site is asked about, what would change the real profile is refused,
consequential actions always ask, response bodies wait for a grant, and every result says which
browser it is about."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tools import ToolContext

from daedalus.browser.agent import SensitiveAsk, Step, _address
from daedalus.browser.model import USER, InvalidRequest, Owner, group_id
from daedalus.browser.user import CONSENT, ConsentNeeded, SkipNeedsConfirm, site_granted, site_of
from daedalus.config import BrowserConfig, BrowserRuleConfig, BrowserUserConfig, RuntimeConfig, Settings
from daedalus.extensions import browser as browser_extension
from daedalus.extensions.api import build_app
from daedalus.host import prompts
from daedalus.host.policy import ALLOW, ASK, Decision, browser_sensitive
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.browser import BROWSER_TOOLS
from tests.support.fake_browserd import Element, FakeBrowserd
from tests.unit.test_browser_service import wait_until

CONNECTED = {"connected": True, "profile_id": "p1", "profile_name": "Work", "ext_version": "0.1.0", "chrome_version": "141.0.7390.54", "platform": "linux",
             "transport": "native", "since": "2026-10-20T10:00:00Z", "last_seen": "2026-10-20T10:00:05Z"}


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="bu-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def agents_browser(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "b").start()
    yield fake
    await fake.stop()


@pytest.fixture
async def chrome(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = FakeBrowserd(base / "u", env="user")
    fake.relay_status = dict(CONNECTED)
    fake.page("https://mail.example.com/", title="Inbox", heading="Inbox", text="three new messages", elements={
        "e1": Element("e1", "button", "Send"),
        "e2": Element("e2", "button", "Archive"),
    })
    fake.page("https://shop.example.co.uk/", title="Shop", elements={"e5": Element("e5", "button", "Buy now")})
    yield await fake.start()
    await fake.stop()


class Rig:
    def __init__(self, manager: SessionManager, app: Any) -> None:
        self.manager = manager
        self.app = app

    @property
    def service(self) -> Any:
        return self.app.extensions["browser"]

    async def session(self) -> str:
        state = await self.manager.create_session("chat")
        await self.manager.get_state(state.session.id)
        return state.session.id

    async def call(self, sid: str, tool_name: str, **arguments: Any) -> tuple[str, bool]:
        tool = self.manager.tools.get(tool_name)
        assert tool is not None, tool_name
        result = await tool.invoke(ToolContext(tenant_id="t", run_id="r", session_id=sid, metadata={"tool_call_id": "c"}), arguments)
        return str(result.content), bool(result.is_error)

    async def switch(self, sid: str) -> None:
        await self.service.choices.switch(sid, USER, consent=True)

    def pending(self, sid: str) -> dict[str, Any]:
        state = self.manager.live_state(sid)
        return dict(state.metadata.get("policy_pending") or {}) if state is not None else {}


def user_config(**user: Any) -> RuntimeConfig:
    return RuntimeConfig(browser=BrowserConfig(control_wait_seconds=0.2, user=BrowserUserConfig(**user)))


@pytest.fixture
def runtime() -> RuntimeConfig:
    return user_config()


@pytest.fixture
async def rig(settings: Settings, db: Database, base: Path, agents_browser: FakeBrowserd, chrome: FakeBrowserd, runtime: RuntimeConfig) -> AsyncIterator[Rig]:
    configured = settings.model_copy(update={"browser_container_dir": base / "b", "browser_user_dir": base / "u", "native": False})
    manager = SessionManager(configured, runtime, db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    assert await app.extensions["browser"].wait_available("container")
    assert await app.extensions["browser"].wait_available(USER)
    yield Rig(manager, app)
    for task in tasks:
        task.cancel()
    await app.extensions["browser"].close()
    await manager.close()


# -- who decides which browser ----------------------------------------------------------------------


async def test_no_browser_tool_takes_an_environment_so_the_agent_cannot_pick_the_operators_chrome(rig: Rig, agents_browser: FakeBrowserd, chrome: FakeBrowserd) -> None:
    for name in BROWSER_TOOLS:
        tool = rig.manager.tools.get(name)
        assert tool is not None
        assert not {"env", "browser", "environment"} & set(tool.definition.parameters.properties or {}), name
    sid = await rig.session()
    # Chrome is connected and offered, but this conversation was never switched: the agent's own.
    text, failed = await rig.call(sid, "BrowserOpen")
    assert not failed, text
    assert group_id(Owner("session", sid)) in agents_browser.groups and not chrome.groups
    # Even straight through the agent's entry, an argument naming the environment is nobody's word.
    agent = rig.app.extensions["browser_agent"]
    owner = Owner("session", sid, session_id=sid)
    from daedalus.browser.agent import Caller  # noqa: PLC0415 — only this test builds a caller by hand

    async def never(ask: SensitiveAsk) -> tuple[bool, str]:
        raise AssertionError("nothing here should ask")

    caller = Caller(owner=owner, actor=f"agent:{sid}", gate=never, files=None)  # type: ignore[arg-type]
    await agent.run("BrowserOpen", {"env": "user", "environment": "user"}, caller)
    assert not chrome.groups
    assert await rig.service.choices.env(sid) == "agent"


async def test_the_first_switch_asks_for_consent_once_and_the_result_names_the_operators_chrome(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    with pytest.raises(ConsentNeeded) as refused:
        await rig.service.choices.switch(sid, USER, consent=False)
    assert refused.value.details["consent"] == CONSENT
    await rig.service.choices.switch(sid, USER, consent=True)
    # Back and forth afterwards without asking again in this conversation.
    await rig.service.choices.switch(sid, "agent", consent=False)
    await rig.service.choices.switch(sid, USER, consent=False)
    text, failed = await rig.call(sid, "BrowserOpen")
    assert not failed, text
    assert "operator's own Chrome «Work»" in text
    assert group_id(Owner("session", sid), env=USER) in chrome.groups
    row = await rig.service.get_row(group_id(Owner("session", sid), env=USER))
    assert row["env"] == USER and row["profile"] == "user"
    text, _ = await rig.call(sid, "BrowserTabs")
    assert text.startswith("[in the operator's own Chrome «Work»]")
    # The prompt says who switches, without the conversation's state in it (it stays cacheable).
    assert "Only the operator switches browsers" in prompts.BROWSER


async def test_a_closed_chrome_is_said_plainly_and_never_falls_back_to_the_agents_browser(rig: Rig, chrome: FakeBrowserd, agents_browser: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    chrome.relay_status = {"connected": False, "last_error": "Chrome closed the connection", "since": None, "last_seen": None}
    text, failed = await rig.call(sid, "BrowserOpen")
    assert failed and "not connected" in text.lower()
    assert not agents_browser.groups


# -- sites ---------------------------------------------------------------------------------------


async def test_a_new_site_asks_the_operator_and_a_yes_grants_that_site_for_the_conversation(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://mail.example.com/", why="read the newest message")
    assert failed and "Approval key" in text
    tab = chrome.tab_of(group_id(Owner("session", sid), env=USER))
    assert tab.page.url == "about:blank", "nothing was opened before the yes"
    (key, asked), = rig.pending(sid).items()
    assert "example.com" in asked["text"] and "your own Chrome" in asked["text"]
    await rig.manager.grant(sid, key, via="test")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://mail.example.com/", why="read the newest message")
    assert not failed, text
    assert tab.page.url == "https://mail.example.com/"
    assert (await rig.service.choices.get(sid))["sites"] == ["example.com"]
    # Its subdomains came with it; another site asks again.
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://news.example.com/")
    assert not failed, text
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://shop.example.co.uk/")
    assert failed and "Approval key" in text
    assert "example.co.uk" in next(iter(v["text"] for v in rig.pending(sid).values()))
    # The grant is this conversation's: another one asks for the same site.
    other = await rig.session()
    await rig.switch(other)
    await rig.call(other, "BrowserOpen")
    text, failed = await rig.call(other, "BrowserNavigate", url="https://mail.example.com/")
    assert failed and "Approval key" in text


@pytest.mark.parametrize("runtime", [user_config(granted_sites=["example.com"])])
async def test_the_operators_always_granted_sites_do_not_ask(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    text, failed = await rig.call(sid, "BrowserOpen", url="https://mail.example.com/")
    assert not failed, text
    assert not rig.pending(sid)
    # And the user daemon's guard is given them, present even when empty: every other site is asked.
    assert chrome.wall is not None and chrome.wall["egress_allow"] == ["example.com"]


async def test_a_staff_members_site_question_goes_to_the_operator_not_its_orchestrator() -> None:
    ask = SensitiveAsk(Decision(ASK, "x", "browser.site", key="k"), "BrowserNavigate", "s-1-u", "open", ["site"], "https://example.com", "example.com", "example.com", 0, "")
    assert ask.network is False
    assert "your own Chrome" in ask.said()


def test_a_site_is_the_registrable_domain_and_covers_its_subdomains() -> None:
    assert site_of("https://a.b.example.com/x?q=1") == "example.com"
    assert site_of("shop.example.co.uk") == "example.co.uk"
    assert site_of("mail.yandex.ru") == "yandex.ru"
    assert site_of("10.0.0.1") == "10.0.0.1"
    assert site_granted("news.example.com", ["example.com"]) and site_granted("example.com", ["example.com"])
    assert not site_granted("badexample.com", ["example.com"])


# -- what is refused there --------------------------------------------------------------------------


async def test_cookie_import_clear_fresh_and_profile_clear_are_refused_in_the_operators_chrome(rig: Rig, chrome: FakeBrowserd, base: Path) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    text, failed = await rig.call(sid, "BrowserOpen", fresh=True)
    assert failed and "agent's own browser" in text
    assert not chrome.groups
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserCookies", action="import", text="a=b", domain="example.com")
    assert failed and "real profile" in text
    text, failed = await rig.call(sid, "BrowserCookies", action="clear", domain="example.com")
    assert failed and "real profile" in text
    assert not [m for m, _ in chrome.calls if m in ("cookies.set", "cookies.clear")], "refused before the daemon was asked"
    from daedalus.browser.model import Forbidden  # noqa: PLC0415

    with pytest.raises(Forbidden):
        await rig.service.profile_action(USER, "user", "clear")
    assert not [m for m, _ in chrome.calls if m.startswith("profile.")]


async def test_a_sign_in_is_handed_to_the_operator_rather_than_typed_into_their_profile(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="https://login.example.com/", reason="to read the inbox")
    assert not failed, text
    assert "Asked the operator" in text and "agent's window" in text
    row = await rig.service.get_row(group_id(Owner("session", sid), env=USER))
    assert row["control_owner"] == "paused"


@pytest.mark.parametrize("runtime", [RuntimeConfig(browser=BrowserConfig(control_wait_seconds=0.2, user=BrowserUserConfig(granted_sites=["example.com"]),
                                                                       rules=[BrowserRuleConfig(domain="*.example.com", action="allow")]))])
async def test_sending_always_asks_in_the_operators_chrome_whatever_the_rules_allow(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    await rig.call(sid, "BrowserOpen", url="https://mail.example.com/")
    await rig.call(sid, "BrowserSnapshot")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e1", element="the Send button")
    assert failed and "Approval key" in text


def test_rules_cannot_lift_a_consequential_kind_in_user_but_still_lift_others() -> None:
    rules = [BrowserRuleConfig(domain="shop.test", action="allow")]
    common = {"tool": "BrowserAct", "group": "g", "host": "shop.test", "page_origin": "https://shop.test", "action": "click", "name": "Buy", "text": "", "rules": rules}
    for kind in ("purchase", "send", "destroy", "credentials", "submit"):
        assert browser_sensitive(kinds=[kind], env="user", **common).action == ASK, kind
    assert browser_sensitive(kinds=["purchase"], **common).action == ALLOW, "the agent's own browser keeps the rule"
    assert browser_sensitive(kinds=["accept"], env="user", **common).action == ALLOW


async def test_a_response_body_waits_for_the_operator_to_turn_bodies_on(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    chrome.pages["https://mail.example.com/"].requests = [{"url": "https://mail.example.com/api/messages", "method": "GET", "type": "fetch", "status": 200, "mime": "application/json", "body": '{"secret": "inbox"}'}]
    await rig.service.choices.grant_site(sid, "example.com")
    await rig.call(sid, "BrowserOpen", url="https://mail.example.com/")
    listing, _ = await rig.call(sid, "BrowserNetwork")
    assert "r1" in listing, listing
    rid = "r1"
    text, failed = await rig.call(sid, "BrowserNetwork", id=rid, body=True)
    assert "inbox" not in text and "unless they turn it on" in text
    assert not [p for m, p in chrome.calls if m == "page.request" and p.get("body")], "the body was never even asked for"
    rig.manager.config.browser.user.bodies = True
    text, failed = await rig.call(sid, "BrowserNetwork", id=rid, body=True)
    assert "inbox" in text, text


@pytest.mark.parametrize("runtime", [user_config(granted_sites=["paypal.com"])])
async def test_watch_mode_is_on_by_default_in_the_operators_chrome(rig: Rig, chrome: FakeBrowserd) -> None:
    assert rig.manager.config.browser.watch_mode is False
    sid = await rig.session()
    await rig.switch(sid)
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://www.paypal.com/")
    assert failed and "watches the agent" in text


# -- the routes -------------------------------------------------------------------------------------


async def test_the_switch_route_asks_for_consent_and_the_listing_carries_the_choice(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    headers = {"x-daedalus-token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        listing = (await client.get(f"/api/browsers?session={sid}", headers=headers)).json()
        assert listing["user"] == {"env": "agent", "consented": False, "connected": True, "name": "Work"}
        refused = await client.put(f"/api/browser/user/sessions/{sid}", json={"env": "user"}, headers=headers)
        assert refused.status_code == 409 and refused.json()["code"] == "consent_needed" and refused.json()["consent"] == CONSENT
        done = await client.put(f"/api/browser/user/sessions/{sid}", json={"env": "user", "consent": True}, headers=headers)
        assert done.status_code == 200 and done.json()["env"] == "user"
        listing = (await client.get(f"/api/browsers?session={sid}", headers=headers)).json()
        assert listing["user"]["env"] == "user" and listing["user"]["consented"] is True
        status = (await client.get("/api/browser/user", headers=headers)).json()
        assert status["daemon"] is True and status["relay"]["profile_name"] == "Work" and status["native"] is False
        check = await client.post("/api/browser/user/check", headers=headers)
        assert check.status_code == 200 and check.json()["rtt_ms"] == 12 and chrome.notified
        # The connector is the desktop's: a server says so rather than pretending.
        assert (await client.post("/api/browser/user/register", headers=headers)).status_code == 501
        assert (await client.get("/api/browser/user")).status_code == 401


async def test_a_popup_check_and_the_relay_coming_and_going_reach_the_app(rig: Rig, chrome: FakeBrowserd) -> None:
    from daedalus.host.events import EventFilter  # noqa: PLC0415

    subscription = rig.manager.bus.subscribe(EventFilter(types=("browser.user",)), name="test")
    chrome.emit("relay.check", {"n": 3, "profile_id": "p1", "profile_name": "Work"})
    chrome.emit("relay.lost", {"reason": "closed", "profile_id": "p1", "profile_name": "Work"})
    seen = []
    async with asyncio.timeout(5):
        async for event in subscription:
            seen.append(dict(event.payload))
            if len(seen) == 2:
                break
    assert seen == [{"kind": "check", "profile_name": "Work"}, {"kind": "lost", "profile_name": "Work", "reason": "closed"}]


# -- the agent asks for the operator's Chrome --------------------------------------------------------


def woken(rig: Rig) -> list[tuple[Owner, str]]:
    """What the agent is woken with, kept instead of starting a run."""
    told: list[tuple[Owner, str]] = []

    async def wake(owner: Owner, text: str) -> None:
        told.append((owner, text))

    rig.service.wake = wake
    return told


async def asked(rig: Rig, sid: str) -> str:
    text, failed = await rig.call(sid, "BrowserAskOperatorChrome", reason="read the anchorinference page signed in as you")
    assert failed and "Approval key" in text and "End your turn" in text, text
    (key, request), = rig.pending(sid).items()
    assert key in text
    assert request["on_answer"] == "browser.user.switch"
    assert "The agent asks to work in your Chrome «Work»: read the anchorinference page signed in as you" in request["text"]
    assert CONSENT in request["text"], "the card carries the consent summary the panel's first switch shows"
    return str(key)


async def test_the_agents_ask_switches_nothing_until_the_operator_says_yes(rig: Rig, chrome: FakeBrowserd, agents_browser: FakeBrowserd) -> None:
    sid = await rig.session()
    key = await asked(rig, sid)
    assert await rig.service.choices.get(sid) == {"env": "agent", "consented": False, "sites": [], "approvals": "manual"}
    # Asking again, or opening the browser meanwhile, moves nothing either; the result names the browser.
    await rig.call(sid, "BrowserAskOperatorChrome", reason="read the anchorinference page signed in as you")
    text, failed = await rig.call(sid, "BrowserOpen")
    assert not failed, text
    assert "the agent's own browser (not the operator's Chrome «Work»)" in text
    assert not chrome.groups and await rig.service.choices.env(sid) == "agent"
    # The yes is the operator's alone: an orchestrator answering it is refused and nothing changes.
    with pytest.raises(ValueError, match="only the operator"):
        await rig.manager.grant(sid, key, via="orchestrator")
    assert await rig.service.choices.env(sid) == "agent" and key in rig.pending(sid)


async def test_the_operators_yes_switches_the_conversation_as_the_panel_does(rig: Rig, chrome: FakeBrowserd) -> None:
    from daedalus.host.events import EventFilter  # noqa: PLC0415

    told = woken(rig)
    subscription = rig.manager.bus.subscribe(EventFilter(types=("browser.user",)), name="test")
    sid = await rig.session()
    key = await asked(rig, sid)
    await rig.manager.grant(sid, key, via="app")
    # The state the panel's switch writes, consent recorded with it.
    panel = await rig.session()
    await rig.service.switch_conversation(panel, USER, consent=True)
    assert await rig.service.choices.get(sid) == await rig.service.choices.get(panel) == {"env": USER, "consented": True, "sites": [], "approvals": "manual"}
    async with asyncio.timeout(5):
        async for event in subscription:
            assert event.payload["kind"] == "switched" and event.session_id == sid
            break
    # No grant is left for the agent's next call to spend, and the agent hears the answer.
    state = rig.manager.live_state(sid)
    assert state is not None and not state.metadata.get("policy_grants") and not rig.pending(sid)
    assert told and told[-1][0].session_id == sid and "switched this conversation to their own Chrome «Work»" in told[-1][1]
    text, failed = await rig.call(sid, "BrowserOpen")
    assert not failed, text
    assert "operator's own Chrome «Work»" in text and group_id(Owner("session", sid), env=USER) in chrome.groups


async def test_the_operators_no_leaves_the_conversation_in_the_agents_browser(rig: Rig, chrome: FakeBrowserd) -> None:
    told = woken(rig)
    sid = await rig.session()
    key = await asked(rig, sid)
    await rig.manager.refuse(sid, key, via="app")
    assert await rig.service.choices.get(sid) == {"env": "agent", "consented": False, "sites": [], "approvals": "manual"}
    assert not rig.pending(sid)
    assert "declined" in told[-1][1] and "your own browser" in told[-1][1]


async def test_a_chrome_that_is_not_connected_is_said_plainly_and_nothing_is_asked(rig: Rig, chrome: FakeBrowserd, agents_browser: FakeBrowserd) -> None:
    sid = await rig.session()
    chrome.relay_status = {"connected": False, "last_error": "Chrome closed the connection", "since": None, "last_seen": None}
    chrome.emit("relay.lost", {"reason": "closed", "profile_id": "p1", "profile_name": "Work"})
    async with asyncio.timeout(5):
        while rig.service.user_connection()[0]:
            await asyncio.sleep(0.02)
    text, failed = await rig.call(sid, "BrowserAskOperatorChrome", reason="read a page")
    assert failed and "not connected" in text and "Settings → Environments → Your Chrome" in text and "Chrome closed the connection" in text
    assert not rig.pending(sid) and not agents_browser.groups and not chrome.groups


@pytest.mark.parametrize("runtime", [user_config(enabled=False)])
async def test_an_installation_that_does_not_offer_the_operators_chrome_says_so(rig: Rig) -> None:
    sid = await rig.session()
    text, failed = await rig.call(sid, "BrowserAskOperatorChrome", reason="read a page")
    assert failed and "not offered" in text and "Settings → Environments → Your Chrome" in text
    assert not rig.pending(sid)
    state = await rig.manager.get_state(sid)
    assert state is not None
    assert [await hook(state) for hook in rig.manager.turn_line_hooks] == [""]


async def test_the_turn_line_says_where_the_conversation_is_and_stays_the_same_between_turns(rig: Rig, chrome: FakeBrowserd) -> None:
    from protocore.contracts.types import Message, MessageRole, TextBlock  # noqa: PLC0415

    sid = await rig.session()
    state = await rig.manager.get_state(sid)
    assert state is not None
    message = Message(role=MessageRole.user, content_blocks=[TextBlock(text="Попробуй зайти на сайт в моём браузере")])
    first = (await rig.manager._with_turn_context(state, message)).content_blocks[0].text  # type: ignore[union-attr]
    second = (await rig.manager._with_turn_context(state, message)).content_blocks[0].text  # type: ignore[union-attr]
    line = "- Browser: the operator's Chrome «Work» is connected, but this conversation uses your own browser, not theirs."
    assert line in first and line in second
    assert [x for x in first.splitlines() if x.startswith("- Browser:")] == [x for x in second.splitlines() if x.startswith("- Browser:")]
    assert "BrowserAskOperatorChrome" in first
    await rig.switch(sid)
    third = (await rig.manager._with_turn_context(state, message)).content_blocks[0].text  # type: ignore[union-attr]
    assert "- Browser: the operator gave this conversation their own Chrome «Work»; the browser tools work there." in third
    # The static rules point at the ask, with both languages' words, and keep the conversation's state out.
    assert "BrowserAskOperatorChrome" in prompts.BROWSER and "в моём браузере" in prompts.BROWSER and "«Work»" not in prompts.BROWSER
    hint = rig.manager.tools.get("BrowserAskOperatorChrome").search_hint  # type: ignore[union-attr]
    assert "my browser" in hint and "мой браузер" in hint and "моём браузере" in hint


async def test_a_staff_members_ask_goes_to_the_operator(rig: Rig, chrome: FakeBrowserd) -> None:
    from daedalus.host.events import EventFilter  # noqa: PLC0415

    told = woken(rig)
    subscription = rig.manager.bus.subscribe(EventFilter(types=("permission.pending",)), name="test")
    sid = await rig.session()
    state = rig.manager.live_state(sid)
    assert state is not None
    state.metadata.update({"staff_session_id": "live-1", "staff_id": "m1"})
    key = await asked(rig, sid)
    async with asyncio.timeout(5):
        async for event in subscription:
            payload = dict(event.payload)
            break
    assert payload["request_id"] == key and payload["routed_to"] == "operator" and payload["chrome"]["name"] == "Work"
    with pytest.raises(ValueError, match="only the operator"):
        await rig.manager.refuse(sid, key, via="orchestrator")
    await rig.manager.grant(sid, key, via="app")
    assert await rig.service.choices.env(sid) == USER
    assert told[-1][0].staff_id == "m1"


async def test_a_command_line_members_browser_cannot_ask_to_move(rig: Rig, chrome: FakeBrowserd) -> None:
    from daedalus.browser.agent import Caller  # noqa: PLC0415 — the command-line member's caller, by hand

    async def never(ask: SensitiveAsk) -> tuple[bool, str]:
        raise AssertionError("nothing here should ask")

    caller = Caller(owner=Owner("staff", "m1", staff_id="m1"), actor="staff:m1", gate=never, files=None)  # type: ignore[arg-type]
    text, failed = await rig.app.extensions["browser_agent"].run("BrowserAskOperatorChrome", {"reason": "read a page"}, caller)
    assert failed and "cannot move into the operator's Chrome" in text


# -- the approval modes ------------------------------------------------------------------------------


async def acting_page(chrome: FakeBrowserd) -> None:
    chrome.page("https://mail.example.com/", title="Inbox", heading="Inbox", text="three new messages", elements={
        "e1": Element("e1", "button", "Send"),
        "e3": Element("e3", "button", "Accept the terms"),
    })


async def test_manual_asks_about_a_new_site_and_a_routine_action_as_before(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    assert (await rig.service.choices.get(sid))["approvals"] == "manual"
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://mail.example.com/")
    assert failed and "Approval key" in text


async def test_automatic_grants_new_sites_and_routine_asks_but_still_asks_before_sending(rig: Rig, chrome: FakeBrowserd) -> None:
    await acting_page(chrome)
    sid = await rig.session()
    await rig.switch(sid)
    await rig.service.choices.set_approvals(sid, "auto")
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://mail.example.com/")
    assert not failed and "without asking" in text and "approve automatically" in text, text
    assert not rig.pending(sid) and (await rig.service.choices.get(sid))["sites"] == ["example.com"]
    await rig.call(sid, "BrowserSnapshot")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e3", element="the Accept button")
    assert not failed and "Done without asking (accept)" in text, text
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e1", element="the Send button")
    assert failed and "Approval key" in text
    audit = await rig.service.audit_log(group_id(Owner("session", sid), env=USER))
    assert any(e["action"] == "sensitive" and e["detail"].get("by") == "auto" for e in audit)
    assert any(e["action"] == "act" and e["detail"].get("decision") == "allowed_auto" for e in audit)


@pytest.mark.parametrize("runtime", [RuntimeConfig(browser=BrowserConfig(control_wait_seconds=0.2, user=BrowserUserConfig(),
                                                                       rules=[BrowserRuleConfig(domain="mail.example.com", action="deny", kinds=["destroy"])]))])
async def test_skip_asks_about_nothing_yet_never_lifts_a_refusal(rig: Rig, chrome: FakeBrowserd) -> None:
    chrome.page("https://mail.example.com/", title="Inbox", elements={"e1": Element("e1", "button", "Send"), "e4": Element("e4", "button", "Delete the account")})
    sid = await rig.session()
    await rig.switch(sid)
    with pytest.raises(SkipNeedsConfirm):  # the first skip needs the operator's yes
        await rig.service.choices.set_approvals(sid, "skip")
    await rig.service.choices.set_approvals(sid, "skip", confirm=True)
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://mail.example.com/")
    assert not failed, text
    await rig.call(sid, "BrowserSnapshot")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e1", element="the Send button")
    assert not failed and "skip all approvals" in text, text
    # The operator's rule refuses deleting here, and skipping approvals is not a way around it.
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e4", element="the Delete button")
    assert failed and "refused by policy" in text
    assert not rig.pending(sid)
    # The built-in refusals stand too: a cookie value is not read in the operator's Chrome.
    text, failed = await rig.call(sid, "BrowserCookies", action="import", file="cookies.txt")
    assert failed


def test_the_modes_lift_questions_only() -> None:
    from daedalus.host.policy import DENY, approval_mode_lifts  # noqa: PLC0415

    ask = Decision(ASK, "x", "browser.sensitive", key="k")
    assert not approval_mode_lifts("manual", ask, ["site"])
    assert approval_mode_lifts("auto", ask, ["site"]) and approval_mode_lifts("auto", ask, ["accept", "upload"])
    for kind in ("purchase", "send", "destroy", "credentials"):
        assert not approval_mode_lifts("auto", ask, [kind]) and approval_mode_lifts("skip", ask, [kind])
    for final in (Decision(DENY, "no", "browser.rule"), Decision("handoff", "yours", "rule")):
        assert not approval_mode_lifts("skip", final, ["send"])


async def test_only_the_operator_sets_the_mode_and_the_agent_has_no_way_to(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await rig.session()
    await rig.switch(sid)
    headers = {"x-daedalus-token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.put(f"/api/sessions/{sid}/browser-approvals", json={"mode": "auto"})).status_code == 401
        assert (await client.get(f"/api/sessions/{sid}/browser-approvals", headers=headers)).json()["mode"] == "manual"
        refused = await client.put(f"/api/sessions/{sid}/browser-approvals", json={"mode": "skip"}, headers=headers)
        assert refused.status_code == 409 and refused.json()["code"] == "skip_confirm_needed"
        assert (await client.put(f"/api/sessions/{sid}/browser-approvals", json={"mode": "nobody"}, headers=headers)).status_code == 422
        done = await client.put(f"/api/sessions/{sid}/browser-approvals", json={"mode": "auto"}, headers=headers)
        assert done.status_code == 200 and done.json()["mode"] == "auto"
    # No tool the agent has takes a mode, and none of the browser's writes the conversation's choices.
    for tool in rig.manager.tools.list_all():
        assert "approvals" not in repr(tool.definition).lower(), tool.name
    assert (await rig.service.choices.get(sid))["approvals"] == "auto"


# -- Chrome conversations -----------------------------------------------------------------------------


async def test_a_chrome_conversation_is_made_moved_and_marked_through_the_api(rig: Rig, chrome: FakeBrowserd) -> None:
    from daedalus.browser.panel import CHROME_CHAT_LINE  # noqa: PLC0415

    headers = {"x-daedalus-token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        made = (await client.post("/api/sessions", json={"title": "Shop", "kind": "chrome"}, headers=headers)).json()
        assert made["kind"] == "chrome"
        # Choosing a Chrome conversation is the operator's yes: it is in their Chrome already.
        assert await rig.service.choices.env(made["id"]) == USER
        assert (await client.get(f"/api/sessions/{made['id']}", headers=headers)).json()["kind"] == "chrome"
        listed = {row["id"]: row["kind"] for row in (await client.get("/api/sessions", headers=headers)).json()["sessions"]}
        assert listed[made["id"]] == "chrome"
        computer = (await client.post("/api/sessions", json={"title": "Desk", "kind": "computer"}, headers=headers)).json()
        assert computer["kind"] == "computer" and await rig.service.choices.env(computer["id"]) != USER
        changed = await client.post(f"/api/sessions/{computer['id']}/kind", json={"kind": "chat"}, headers=headers)
        assert changed.status_code == 200 and changed.json() == {"id": computer["id"], "kind": "chat"}
        assert (await client.post(f"/api/sessions/{computer['id']}/kind", json={"kind": "robot"}, headers=headers)).status_code == 422
        assert (await client.post("/api/sessions/nope/kind", json={"kind": "chat"}, headers=headers)).status_code == 404
        # A plain chat the operator moves to their Chrome becomes a Chrome conversation, and stays one.
        plain = await rig.session()
        await client.put(f"/api/browser/user/sessions/{plain}", json={"env": "user", "consent": True}, headers=headers)
        state = await rig.manager.get_state(plain)
        assert state is not None and state.kind == "chrome"
        await client.put(f"/api/browser/user/sessions/{plain}", json={"env": "agent"}, headers=headers)
        assert state.kind == "chrome"
    # Its turns carry the side panel's browser-first rule, in words fit for the app.
    lines = [await hook(state) for hook in rig.manager.turn_line_hooks]
    assert not any(CHROME_CHAT_LINE in line for line in lines)
    await rig.switch(plain)
    lines = [await hook(state) for hook in rig.manager.turn_line_hooks]
    assert any(CHROME_CHAT_LINE in line for line in lines)


@pytest.mark.parametrize("runtime", [user_config(enabled=False)])
async def test_a_chrome_conversation_is_refused_where_chrome_is_not_offered(rig: Rig) -> None:
    headers = {"x-daedalus-token": "tok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(rig.app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        refused = await client.post("/api/sessions", json={"title": "Shop", "kind": "chrome"}, headers=headers)
        assert refused.status_code == 409 and "Your Chrome" in refused.json()["detail"]
        assert all(row["title"] != "Shop" for row in (await client.get("/api/sessions", headers=headers)).json()["sessions"])


# -- the agent's tabs there -------------------------------------------------------------------------

GRANTED = user_config(granted_sites=["example.com"])


def documents(chrome: FakeBrowserd) -> None:
    chrome.page("https://tax.example.com/documents", title="Documents", heading="Documents", elements={
        "e1": Element("e1", "row", "Tax statement 2026", tag="tr", detaches=True),
        "e2": Element("e2", "button", "Filter"),
    })
    chrome.page("https://tax.example.com/claim", title="Claim", elements={
        "e3": Element("e3", "button", "File the claim", submits=True, form_method="post"),
        "e4": Element("e4", "button", "Find", submits=True, form_method="get"),
    })


async def opened(rig: Rig, chrome: FakeBrowserd, url: str = "https://tax.example.com/documents") -> str:
    documents(chrome)
    sid = await rig.session()
    await rig.switch(sid)
    text, failed = await rig.call(sid, "BrowserOpen", url=url)
    assert not failed, text
    return sid


def tabs_of(chrome: FakeBrowserd, sid: str) -> list[str]:
    return [t.page.url for t in chrome.groups[group_id(Owner("session", sid), env=USER)].tabs]


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_a_new_tab_on_an_open_address_switches_to_it_and_the_result_marks_the_new_one(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://tax.example.com/documents/")
    assert not failed and "already shows" in text and "switched to it" in text, text
    assert len(tabs_of(chrome, sid)) == 1
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://mail.example.com/")
    assert not failed and text.startswith("[in the operator's own Chrome «Work»]\nOpened tab t2 on https://mail.example.com/."), text
    assert "https://mail.example.com/ (active; new)" in text
    # Opened blank and then sent there, as one tab: no stray blank tab beside it.
    assert tabs_of(chrome, sid) == ["https://tax.example.com/documents", "https://mail.example.com/"]
    # BrowserOpen of an address a tab already shows brings that tab forward rather than navigating.
    text, failed = await rig.call(sid, "BrowserOpen", url="https://tax.example.com/documents")
    assert not failed and "Tab t1 already shows" in text, text
    assert tabs_of(chrome, sid) == ["https://tax.example.com/documents", "https://mail.example.com/"]
    assert chrome.groups[group_id(Owner("session", sid), env=USER)].active == "t1"


def test_a_route_in_the_fragment_is_a_different_page_and_an_anchor_is_not() -> None:
    assert _address("https://mail.example.com/#/inbox") != _address("https://mail.example.com/#/settings")
    assert _address("https://mail.example.com/#!/inbox") != _address("https://mail.example.com/#!/settings")
    assert _address("https://mail.example.com/#/inbox/") == _address("https://mail.example.com/#/inbox")
    assert _address("https://example.com/guide#install") == _address("https://example.com/guide#usage") == _address("https://example.com/guide/")
    assert _address("https://Example.com/a/") == _address("https://example.com/a")


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_a_tab_on_another_route_of_the_same_app_is_not_taken_for_it(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome, "https://mail.example.com/#/inbox")
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://mail.example.com/#/settings")
    assert not failed and "already shows" not in text and "Opened tab t2" in text, text


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_a_site_still_waiting_for_the_operator_leaves_no_blank_tab(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://shop.example.co.uk/")
    assert failed and "Approval key" in text
    assert len(tabs_of(chrome, sid)) == 1, "nothing was opened before the yes"
    (key, _), = rig.pending(sid).items()
    await rig.manager.grant(sid, key, via="test")
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://shop.example.co.uk/")
    assert not failed, text
    assert tabs_of(chrome, sid) == ["https://tax.example.com/documents", "https://shop.example.co.uk/"]


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_three_tabs_are_the_most_kept_and_blank_ones_make_room_first(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    await rig.call(sid, "BrowserTabs", action="new")
    await rig.call(sid, "BrowserTabs", action="select", tab="t1")
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://mail.example.com/")
    assert not failed, text
    assert len(tabs_of(chrome, sid)) == 3
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://news.example.com/")
    assert not failed and "Closed t2 first (dead or blank)" in text, text
    assert tabs_of(chrome, sid) == ["https://tax.example.com/documents", "https://mail.example.com/", "https://news.example.com/"]
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://docs.example.com/")
    assert failed and "You already have 3 tabs" in text and "nothing was opened" in text, text
    assert "https://news.example.com/" in text
    assert len(tabs_of(chrome, sid)) == 3


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_a_click_that_kills_its_tab_is_said_at_once_and_held_back_once_when_repeated(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    await rig.call(sid, "BrowserSnapshot")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e1", element="the tax statement row")
    assert not failed and "Tab t1 stopped answering right after this" in text and "Do not repeat this action" in text, text
    text, failed = await rig.call(sid, "BrowserText")
    assert failed and "Tab t1 no longer answers" in text and "BrowserClose(tab='t1')" in text, text
    listing, _ = await rig.call(sid, "BrowserTabs")
    assert "(active; dead: close it)" in listing, listing
    # The same row in a second tab is held back once, and goes through when the agent insists.
    await rig.call(sid, "BrowserTabs", action="new", url="https://tax.example.com/documents")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e1", element="the statement row again")
    assert failed and "earlier left its tab dead" in text and "send it again" in text, text
    assert not chrome.tab_of(group_id(Owner("session", sid), env=USER)).detached
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e1", element="the statement row again")
    assert not failed and "stopped answering" in text
    # A dead tab is the first to go when room is needed.
    text, failed = await rig.call(sid, "BrowserTabs", action="new", url="https://mail.example.com/")
    assert not failed, text


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_an_address_that_is_a_pdf_arrives_as_a_download_and_the_tab_stays(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    chrome.documents["https://tax.example.com/statement.pdf"] = ("statement.pdf", b"%PDF-1.7 statement")
    text, failed = await rig.call(sid, "BrowserNavigate", url="https://tax.example.com/statement.pdf")
    assert not failed and "saved as the download statement.pdf" in text and "Tab t1 stayed on https://tax.example.com/documents" in text, text
    text, failed = await rig.call(sid, "BrowserDownload", name="statement.pdf")
    assert not failed, text


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_chromes_own_words_from_an_older_daemon_are_read_as_a_dead_tab_too(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    chrome.fail["page.snapshot"] = (-32603, "reading the page: Runtime.evaluate: the debugger is not attached to this tab (detached) (-32000)", {})
    text, failed = await rig.call(sid, "BrowserSnapshot")
    assert failed and "Tab t1 no longer answers" in text, text
    listing, _ = await rig.call(sid, "BrowserTabs")
    assert "dead: close it" in listing
    text, failed = await rig.call(sid, "BrowserClose", tab="t1")
    assert not failed and "dead" not in text


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_a_posted_form_waits_for_the_operator_while_read_only_and_a_search_does_not(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome, "https://tax.example.com/claim")
    await rig.call(sid, "BrowserSnapshot")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e4", element="the Find button")
    assert not failed, text
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e3", element="the File the claim button")
    assert failed and "Approval key" in text and "submit a form" in text, text


@pytest.mark.parametrize("runtime", [user_config(granted_sites=["example.com"], read_only=False)])
async def test_with_read_only_off_a_posted_form_goes_through(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome, "https://tax.example.com/claim")
    await rig.call(sid, "BrowserSnapshot")
    text, failed = await rig.call(sid, "BrowserAct", action="click", ref="e3", element="the File the claim button")
    assert not failed, text


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_the_tabs_close_when_the_turn_ends_unless_the_operator_holds_them(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    gid = group_id(Owner("session", sid), env=USER)
    chrome.set_control(gid, "paused", reason="the operator took it")
    await wait_until(lambda: _control(rig, gid), "paused")
    assert not await browser_extension.close_turn_tabs(rig.service, sid)
    chrome.set_control(gid, "agent")
    await wait_until(lambda: _control(rig, gid), "agent")
    assert await browser_extension.close_turn_tabs(rig.service, sid)
    row = await rig.service.get_row(gid)
    assert row["status"] == "closed" and row["close_reason"] == "turn_end"
    assert gid not in chrome.groups
    text, failed = await rig.call(sid, "BrowserSnapshot")
    assert failed and "BrowserOpen" in text


@pytest.mark.parametrize("runtime", [user_config(granted_sites=["example.com"], close_tabs=False)])
async def test_the_operator_may_keep_the_tabs_open_after_the_turn(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    assert not await browser_extension.close_turn_tabs(rig.service, sid)
    assert (await rig.service.get_row(group_id(Owner("session", sid), env=USER)))["status"] == "open"


@pytest.mark.parametrize("runtime", [GRANTED])
async def test_the_operator_closing_the_group_in_chrome_is_kept_as_theirs(rig: Rig, chrome: FakeBrowserd) -> None:
    sid = await opened(rig, chrome)
    gid = group_id(Owner("session", sid), env=USER)
    group = chrome.groups.pop(gid)
    chrome.emit("group.closed", {"group_id": gid, "browser_id": group.browser_id, "profile": group.profile, "labels": group.labels, "reason": "closed_in_chrome"})
    await wait_until(lambda: _status(rig, gid), "closed")
    row = await rig.service.get_row(gid)
    assert row["close_reason"] == "closed_in_chrome"
    log = await rig.service.audit_log(gid)
    assert any(e["action"] == "close" and e["actor"] == "operator" for e in log), log


async def _control(rig: Rig, gid: str) -> str:
    return str((await rig.service.get_row(gid))["control_owner"])


async def _status(rig: Rig, gid: str) -> str:
    return str((await rig.service.get_row(gid))["status"])


def test_text_on_a_click_is_refused_rather_than_ignored() -> None:
    with pytest.raises(InvalidRequest, match="text is read by type and scroll only"):
        Step(action="click", element="the item", ref="e50", text="Смотреть").check()
    Step(action="click", element="the item", ref="e50", text="  ").check()
    Step(action="type", element="the box", ref="e1", text="").check()
    Step(action="scroll", element="the page", text="Total").check()
