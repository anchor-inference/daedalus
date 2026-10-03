"""A dialog is shared like a service: local, a private link, or anyone with the link.

The page is the messages and the pictures. Tool calls, files, thinking and the host's own
wake-ups are not on it, and a path the host appended for an attachment is not either.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.dialog_share import DialogShares, public_messages, visible_text
from daedalus.extensions.services import SHARE_COOKIE_PREFIX
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectStore

SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz"
PATH = "/var/inbox/notes.txt"


def _view(role: str, text: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "role": role,
        "text": text,
        "origin": "operator" if role == "user" else "",
        "thinking": "",
        "tool_calls": [],
        "tool_results": [],
        "created_at": "2026-09-27T10:00:00+00:00",
        "seq": 1,
    }
    base.update(over)
    return base


def test_the_attachment_list_and_secrets_do_not_leave_with_the_message() -> None:
    plain = f"Please look.\n\nAttached files:\n- {PATH} (text/plain, 12 bytes)\n"
    assert visible_text(plain) == "Please look."
    kept = (
        "File attached.\n\n"
        "Attached files (kept by handle; Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])):\n"
        "- att:0123456789ab notes.txt (text/plain, 4 bytes)\n"
    )
    assert visible_text(kept) == "File attached."
    assert PATH not in visible_text(f"the token is {SECRET}")
    assert SECRET not in visible_text(f"the token is {SECRET}")


def test_the_page_is_the_dialog_and_not_the_work_around_it() -> None:
    picture = "pic-1"
    views = [
        _view("user", f"Hello\n\nAttached files:\n- {PATH} (text/plain, 4 bytes)", seq=1),
        _view("user", "[Loop iteration 3 — every hour]\n<loop_instruction>keep going</loop_instruction>", origin="loop", seq=2),
        _view("user", "wake the board", origin="schedule", seq=3),
        _view("user", "from outside", origin="inbound:telegram", seq=4),
        _view("assistant", "I will read it", tool_calls=[{"id": "c1", "name": "Read", "arguments": {"path": PATH}}], seq=5),
        _view("tool", "secret-output", tool_results=[{"id": "c1", "content": "secret-output"}], seq=6),
        _view("assistant", "the answer", thinking="private-thought", seq=7),
        _view(
            "assistant",
            f"Here it is.\n\n![Chart](daedalus-media:{picture})",
            seq=8,
            media=[{"id": picture, "layout": "single", "items": [{"id": "shot", "kind": "image", "url": "", "alt": SECRET, "width": 400, "height": 300}]}],
        ),
        _view("assistant", f"remote {SECRET}", seq=9, media=[{"id": "far", "layout": "single", "items": [{"id": "r1", "kind": "image", "url": "https://example.com/a.png", "alt": ""}]}]),
        _view("system", "the prompt", seq=10),
    ]
    page = public_messages(views, "slug")
    texts = [item["text"] for item in page]
    assert texts[0] == "Hello"
    assert any(item["via"] == "telegram" for item in page)
    assert "Here it is." in texts[-2]
    assert texts[-1].startswith("remote ")
    blob = "\n".join(texts)
    assert PATH not in blob and SECRET not in blob and "secret-output" not in blob
    assert "keep going" not in blob and "wake the board" not in blob and "I will read it" not in blob
    assert "private-thought" not in blob and "the prompt" not in blob
    own = next(item for item in page if "Here it is." in item["text"])
    assert own["media"][0]["items"][0]["url"] == "/c/slug/media/pic-1/shot"
    assert own["media"][0]["items"][0]["alt"] != SECRET
    remote = page[-1]["media"][0]["items"][0]
    assert remote["url"] == "https://example.com/a.png"
    assert "blob" not in remote


async def test_a_slug_is_minted_once_and_a_key_only_when_the_link_is_private(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    project = await ProjectStore(db).create("Bakery", [str(root)])
    await db.execute(
        "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, project_id) VALUES ('s1', 't', 'one', '2026-09-27T00:00:00+00:00', '2026-09-27T00:00:00+00:00', ?)",
        (project.id,),
    )
    app = SimpleNamespace(db=db, settings=SimpleNamespace(miniapp_public_url="https://example.com/app"), manager=None)
    shares = DialogShares(app)  # type: ignore[arg-type]
    public = await shares.share("s1", "public")
    assert public["mode"] == "public" and public["key"] is None
    assert public["url"] is not None and public["url"].startswith("https://example.com/c/")
    assert public["public_base"] == "https://example.com"
    slug = public["slug"]
    keyed = await shares.share("s1", "key")
    assert keyed["slug"] == slug and keyed["key"] and keyed["url"] == f"https://example.com/c/{slug}?key={keyed['key']}"
    rotated = await shares.share("s1", "key", rotate_key=True)
    assert rotated["slug"] == slug and rotated["key"] != keyed["key"]
    local = await shares.share("s1", "local")
    assert local["mode"] == "local" and local["url"] is None and local["slug"] == slug
    stored = await shares.by_slug(slug or "")
    assert stored is not None and stored["share_mode"] == "local"
    assert shares.allows(stored, None) is False
    await shares.share("s1", "public")
    stored = await shares.by_slug(slug or "")
    assert stored is not None and shares.allows(stored, "anything") is True
    with pytest.raises(KeyError):
        await shares.share("missing", "public")
    with pytest.raises(ValueError):
        await shares.share("s1", "world")


class _Manager:
    def __init__(self, views: list[dict[str, Any]], *, oldest: int, title: str) -> None:
        self.views = views
        self.oldest = oldest
        self.title = title
        self.sessions = self

    async def transcript_bounds(self, session_id: str) -> tuple[int, int]:
        return self.oldest, 99

    async def get_state(self, session_id: str) -> Any:
        return SimpleNamespace(session=SimpleNamespace(title=self.title))

    async def transcript_page(self, session_id: str, tail: int = 0, before: int = 0) -> list[dict[str, Any]]:
        return self.views


async def test_the_page_says_when_earlier_messages_were_left_off() -> None:
    manager = _Manager([_view("user", "later", seq=40)], oldest=1, title=f"Notes {SECRET}")
    shares = DialogShares(SimpleNamespace(db=None, settings=SimpleNamespace(miniapp_public_url=""), manager=manager))  # type: ignore[arg-type]
    page = await shares.page("s1", "slug")
    assert page["older"] is True
    assert SECRET not in page["title"]
    assert page["messages"][0]["text"] == "later"
    manager.oldest = 40
    assert (await shares.page("s1", "slug"))["older"] is False


class FakeDialogs:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows = {
            "public-dialog-slug": {"id": "s1", "share_mode": "public", "share_slug": "public-dialog-slug", "share_key": None},
            "keyed-dialog-slug": {"id": "s2", "share_mode": "key", "share_slug": "keyed-dialog-slug", "share_key": "s3cret-key"},
            "local-dialog-slug": {"id": "s3", "share_mode": "local", "share_slug": "local-dialog-slug", "share_key": None},
        }

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        return self.rows.get(slug)

    def allows(self, row: dict[str, Any], presented: str | None) -> bool:
        mode = row.get("share_mode") or "local"
        if mode == "public":
            return True
        key = row.get("share_key")
        if mode == "key" and key and presented:
            return secrets.compare_digest(str(key), str(presented))
        return False

    async def page(self, session_id: str, slug: str) -> dict[str, Any]:
        return {"title": "Hello", "older": False, "messages": [{"role": "user", "text": "hi", "at": "2026-01-01T00:00:00+00:00", "via": "", "media": []}]}

    async def media(self, session_id: str, presentation_id: str, item_id: str) -> tuple[str, str] | None:
        if presentation_id == "pic" and item_id == "one":
            return "text/plain", str(self.path)
        if presentation_id == "active" and item_id == "one":
            return "image/svg+xml", str(self.path)
        if presentation_id == "remote":
            return "image/png", "https://example.com/pic.png"
        return None


@pytest.fixture
def client(tmp_path: Path) -> Any:
    picture = tmp_path / "shot.txt"
    picture.write_text("pixels", encoding="utf-8")
    app = SimpleNamespace(
        settings=Settings(_env_file=None, telegram_bot_token="123:abc", owner_user_id=1),  # type: ignore[call-arg]
        config=RuntimeConfig(),
        manager=SimpleNamespace(list_sessions=lambda limit=100: _empty()),
        front=None,
        extensions={"dialogs": FakeDialogs(picture)},
    )
    api = build_app(app, "tok")  # type: ignore[arg-type]
    with TestClient(api) as test_client:
        yield test_client


async def _empty() -> list[Any]:
    return []


def test_a_public_link_opens_without_a_login_and_a_private_one_needs_its_key(client: TestClient) -> None:
    missing = client.get("/c/no-such-dialog", follow_redirects=False)
    assert missing.status_code == 404 and "not shared" in missing.text
    assert missing.headers["x-robots-tag"].startswith("noindex")
    russian = client.get("/c/no-such-dialog", headers={"Accept-Language": "ru"}, follow_redirects=False)
    assert russian.status_code == 404 and "не открыт" in russian.text
    assert client.get("/c/local-dialog-slug", follow_redirects=False).status_code == 404

    opened = client.get("/c/public-dialog-slug", follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/app/c/public-dialog-slug"
    assert opened.headers["cache-control"] == "no-store"

    page = client.get("/c/public-dialog-slug/transcript")
    assert page.status_code == 200 and page.json()["messages"][0]["text"] == "hi"
    assert page.headers["x-robots-tag"].startswith("noindex") and page.headers["cache-control"] == "no-store"
    shell = client.get("/app/c/public-dialog-slug")
    assert shell.headers["x-robots-tag"].startswith("noindex") and shell.headers["cache-control"] == "no-store"

    assert client.get("/c/keyed-dialog-slug", follow_redirects=False).status_code == 403
    assert client.get("/c/keyed-dialog-slug?key=wrong", follow_redirects=False).status_code == 403
    assert "needs its key" in client.get("/c/keyed-dialog-slug", follow_redirects=False).text
    assert client.get("/c/keyed-dialog-slug/transcript").status_code == 403
    assert client.get("/c/keyed-dialog-slug/media/pic/one").status_code == 403
    named = SHARE_COOKIE_PREFIX + "keyed-dialog-slug"
    assert client.get("/c/keyed-dialog-slug/transcript", cookies={named: "s3cret-key"}).status_code == 200
    assert client.get("/c/keyed-dialog-slug/transcript", headers={"X-Share-Key": "s3cret-key"}).status_code == 200
    granted = client.get("/c/keyed-dialog-slug?key=s3cret-key", follow_redirects=False)
    assert granted.status_code == 303 and granted.headers["location"] == "/app/c/keyed-dialog-slug"
    assert "key=" not in granted.headers["location"]
    cookie = granted.headers["set-cookie"]
    assert named in cookie and "Path=/c/keyed-dialog-slug" in cookie
    # The key moved into the cookie, so the next read of the same client no longer needs it in the address.
    assert client.get("/c/keyed-dialog-slug/transcript").status_code == 200

    robots = client.get("/c/public-dialog-slug/robots.txt")
    assert robots.status_code == 200 and "Disallow: /" in robots.text

    shot = client.get("/c/public-dialog-slug/media/pic/one")
    assert shot.status_code == 200 and shot.text == "pixels" and shot.headers["x-content-type-options"] == "nosniff"
    active = client.get("/c/public-dialog-slug/media/active/one")
    assert active.status_code == 200 and active.text == "pixels"
    policy = active.headers["content-security-policy"]
    assert "sandbox allow-scripts;" in policy and "allow-same-origin" not in policy
    assert "connect-src 'none'" in policy
    remote = client.get("/c/public-dialog-slug/media/remote/one", follow_redirects=False)
    assert remote.status_code == 302 and remote.headers["location"] == "https://example.com/pic.png"
    assert client.get("/c/public-dialog-slug/media/pic/missing").status_code == 404
