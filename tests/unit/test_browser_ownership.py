"""A saved browser row does not prove the old instance is attachable."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.browser.model import EnvUnavailable
from daedalus.extensions.browser_state import browser_ownership, safe_display_url


class Browser:
    def __init__(self, groups: list[dict] | None, *, unavailable: bool = False) -> None:
        self.groups = groups
        self.unavailable = unavailable
        self.links = {"container": SimpleNamespace(client=SimpleNamespace(instance="instance-one"))}

    async def get_row(self, group_id: str) -> dict:
        return {"id": group_id, "env": "container", "status": "open", "closed_at": None,
                "close_reason": "", "daemon_instance": "instance-one", "browser_id": "browser-one",
                "owner_kind": "session", "owner_id": "session-one", "project_id": "project-one",
                "session_id": "session-one", "staff_id": None, "url": "https://example.org/work?token=private"}

    async def _call(self, env: str, method: str, params: dict, *, what: str) -> dict:
        if self.unavailable:
            raise EnvUnavailable("browser daemon is down", env=env, reason="unreachable")
        return {"groups": self.groups}


@pytest.mark.asyncio
async def test_reconnect_requires_exact_live_instance() -> None:
    matching = await browser_ownership(Browser([{"id": "group-one", "browser_id": "browser-one"}]), "group-one")
    assert matching["alive"] == "yes" and matching["reconnectable"]
    missing = await browser_ownership(Browser([]), "group-one")
    assert missing["alive"] == "no" and not missing["reconnectable"]
    replaced = await browser_ownership(Browser([{"id": "group-one", "browser_id": "browser-two"}]), "group-one")
    assert replaced["alive"] == "unknown" and not replaced["reconnectable"]
    changed_daemon = Browser([{"id": "group-one", "browser_id": "browser-one"}])
    changed_daemon.links["container"].client.instance = "instance-two"
    assert (await browser_ownership(changed_daemon, "group-one"))["alive"] == "unknown"
    unavailable = await browser_ownership(Browser(None, unavailable=True), "group-one")
    assert unavailable["alive"] == "unknown" and not unavailable["reconnectable"]


def test_saved_url_is_redacted_for_display() -> None:
    assert safe_display_url("https://example.org/work?token=private#view") == "https://example.org/work"
    assert safe_display_url("https://user:password@example.org/") is None
    assert safe_display_url("javascript:alert(1)") is None
