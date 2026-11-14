"""An upload in the agent's own browser, end to end through a run: the model's BrowserAct, the host's
sensitive-action gate and its one-time grant, the daemon (the fake, which answers as the real one
does; the real one is proved on a real Chromium in ``browserd/internal/rpc/upload_test.go``).

What these replay: a form whose file input is hidden behind a button of the page's own. The upload to
the hidden input was asked about, allowed, and then refused as having no size; the grant was spent, so
the retry on the button asked again, was allowed again and refused as "not a file input"; the input
made visible by hand was allowed a third time and answered "Done" with no file in the form. Four Allows,
no files.
"""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import BrowserConfig, Settings
from daedalus.extensions import browser as browser_extension
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.fake_browserd import Element, FakeBrowserd
from tests.support.models import model_config
from tests.unit.test_guest_turns import ScriptedProvider, provider_endpoint, settled

pytestmark = pytest.mark.usefixtures("bare_sandbox")

FORM = "https://forms.test/appeal"
FILES = ["card.png", "receipt.pdf"]


def form(daemon: FakeBrowserd) -> None:
    daemon.page(FORM, title="Appeal", heading="Appeal", elements={
        "e1": Element("e1", "button", "Attach", file_input="e2"),
        "e2": Element("e2", "button", "Choose File", tag="input", type="file", box={"x": 0.0, "y": -452.0, "w": 0.0, "h": 0.0}),
        "e3": Element("e3", "button", "Add a picture", opens_chooser=True),
        "e4": Element("e4", "button", "Sink", tag="input", type="file", swallows=True),
        "e5": Element("e5", "button", "Save draft"),
        "e6": Element("e6", "button", "Hidden button", box={"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}),
    })


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="bup-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(base: Path) -> AsyncIterator[FakeBrowserd]:
    fake = await FakeBrowserd(base / "b").start()
    form(fake)
    yield fake
    await fake.stop()


class Rig:
    def __init__(self, manager: SessionManager, daemon: FakeBrowserd) -> None:
        self.manager = manager
        self.daemon = daemon

    async def session(self) -> str:
        state = await self.manager.create_session("appeal", own_directory=True)
        live = await self.manager.get_state(state.session.id)
        assert live is not None
        workspace = Path(live.workspace)
        for name in FILES:
            (workspace / name).write_bytes(b"bytes of " + name.encode())
        # The browser tools come as a group the first call loads; a run of one call loads them.
        (loading,) = await self.run(state.session.id, {"url": FORM})
        assert "loaded now" in loading, loading
        return state.session.id

    async def run(self, sid: str, *calls: dict[str, Any]) -> list[str]:
        """One turn in which the model makes these calls in order; what each call answered."""
        provider = ScriptedProvider([*({"tool": "BrowserAct" if "action" in c or "steps" in c else "BrowserOpen", "args": c} for c in calls), {"text": "done"}])
        provider_endpoint(self.manager, provider)
        state = self.manager.live_state(sid)
        await self.manager.submit(sid, "attach the files to the appeal")
        await settled(self.manager, state)
        await self.manager.flush_background()
        out = []
        for request in provider.requests[1:]:
            block = request.messages[-1].content_blocks[0]
            out.append(str(getattr(block, "content", "")))
        return out[: len(calls)]

    def grants(self, sid: str) -> set[str]:
        state = self.manager.live_state(sid)
        return set((state.metadata.get("policy_grants") or {}) if state is not None else {})

    async def cards(self) -> int:
        await self.manager.flush_background()
        rows = await self.manager.db.fetchall("SELECT seq FROM app_events WHERE type = 'permission.pending'")
        return len(rows)


@pytest.fixture
async def rig(settings: Settings, db: Database, base: Path, daemon: FakeBrowserd) -> AsyncIterator[Rig]:
    configured = settings.model_copy(update={"browser_container_dir": base / "b"})
    runtime = model_config().model_copy(update={"browser": BrowserConfig(control_wait_seconds=0.2)})
    manager = SessionManager(configured, runtime, db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None)
    tasks = await browser_extension.install(app)  # type: ignore[arg-type]
    assert await app.extensions["browser"].wait_available("container")
    yield Rig(manager, daemon)
    for task in tasks:
        task.cancel()
    await app.extensions["browser"].close()
    await manager.close()


def upload(ref: str, element: str) -> dict[str, Any]:
    return {"action": "upload", "ref": ref, "element": element, "paths": FILES}


def key_of(text: str) -> str:
    assert "Approval key: " in text, text
    return text.split("Approval key: ")[1].split(".")[0]


async def test_one_allow_carries_an_upload_through_a_failed_try_to_a_hidden_input(rig: Rig) -> None:
    sid = await rig.session()
    opened, asked = await rig.run(sid, {"url": FORM}, upload("e2", "the file field"))
    assert "Browser opened" in opened, opened
    key = key_of(asked)
    assert await rig.cards() == 1 and not rig.daemon.uploaded_to
    await rig.manager.grant(sid, key, via="app")

    # The retry names a button that opens nothing: refused as not done, and the Allow is not spent.
    (missed,) = await rig.run(sid, upload("e5", "the Save draft button"))
    assert "opened no file chooser" in missed and "approval for it still stands" in missed, missed
    assert key in rig.grants(sid), "the grant was spent on a call that did nothing"

    # The same files to the hidden input, by the page's own button: no new card, and they arrive.
    (done,) = await rig.run(sid, upload("e1", "the Attach button"))
    assert done.startswith("Done: upload"), done
    assert "Attached 2 files to the file input beside the element named" in done and "card.png, receipt.pdf" in done, done
    assert rig.daemon.uploaded_to == [("e2", FILES)]
    assert await rig.cards() == 1, "one Allow, one card"
    assert key not in rig.grants(sid), "spent once the files went"

    # Spent: the same upload again is a question again.
    (again,) = await rig.run(sid, upload("e2", "the file field"))
    assert key_of(again) == key and rig.daemon.uploaded_to == [("e2", FILES)]


async def test_the_question_is_the_same_whichever_element_takes_the_files(rig: Rig) -> None:
    sid = await rig.session()
    _, by_input, by_button, by_chooser = await rig.run(sid, {"url": FORM}, upload("e2", "the file field"), upload("e1", "the Attach button"), upload("e3", "Add a picture"))
    assert key_of(by_input) == key_of(by_button) == key_of(by_chooser)
    assert await rig.cards() == 1


async def test_a_button_that_opens_the_chooser_takes_the_files(rig: Rig) -> None:
    sid = await rig.session()
    _, asked = await rig.run(sid, {"url": FORM}, upload("e3", "Add a picture"))
    await rig.manager.grant(sid, key_of(asked), via="app")
    (done,) = await rig.run(sid, upload("e3", "Add a picture"))
    assert done.startswith("Done: upload") and "the file chooser opened for" in done, done
    assert rig.daemon.uploaded_to == [("e3", FILES)]


async def test_files_that_do_not_attach_are_an_error_never_done_and_the_allow_stands(rig: Rig) -> None:
    sid = await rig.session()
    _, asked = await rig.run(sid, {"url": FORM}, upload("e4", "the Sink field"))
    key = key_of(asked)
    await rig.manager.grant(sid, key, via="app")
    (lost,) = await rig.run(sid, upload("e4", "the Sink field"))
    assert "did not attach" in lost and not lost.startswith("Done") and "Done:" not in lost, lost
    assert key in rig.grants(sid) and not rig.daemon.uploaded_to


async def test_a_step_that_went_through_keeps_its_grant_spent_though_a_later_step_failed(rig: Rig) -> None:
    sid = await rig.session()
    _, asked = await rig.run(sid, {"url": FORM}, upload("e1", "the Attach button"))
    key = key_of(asked)
    await rig.manager.grant(sid, key, via="app")
    (both,) = await rig.run(sid, {"steps": [upload("e1", "the Attach button"), {"action": "click", "ref": "e6", "element": "the hidden button"}]})
    assert "no size" in both and rig.daemon.uploaded_to == [("e2", FILES)], both
    assert key not in rig.grants(sid), "the files went: the Allow is used, whatever the later step did"


async def test_an_action_refused_before_it_reached_the_page_does_not_spend_its_grant(rig: Rig) -> None:
    """The size refusal itself, for any action: a click the page has made unclickable after the Allow."""
    sid = await rig.session()
    rig.daemon.pages[FORM].elements["e5"] = Element("e5", "button", "Delete everything")
    _, asked = await rig.run(sid, {"url": FORM}, {"action": "click", "ref": "e5", "element": "the Delete button"})
    key = key_of(asked)
    await rig.manager.grant(sid, key, via="app")
    tab = rig.daemon.tab_of(f"s-{sid}")
    tab.page.elements["e5"].box = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    (refused,) = await rig.run(sid, {"action": "click", "ref": "e5", "element": "the Delete button"})
    assert "no size" in refused, refused
    assert key in rig.grants(sid)
