"""The size of the session listing's first page.

The sidebar and the phone's drawer draw what the first page holds. At thirty rows most titles sat
behind "More"; the page is now two hundred, and the cursor still walks on past it.
"""

from __future__ import annotations

from typing import Any

from daedalus.extensions.api import SESSION_PAGE
from tests.unit.test_project_commands import running  # noqa: F401 — the fixture is used by name
from tests.unit.test_project_folders_api import HEADERS


async def test_the_first_page_holds_every_chat_up_to_the_page(running: Any) -> None:  # noqa: F811
    manager, client = running
    for n in range(40):
        await manager.create_session(f"chat {n}")
    listing = (await client.get("/api/sessions", headers=HEADERS)).json()
    assert SESSION_PAGE == 200
    assert len(listing["sessions"]) >= 40
    assert listing["next_cursor"] is None


async def test_a_smaller_page_still_pages_by_cursor(running: Any) -> None:  # noqa: F811
    manager, client = running
    for n in range(5):
        await manager.create_session(f"chat {n}")
    first = (await client.get("/api/sessions", headers=HEADERS, params={"limit": 2})).json()
    assert len(first["sessions"]) == 2 and first["next_cursor"]
    rest = (await client.get("/api/sessions", headers=HEADERS, params={"limit": 500, "cursor": first["next_cursor"]})).json()
    assert {s["id"] for s in rest["sessions"]}.isdisjoint({s["id"] for s in first["sessions"]})
    assert rest["next_cursor"] is None
    assert (await client.get("/api/sessions", headers=HEADERS, params={"limit": 501})).status_code == 422
