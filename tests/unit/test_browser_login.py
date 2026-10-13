"""The operator's private sign-in end to end against the in-process daemon: BrowserLogin asks through
the group's "needs you", the form's answer is typed into the page by the daemon, the agent is woken with
where the page ended and whether it took, and the credentials reach nothing else — not a tool result,
an event, the audit, the log, a stored row, the transcript or a model's input. A saved sign-in is asked
about once or allowed for the session, and the Inbox entry for a sign-in is a link, never a form."""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.browser.login import LoginConflict, outcome, site_of
from daedalus.config import Settings
from daedalus.extensions import browser as browser_extension
from daedalus.host import prompts
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.browser import BROWSER_TOOLS, READ_ONLY_TOOLS
from tests.support.fake_browserd import Element, FakeBrowserd
from tests.support.models import model_config
from tests.unit.test_browser_tools import Rig, base, daemon, rig  # noqa: F401
from tests.unit.test_session_runner import ScriptedProvider, _wait_finished

USER = "someone@example.com"
PASSWORD = "FAKE-PASSWORD-hunter-77"
WRONG = "FAKE-PASSWORD-not-it-12"
CODE = "FAKE-CODE-9081"


def acme(daemon: FakeBrowserd) -> None:
    daemon.page("https://acme.test/login", title="Sign in", heading="Sign in", elements={
        "e40": Element("e40", "textbox", "Email", tag="input", type="email"),
        "e41": Element("e41", "textbox", "Password", tag="input", type="password"),
        "e42": Element("e42", "button", "Sign in", goes_to="https://acme.test/home"),
    })
    daemon.page("https://acme.test/home", title="Invoices", heading="Your invoices")
    # A form whose button leads back to a page asking for the password: a refused password.
    daemon.page("https://acme.test/refuses", title="Sign in", elements={
        "e50": Element("e50", "textbox", "Email", tag="input", type="email"),
        "e51": Element("e51", "textbox", "Password", tag="input", type="password"),
        "e52": Element("e52", "button", "Sign in", goes_to="https://acme.test/login"),
    })
    daemon.page("https://acme.test/two-factor", title="Sign in", elements={
        "e60": Element("e60", "textbox", "Email", tag="input", type="email"),
        "e61": Element("e61", "textbox", "Password", tag="input", type="password"),
        "e62": Element("e62", "button", "Sign in", goes_to="https://acme.test/code"),
    })
    daemon.page("https://acme.test/code", title="Verify", elements={
        "e70": Element("e70", "textbox", "Code", tag="input", autocomplete="one-time-code"),
        "e71": Element("e71", "button", "Verify", goes_to="https://acme.test/home"),
    })
    daemon.page("https://acme.test/about", title="About", heading="About us")


async def wait_until(check: Any, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def nowhere(text: str, *values: str) -> None:
    for value in values or (USER, PASSWORD, WRONG, CODE):
        assert value not in text, f"a credential got out: {value!r} in {text[:400]!r}"


async def every_stored_text(db: Database) -> str:
    """Every text column of every table, as one string: the transcript, the events, the audit, the
    request manifests and whatever else the host keeps. A sealed value is bytes and is not here."""
    out: list[str] = []
    for table in [r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]:
        for row in await db.fetchall(f'SELECT * FROM "{table}"'):  # noqa: S608 — table names come from the schema
            out.extend(str(v) for v in tuple(row) if isinstance(v, str))
    return "\n".join(out)


def login_of(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["needs_json"])["login"]


async def group_row(rig: Rig, sid: str) -> dict[str, Any]:
    row = await rig.app.db.fetchone("SELECT * FROM browser_groups WHERE id = ?", (f"s-{sid}",))
    assert row is not None
    return dict(row)


def desk(rig: Rig) -> Any:
    return rig.app.extensions["browser_agent"].logins


async def test_the_tool_is_registered_as_a_change_and_taught() -> None:
    assert "BrowserLogin" in BROWSER_TOOLS and "BrowserLogin" not in READ_ONLY_TOOLS
    assert "BrowserLogin(url_or_domain, reason)" in prompts.BROWSER
    assert site_of("https://www.Acme.test/login?next=/") == "acme.test" and site_of("acme.test") == "acme.test" and site_of("www.test") == "www.test"
    said = outcome("acme.test", {"url": "https://acme.test/home?token=abc#x", "signed_in": "yes", "filled": ["username", "password"]})
    assert said == "Sign-in to acme.test: submitted; now at https://acme.test/home; signed in: yes."


async def test_a_sign_in_goes_from_the_form_to_the_page_and_nowhere_else(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    acme(rig.daemon)
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://acme.test/login")
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="read this month's invoices")
    assert not failed, text
    assert "private form" in text and "End your turn" in text
    row = await group_row(rig, sid)
    login = login_of(row)
    assert login["site"] == "acme.test" and login["saved"] is False and login["session_id"] == sid and row["control_owner"] == "paused"
    needs = await rig.events("browser.needs_you")
    assert needs[-1].payload["login"] == {"id": login["id"], "site": "acme.test", "saved": False}
    # Paused for the operator: the agent's own calls wait for the form.
    text, failed = await rig.call(sid, "BrowserSnapshot")
    assert failed and "paused" in text

    result = await desk(rig).submit(f"s-{sid}", login["id"], username=USER, password=PASSWORD)
    assert result == {"site": "acme.test", "url": "https://acme.test/home", "signed_in": "yes", "filled": ["username", "password"], "waiting": "", "saved": False}
    await wait_until(lambda: any("Sign-in to acme.test" in t for _, t in rig.submitted))
    (woken,) = [t for _, t in rig.submitted if "Sign-in to acme.test" in t]
    assert woken == "[browser] Sign-in to acme.test: submitted; now at https://acme.test/home; signed in: yes. Take a BrowserSnapshot before acting."
    row = await group_row(rig, sid)
    assert row["control_owner"] == "agent" and row["needs_json"] is None
    # The daemon got the values; it is the one place they went.
    (sent,) = [p for m, p in rig.daemon.calls if m == "page.login" and not p.get("probe")]
    assert sent["username"] == USER and sent["password"] == PASSWORD and sent["site"] == "acme.test" and sent["origin"] == {"actor": "operator"}
    text, failed = await rig.call(sid, "BrowserSnapshot")
    assert not failed
    nowhere(text)
    audit = await rig.app.extensions["browser"].audit_log(f"s-{sid}")
    (done,) = [e["detail"] for e in audit if e["action"] == "login"]
    assert done == {"site": "acme.test", "filled": ["username", "password"], "signed_in": "yes", "url": "https://acme.test/home", "saved": False, "how": "typed"}
    nowhere(json.dumps(audit) + json.dumps([t for _, t in rig.submitted]) + json.dumps(result))
    nowhere(await every_stored_text(rig.app.db))
    nowhere(caplog.text)
    # The password is masked from here on wherever it is echoed.
    assert PASSWORD not in rig.manager.redactor.redact(f"the page said {PASSWORD}")
    # The request is over: the same form cannot be sent twice.
    with pytest.raises(LoginConflict):
        await desk(rig).submit(f"s-{sid}", login["id"], username=USER, password=PASSWORD)


async def test_a_refusal_a_code_and_a_decline_are_told_in_words(rig: Rig) -> None:
    acme(rig.daemon)
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://acme.test/refuses")
    await rig.call(sid, "BrowserLogin", url_or_domain="https://acme.test/refuses", reason="check the order")
    result = await desk(rig).submit(f"s-{sid}", login_of(await group_row(rig, sid))["id"], username=USER, password=WRONG)
    assert result["signed_in"] == "no"
    await wait_until(lambda: any("signed in: no" in t for _, t in rig.submitted))

    await rig.call(sid, "BrowserNavigate", url="https://acme.test/two-factor")
    await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="check the order")
    result = await desk(rig).submit(f"s-{sid}", login_of(await group_row(rig, sid))["id"], username=USER, password=PASSWORD)
    assert result["signed_in"] == "unknown" and result["waiting"] == "code"
    await wait_until(lambda: any("asks for a one-time code: call BrowserLogin again" in t for _, t in rig.submitted))
    # The code page now: the next request is for the code alone.
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="the code")
    assert not failed, text
    result = await desk(rig).submit(f"s-{sid}", login_of(await group_row(rig, sid))["id"], otp=CODE)
    assert result["signed_in"] == "yes" and result["filled"] == ["otp"]

    await rig.call(sid, "BrowserNavigate", url="https://acme.test/login")
    await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="again")
    await desk(rig).decline(f"s-{sid}", login_of(await group_row(rig, sid))["id"])
    await wait_until(lambda: any("declined to sign in to acme.test" in t for _, t in rig.submitted))
    assert (await group_row(rig, sid))["control_owner"] == "agent"
    nowhere(await every_stored_text(rig.app.db) + json.dumps([t for _, t in rig.submitted]))


async def test_the_agent_is_told_what_to_do_where_no_form_is(rig: Rig) -> None:
    acme(rig.daemon)
    sid = await rig.session()
    # No browser yet: BrowserLogin opens it on the site, as BrowserOpen would.
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="https://acme.test/about", reason="read the invoices")
    assert failed and "no sign-in form on https://acme.test/about" in text and "BrowserLogin again" in text
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="")
    assert failed and "reason is required" in text
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="x", selectors={"login": "#a"})
    assert failed and "selectors takes" in text
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="https://acme.test/login", reason="x", selectors={"username": "#nothing"})
    assert failed and "#nothing" in text
    # Selectors the agent gives are kept for the fill.
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="https://acme.test/login", reason="x", selectors={"username": "#e40", "submit": "#e42"})
    assert not failed, text
    assert login_of(await group_row(rig, sid))["selectors"] == {"username": "#e40", "submit": "#e42"}
    assert (await rig.app.db.fetchone("SELECT needs_json FROM browser_groups WHERE id = ?", (f"s-{sid}",)))["needs_json"]
    assert rig.daemon.pages  # the fake kept its pages
    assert (await group_row(rig, sid))["url"].startswith("https://acme.test/")


async def test_a_saved_sign_in_is_asked_about_then_used_for_the_session(rig: Rig) -> None:
    acme(rig.daemon)
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://acme.test/login")
    await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="invoices")
    result = await desk(rig).submit(f"s-{sid}", login_of(await group_row(rig, sid))["id"], username=USER, password=PASSWORD, save=True)
    assert result["saved"] is True
    store = rig.manager.secrets
    (saved,) = store.saved_logins()
    assert saved.site == "acme.test" and saved.public()["name"] == "login:acme.test" and "password" not in json.dumps(saved.public())
    sealed = await rig.app.db.fetchone("SELECT sealed FROM saved_logins")
    assert PASSWORD.encode() not in bytes(sealed["sealed"]) and USER.encode() not in bytes(sealed["sealed"])
    # A saved sign-in is never one of the agent's secrets.
    assert not store.available(sid) and "login" not in json.dumps(store.environment(sid))

    # The next request offers it; the operator uses it once.
    await rig.call(sid, "BrowserNavigate", url="https://acme.test/login")
    await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="invoices again")
    login = login_of(await group_row(rig, sid))
    assert login["saved"] is True
    result = await desk(rig).submit(f"s-{sid}", login["id"], use_saved="once")
    assert result["signed_in"] == "yes" and store.saved_logins()[0].uses == 1
    # Once is once: asked again; this time for the rest of the session.
    await rig.call(sid, "BrowserNavigate", url="https://acme.test/login")
    text, _ = await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="third")
    assert "private form" in text
    await desk(rig).submit(f"s-{sid}", login_of(await group_row(rig, sid))["id"], use_saved="always")
    await rig.call(sid, "BrowserNavigate", url="https://acme.test/login")
    text, failed = await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="fourth")
    assert not failed and text.startswith("Signed in with the sign-in the operator saved") and "signed in: yes" in text
    nowhere(text)
    # Another session is asked, whatever this one was allowed.
    other = await rig.session("other")
    await rig.call(other, "BrowserOpen", url="https://acme.test/login")
    text, _ = await rig.call(other, "BrowserLogin", url_or_domain="acme.test", reason="elsewhere")
    assert "private form" in text
    # Forgotten, it is gone, and still masked.
    assert await store.forget_login(saved.id) and not store.saved_logins()
    assert PASSWORD not in rig.manager.redactor.redact(PASSWORD)
    nowhere(await every_stored_text(rig.app.db))


async def test_the_form_types_only_on_the_site_it_was_asked_for(rig: Rig) -> None:
    acme(rig.daemon)
    rig.daemon.page("https://evil.test/login", title="Sign in", elements={
        "e80": Element("e80", "textbox", "Email", tag="input", type="email"),
        "e81": Element("e81", "textbox", "Password", tag="input", type="password"),
    })
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://acme.test/login")
    await rig.call(sid, "BrowserLogin", url_or_domain="acme.test", reason="invoices")
    login = login_of(await group_row(rig, sid))
    # The page moved to another site while the form was open.
    group = rig.daemon.groups[f"s-{sid}"]
    rig.daemon.show(group.id, rig.daemon.pages["https://evil.test/login"])
    from daedalus.browser.model import Forbidden  # noqa: PLC0415

    with pytest.raises(Forbidden, match="not acme.test; nothing was typed"):
        await desk(rig).submit(f"s-{sid}", login["id"], username=USER, password=PASSWORD)
    assert not rig.daemon.tab_of(group.id).page.elements["e81"].value


async def test_the_inbox_entry_for_a_sign_in_is_a_link_never_a_form(rig: Rig) -> None:
    from daedalus.extensions.notifications import NotificationRouter  # noqa: PLC0415

    posted: list[Any] = []

    class Service:
        def language(self) -> str:
            return "en"

        def _front(self) -> Any:
            return SimpleNamespace(username="daedalus_bot")

        async def post(self, draft: Any) -> None:
            posted.append(draft)

    router = NotificationRouter(Service(), rig.manager)  # type: ignore[arg-type]
    event = SimpleNamespace(type="browser.needs_you", session_id="abc", project_id=None, staff_id=None, payload={
        "group_id": "s-abc", "reason": "login", "what": "sign in to acme.test: invoices", "url": "https://acme.test/login", "title": "Invoices",
        "login": {"id": "x1", "site": "acme.test", "saved": False},
    })
    await router.handle(event)  # type: ignore[arg-type]
    (draft,) = posted
    assert draft.kind == "browser_login" and draft.title == "Invoices asks you to sign in to acme.test"
    assert "https://t.me/daedalus_bot?startapp=browser_abc" in draft.body and "no password is typed into a chat" in draft.body
    assert [a.id for a in draft.actions] == ["open"]


async def test_a_scripted_run_never_shows_the_model_a_credential(settings: Settings, db: Database, base: Path, daemon: FakeBrowserd, caplog: pytest.LogCaptureFixture) -> None:
    """A whole run, as a real model's would go: the model asks for the sign-in and ends its turn, the
    operator answers the form, and the wake starts the next turn. Neither the model's inputs nor the
    stored transcript nor the log ever holds the username or the password."""
    caplog.set_level(logging.DEBUG)
    acme(daemon)
    provider = ScriptedProvider([
        {"tool": "BrowserOpen", "args": {"url": "https://acme.test/login"}},
        {"tool": "BrowserOpen", "args": {"url": "https://acme.test/login"}},
        {"tool": "BrowserLogin", "args": {"url_or_domain": "acme.test", "reason": "read the invoices"}},
        {"text": "Waiting for the operator to sign in."},
        {"tool": "BrowserSnapshot", "args": {}},
        {"text": "Signed in; the invoices are there."},
    ])
    configured = settings.model_copy(update={"browser_container_dir": base / "b"})
    manager = SessionManager(configured, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config: [(provider, "scripted-model")]  # type: ignore[method-assign]
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    try:
        assert await app.extensions["browser"].wait_available("container")
        state = await manager.create_session("invoices")
        sid = state.session.id
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(sid, "Sign in to acme.test and read the invoices.")
        assert (await waiter)[0][2] == "completed"
        row = dict(await db.fetchone("SELECT * FROM browser_groups WHERE id = ?", (f"s-{sid}",)))
        waiter = asyncio.create_task(_wait_finished(manager))
        await app.extensions["browser_agent"].logins.submit(f"s-{sid}", login_of(row)["id"], username=USER, password=PASSWORD)
        assert (await waiter)[0][2] == "completed"
        assert len(provider.requests) == 6

        def said(request: Any) -> str:
            return " ".join(str(b.text) if hasattr(b, "text") else str(getattr(b, "content", "")) for m in request.messages for b in m.content_blocks)

        inputs = "\n".join(said(r) for r in provider.requests)
        assert "Sign-in to acme.test: submitted; now at https://acme.test/home; signed in: yes." in inputs
        nowhere(inputs)
        await manager.flush_background()
        nowhere(await every_stored_text(db))
        nowhere(caplog.text)
    finally:
        for task in tasks:
            task.cancel()
        await app.extensions["browser"].close()
        await manager.close()
