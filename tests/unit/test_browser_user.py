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

from daedalus.browser.agent import SensitiveAsk
from daedalus.browser.model import USER, Owner, group_id
from daedalus.browser.user import CONSENT, ConsentNeeded, site_granted, site_of
from daedalus.config import BrowserConfig, BrowserRuleConfig, BrowserUserConfig, RuntimeConfig, Settings
from daedalus.extensions import browser as browser_extension
from daedalus.extensions.api import build_app
from daedalus.host import prompts
from daedalus.host.policy import ALLOW, ASK, Decision, browser_sensitive
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.browser import BROWSER_TOOLS
from tests.support.fake_browserd import Element, FakeBrowserd

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
    for kind in ("purchase", "send", "destroy", "credentials"):
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
