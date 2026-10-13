"""BrowserCookies end to end against the in-process daemon: a cookie file the operator attached goes
into the agent's profile, the listing and the clearing name sites and names, the attachment line says
a cookie file is one, and no value reaches the result, the audit or the app's action log."""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import json
import time
from pathlib import Path

from daedalus.browser.service import _action_row
from daedalus.host import prompts
from daedalus.host.policy import ALLOW, DENY, Policy
from daedalus.host.session_runner import Attachment
from daedalus.tools.browser import BROWSER_TOOLS, READ_ONLY_TOOLS
from tests.unit.test_browser_tools import Rig, base, daemon, rig  # noqa: F401

LATER = int(time.time()) + 86_400 * 30
VALUES = ("FAKE-VALUE-token-1", "FAKE-VALUE-refresh-2", "FAKE-VALUE-session-3", "FAKE-VALUE-header-4")
COOKIE_FILE = (
    "# Netscape HTTP Cookie File\n"
    f".wolt.com\tTRUE\t/\tTRUE\t{LATER}\t__wtoken\t{VALUES[0]}\n"
    f"#HttpOnly_.wolt.com\tTRUE\t/\tTRUE\t{LATER}\t__wrtoken\t{VALUES[1]}\n"
    f"auth.wolt.com\tFALSE\t/\tFALSE\t0\tlang\t{VALUES[2]}\n"
    f".wolt.com\tTRUE\t/\tFALSE\t{LATER}\trefuse-me\tFAKE-VALUE-refused\n"
    "a broken line\n"
)


def no_value(text: str) -> None:
    for value in (*VALUES, "FAKE-VALUE"):
        assert value not in text, f"a cookie value reached the agent: {text!r}"


def inbox_file(rig: Rig, sid: str, name: str, body: str) -> Path:
    inbox = rig.manager.workspace_for(sid) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(body)
    return inbox / name


async def test_the_tool_is_registered_as_a_change_and_taught() -> None:
    assert "BrowserCookies" in BROWSER_TOOLS and "BrowserCookies" not in READ_ONLY_TOOLS
    assert "BrowserCookies(path=" in prompts.group_instructions("browser", selfdev_mode="off")


async def test_an_attached_cookie_file_signs_the_browser_in_and_no_value_comes_back(rig: Rig) -> None:
    sid = await rig.session()
    path = inbox_file(rig, sid, "wolt.com_cookies.txt", COOKIE_FILE)
    # No browser open yet: the import starts the profile's browser, as BrowserOpen would.
    text, failed = await rig.call(sid, "BrowserCookies", path=str(path))
    assert not failed, text
    no_value(text)
    assert "Loaded 3 of 4 cookies from wolt.com_cookies.txt into your browser (the project's profile, kept between sessions): wolt.com 3, auth.wolt.com 1." in text
    assert "The browser refused 1: refuse-me on .wolt.com: the browser refused it: Invalid cookie fields" in text
    assert "Skipped 1: line 6: not the seven tab-separated fields" in text
    assert "1 is a session cookie: it lasts until the browser closes." in text and "BrowserNavigate" in text
    # The daemon got the values; it is the one place they go.
    (sent,) = [p for m, p in rig.daemon.calls if m == "cookies.set"]
    assert {c["value"] for c in sent["cookies"]} >= set(VALUES[:3]) and sent["origin"]["actor"] == "agent"
    row = await rig.app.extensions["browser"].get_row(f"s-{sid}")
    assert list(rig.daemon.cookies) == [row["profile"]] and row["profile"].startswith("project-")

    text, failed = await rig.call(sid, "BrowserCookies", action="list")
    assert not failed
    no_value(text)
    assert "Cookies in your browser (3; names and expiry only, never values):" in text
    assert "- .wolt.com (2): __wtoken (" in text and "__wrtoken (" in text and ", httpOnly, secure)" in text
    assert "- auth.wolt.com (1): lang (session)" in text
    text, _ = await rig.call(sid, "BrowserCookies", action="list", domain="auth.wolt.com")
    assert "lang" in text and "__wtoken" not in text

    text, failed = await rig.call(sid, "BrowserCookies", action="clear")
    assert failed and "clear needs domain" in text
    text, failed = await rig.call(sid, "BrowserCookies", action="clear", domain="auth.wolt.com")
    assert not failed and text == "Cleared 1 cookie of auth.wolt.com from your browser."
    text, _ = await rig.call(sid, "BrowserCookies", action="list", domain="auth.wolt.com")
    assert text == "Your browser holds no cookies for auth.wolt.com."

    # The operator reads in the action log what changed the browser's identity, by site, never a value.
    service = rig.app.extensions["browser"]
    audit = [e for e in await service.audit_log(f"s-{sid}") if e["action"] == "cookies"]
    assert [e["detail"]["action"] for e in audit] == ["clear", "import"]
    assert audit[1]["detail"]["element"] == "auth.wolt.com, wolt.com" and audit[1]["detail"]["count"] == 3
    no_value(json.dumps(audit))
    rows = await rig.manager.db.fetchall("SELECT * FROM browser_audit WHERE action = 'cookies' ORDER BY seq")
    assert [_action_row(dict(r))["kind"] for r in rows] == ["cookies_import", "cookies_clear"]


async def test_a_cookie_header_a_narrowed_file_and_the_refusals(rig: Rig) -> None:
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen")
    text, failed = await rig.call(sid, "BrowserCookies", text=f"Cookie: sid={VALUES[3]}; theme=dark", domain="shop.test")
    assert not failed and "Loaded 2 of 2 cookies from the text given" in text and "shop.test 2" in text
    no_value(text)
    text, failed = await rig.call(sid, "BrowserCookies", text=f"sid={VALUES[3]}")
    assert failed and "give domain" in text
    no_value(text)
    path = inbox_file(rig, sid, "cookies.txt", COOKIE_FILE)
    text, failed = await rig.call(sid, "BrowserCookies", path=str(path), domain="auth.wolt.com")
    assert not failed and "Loaded 1 of 1 cookie" in text and "auth.wolt.com 1" in text
    for bad, said in (
        (dict(), "give path"),
        (dict(path=str(path), text="a=b"), "one of them"),
        (dict(action="eat"), "action is one of"),
        (dict(path=str(inbox_file(rig, sid, "notes.txt", "just some notes\nabout the trip\n"))), "not a cookies.txt"),
        (dict(path="/etc/passwd"), "outside"),
    ):
        text, failed = await rig.call(sid, "BrowserCookies", **bad)
        assert failed and said in text, (bad, text)


async def test_the_attachment_line_says_a_cookie_file_is_one(rig: Rig) -> None:
    sid = await rig.session()
    state = rig.manager.live_state(sid)
    assert state is not None
    staged = rig.manager.workspace_for(sid).parent / "staged"
    staged.mkdir(exist_ok=True)
    (staged / "wolt.com_cookies.txt").write_text(COOKIE_FILE)
    (staged / "notes.txt").write_text("name=value; but this is just a note\n")
    body, _ = await rig.manager._ingest_attachments(state, "sign in please", [
        Attachment(path=staged / "wolt.com_cookies.txt", name="wolt.com_cookies.txt", mime_type="text/plain"),
        Attachment(path=staged / "notes.txt", name="notes.txt", mime_type="text/plain"),
    ])
    lines = body.split("\n")
    cookie_line = next(line for line in lines if "wolt.com_cookies.txt" in line)
    assert cookie_line.endswith("bytes) — cookies for wolt.com, auth.wolt.com; BrowserCookies can load them into the browser"), cookie_line
    notes_line = next(line for line in lines if "notes.txt" in line)
    assert notes_line.endswith("bytes)"), notes_line
    no_value(body)
    # Without a browser on the installation the line says nothing of a tool there is not.
    hook = rig.manager.service_hooks.pop("browser")
    try:
        body, _ = await rig.manager._ingest_attachments(state, "", [Attachment(path=staged / "wolt.com_cookies.txt", name="wolt.com_cookies.txt", mime_type="text/plain")])
        assert "BrowserCookies" not in body
    finally:
        rig.manager.service_hooks["browser"] = hook


def test_the_policy_lets_an_inbox_file_through_and_keeps_the_walls(tmp_path: Path) -> None:
    """Loading cookies the operator handed over needs no question of its own: the file is theirs, the
    profile is the agent's, and the action log says what was loaded. The paths it may read are every
    file tool's: an installation's own files stay sealed."""
    install = tmp_path / "Daedalus"
    workspace = install / "workspaces" / "s1"
    secrets = install / "daedalus-secrets"
    (workspace / "inbox").mkdir(parents=True)
    secrets.mkdir(parents=True)
    policy = Policy(native=True, home_dir=str(tmp_path), sealed_paths=[secrets], protected_paths=[secrets], workspace_roots=[install / "workspaces"], base_dir=workspace)
    assert policy.evaluate("BrowserCookies", {"action": "import", "path": f"{workspace}/inbox/wolt.com_cookies.txt"}).action == ALLOW
    assert policy.evaluate("BrowserCookies", {"action": "import", "path": f"{secrets}/keyproxy.env"}).action == DENY
