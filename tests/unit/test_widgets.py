"""Widgets: the tool that puts one into an answer, the values it keeps, the page it can become, and
what Telegram and a shared link say in its place."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.dialog_share import public_messages
from daedalus.host.pages import store_for
from daedalus.host.widgets import (
    HTML_MAX_BYTES,
    KEYS_MAX,
    VALUE_MAX_BYTES,
    WidgetError,
    WidgetStorage,
    network_notes,
    page_document,
    page_slug,
)
from daedalus.slugs import is_page_slug
from daedalus.stores.database import Database
from daedalus.tools import widgets as widget_tools
from daedalus.transport.telegram.render import RunRenderer, RunView

H = {"X-Daedalus-Token": "tok"}
CHECKLIST = "<ul id='l'></ul><script>widget.storage.get('done').then(function (d) { document.title = String(d); });</script>"


async def _session(db: Database, session_id: str = "s1") -> None:
    await db.execute("INSERT OR IGNORE INTO projects(id, name, created_at, settings) VALUES ('p', 'Work', '2026-10-14', '{}')")
    await db.execute(
        "INSERT INTO sessions(id, tenant_id, project_id, title, created_at, last_message_at) VALUES (?, 't', 'p', 'one', '2026-10-14', '2026-10-14')",
        (session_id,),
    )


# -- the tool -----------------------------------------------------------------------------------


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> Any:
    services = SimpleNamespace(max_tool_output_chars=60_000, extra={})
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)
    return SimpleNamespace(session_id="s1", metadata={"tool_call_id": "call-7"})


async def _call(context: Any, **arguments: Any) -> Any:
    return await widget_tools.widget().invoke(context, arguments)


async def test_the_tool_draws_a_widget_and_names_it_by_its_call(context: Any) -> None:
    result = await _call(context, title="  Loan   calculator ", html=CHECKLIST, data={"rate": 4.5})
    assert not result.is_error, result.content
    assert result.metadata == {"widget_id": "call-7", "title": "Loan calculator"}
    assert "Loan calculator" in result.content and "no network" not in result.content


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        ({"title": "", "html": CHECKLIST}, "title"),
        ({"title": "x" * 121, "html": CHECKLIST}, "title"),
        ({"title": "T", "html": "   "}, "HTML"),
        ({"title": "T", "html": "x" * (HTML_MAX_BYTES + 1)}, "PageCreate"),
        ({"title": "T", "html": CHECKLIST, "data": {"n": float("nan")}}, "JSON"),
        ({"title": "T", "html": CHECKLIST, "data": {"blob": "x" * 70_000}}, "65536"),
    ],
)
async def test_a_widget_that_cannot_be_drawn_is_refused_while_the_agent_can_fix_it(context: Any, arguments: dict[str, Any], words: str) -> None:
    result = await _call(context, **arguments)
    assert result.is_error and words in result.content


async def test_the_answer_says_what_wants_a_network_the_widget_does_not_have(context: Any) -> None:
    html = "<script src='https://cdn.example.com/chart.js'></script><script>fetch('/api/sessions')</script>"
    result = await _call(context, title="Chart", html=html)
    assert not result.is_error
    assert "no network" in result.content and "external <script src>" in result.content and "fetch" in result.content
    assert network_notes("<svg viewBox='0 0 10 10'></svg><img src='data:image/png;base64,AA'>") == []


# -- storage ------------------------------------------------------------------------------------


async def test_values_round_trip_per_widget_and_leave_with_the_session(db: Database) -> None:
    await _session(db)
    kept = WidgetStorage(db)
    await kept.set("s1", "call-7", "done", [1, 3])
    await kept.set("s1", "call-7", "note/a", {"text": "привет"})
    await kept.set("s1", "call-8", "done", [2])
    assert await kept.values("s1", "call-7") == {"done": [1, 3], "note/a": {"text": "привет"}}
    assert await kept.values("s1", "call-8") == {"done": [2]}
    assert await kept.delete("s1", "call-7", "done") and not await kept.delete("s1", "call-7", "done")
    with pytest.raises(WidgetError) as unknown:
        await kept.values("nope", "call-7")
    assert unknown.value.status == 404
    await db.execute("DELETE FROM sessions WHERE id = 's1'")
    assert await db.fetchall("SELECT * FROM widget_storage") == []


async def test_the_caps_refuse_a_write_and_keep_what_was_there(db: Database) -> None:
    await _session(db)
    kept = WidgetStorage(db)
    await kept.set("s1", "w", "k", "small")
    with pytest.raises(WidgetError) as big:
        await kept.set("s1", "w", "k", "x" * VALUE_MAX_BYTES)
    assert big.value.status == 413
    assert await kept.values("s1", "w") == {"k": "small"}
    for n in range(KEYS_MAX - 1):
        await kept.set("s1", "w", f"n{n}", n)
    with pytest.raises(WidgetError):
        await kept.set("s1", "w", "one-too-many", 1)
    # Rewriting a key that is there already is not a new key.
    await kept.set("s1", "w", "k", "again")
    for bad in ("", "x" * 201):
        with pytest.raises(WidgetError):
            await kept.set("s1", "w", bad, 1)
    with pytest.raises(WidgetError):
        await kept.set("s1", "../w", "k", 1)


async def test_all_of_a_widgets_values_are_capped_together(db: Database) -> None:
    await _session(db)
    kept = WidgetStorage(db)
    chunk = "x" * (VALUE_MAX_BYTES - 100)
    for n in range(4):
        await kept.set("s1", "w", f"k{n}", chunk)
    with pytest.raises(WidgetError) as full:
        await kept.set("s1", "w", "k4", chunk)
    assert full.value.status == 413 and "in all" in str(full.value)


# -- the API and the page a widget becomes ------------------------------------------------------


@pytest.fixture
async def site(settings: Settings, db: Database, tmp_path: Path) -> Any:
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(bus=None), front=None, extensions={}, guard=None)
    await _session(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        yield client, app


async def test_the_app_keeps_a_widgets_values_over_the_api(site: Any) -> None:
    client, _ = site
    base = "/api/sessions/s1/widgets/call-7/storage"
    assert (await client.get(base)).status_code == 401
    assert (await client.get(base, headers=H)).json() == {"values": {}}
    assert (await client.put(f"{base}/done", json={"value": [1, 2]}, headers=H)).status_code == 200
    assert (await client.put(f"{base}/notes/today", json={"value": "milk"}, headers=H)).status_code == 200
    assert (await client.get(base, headers=H)).json() == {"values": {"done": [1, 2], "notes/today": "milk"}}
    assert (await client.delete(f"{base}/done", headers=H)).status_code == 200
    assert (await client.delete(f"{base}/done", headers=H)).status_code == 404
    assert (await client.put(f"{base}/big", json={"value": "x" * VALUE_MAX_BYTES}, headers=H)).status_code == 413
    assert (await client.get("/api/sessions/nope/widgets/call-7/storage", headers=H)).status_code == 404


async def test_open_as_page_publishes_the_widget_with_its_values_and_its_agent(site: Any) -> None:
    client, app = site
    await client.put("/api/sessions/s1/widgets/call-7/storage/done", json={"value": [3]}, headers=H)
    body = {"title": "Packing list", "html": CHECKLIST, "data": {"items": ["tent", "</script><b>"]}}
    made = (await client.post("/api/sessions/s1/widgets/call-7/page", json=body, headers=H)).json()
    assert made == {"slug": "packing-list", "title": "Packing list", "path": "/app/p/packing-list", "version": 1}
    again = (await client.post("/api/sessions/s1/widgets/call-7/page", json=body, headers=H)).json()
    assert again["slug"] == "packing-list-2"
    pages = store_for(app)
    manifest = await pages.require("packing-list")
    assert manifest["session_id"] == "s1" and manifest["version"] == 1
    assert (await pages.storage_get("packing-list", "done"))["value"] == [3]  # type: ignore[index]
    served = pages.served(manifest)
    assert served is not None
    text = (served.root / "index.html").read_text("utf-8")
    assert "/pages/_kit/daedalus.js" in text and "window.widget" in text and CHECKLIST in text
    assert "</script><b>" not in text, "the data cannot close the shim's script"
    assert text.index("window.widget") < text.index(CHECKLIST), "widget is defined before the widget's own scripts run"
    refused = await client.post("/api/sessions/s1/widgets/call-7/page", json={**body, "html": "x" * (HTML_MAX_BYTES + 1)}, headers=H)
    assert refused.status_code == 413


def test_the_shim_goes_first_in_the_widgets_own_head() -> None:
    full = page_document("T", "<!doctype html><html lang='en'><head><title>x</title></head><body>hi</body></html>", "null")
    assert full.index("<head>") < full.index("window.widget") < full.index("<title>x</title>")
    bare = page_document("T", "<html><body>hi</body></html>", "null")
    assert bare.index("<head>") < bare.index("window.widget") < bare.index("<body>")
    fragment = page_document("A <b>", "<p>hi</p>", "{}")
    assert fragment.startswith("<!doctype html>") and "<title>A &lt;b&gt;</title>" in fragment and "<p>hi</p>" in fragment


def test_a_page_slug_comes_from_the_title_and_steps_past_the_taken_ones() -> None:
    assert page_slug("Loan calculator", set()) == "loan-calculator"
    # Letters of any script are kept: a Russian title used to become "widget", "widget-2"…
    assert page_slug("Калькулятор ипотеки", set()) == "калькулятор-ипотеки"
    assert page_slug("Калькулятор", {"калькулятор", "калькулятор-2"}) == "калькулятор-3"
    # Decomposed input (a name typed on macOS) gives the same slug as composed.
    assert page_slug(unicodedata.normalize("NFD", "Мой отчёт"), set()) == "мой-отчёт"
    assert page_slug("../etc/passwd", set()) == "etc-passwd"
    assert page_slug("!!!", set()) == "widget"
    for title in ("Калькулятор ипотеки", "Ünïcödé — ok", "नमस्ते दुनिया", "İstanbul"):
        assert is_page_slug(page_slug(title, set())), title


def test_the_page_store_and_the_navigation_take_a_slug_in_any_script(tmp_path: Path) -> None:
    from daedalus.config import NavigationConfig
    from daedalus.host.pages import PageError, check_slug

    assert check_slug("калькулятор") == "калькулятор"
    for bad in ("Калькулятор", "_kit", "a/b", "a.b", "a b", "", "x" * 64, unicodedata.normalize("NFD", "отчёт")):
        with pytest.raises(PageError):
            check_slug(bad)
    assert NavigationConfig(rail=["page:калькулятор", "inbox"]).rail == ["page:калькулятор", "inbox"]
    with pytest.raises(ValueError):
        NavigationConfig(rail=["page:a/b"])


# -- where a widget cannot run ------------------------------------------------------------------


class Outbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.sent.append(text)
        return len(self.sent)

    async def send_html(self, html: str) -> int:
        self.sent.append(html)
        return len(self.sent)

    async def edit_text(self, message_id: int, text: str, *, html: bool = False) -> None:
        return None


def _event(kind: EventType, **payload: Any) -> TurnEvent:
    return TurnEvent(type=kind, run_id="r1", payload=payload)


async def test_telegram_names_each_drawn_widget_with_a_link_into_the_app(tmp_path: Path) -> None:
    outbox = Outbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m", session_id="s1"), edit_interval=0.0)  # type: ignore[arg-type]
    await renderer.handle(_event(EventType.MESSAGE_START))
    for call, title, failed in (("c1", "Too big", True), ("c2", "Loan calculator", False)):
        await renderer.handle(_event(EventType.TOOL_USE_START, tool_call_id=call, tool_name="Widget"))
        await renderer.handle(_event(EventType.TOOL_USE_STOP, tool_call_id=call, final_input={"title": title, "html": "<p>x</p>"}))
        await renderer.handle(_event(EventType.TOOL_RESULT, tool_call_id=call, content="", is_error=failed))
    await renderer.handle(_event(EventType.MESSAGE_STOP, stop_reason="tool_use"))
    await renderer.handle(_event(EventType.MESSAGE_START))
    await renderer.handle(_event(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "Move the sliders."}))
    await renderer.handle(_event(EventType.MESSAGE_STOP, stop_reason="end_turn"))
    await renderer.finish("completed", workspace=tmp_path)
    assert outbox.sent[0] == "Move the sliders."
    line = next(text for text in outbox.sent if "🧩" in text)
    assert "Loan calculator" in line and "(/app/agents/s1)" in line and "Too big" not in line


def test_a_shared_link_shows_a_widgets_title_and_never_its_document() -> None:
    def view(role: str, text: str = "", **extra: Any) -> dict[str, Any]:
        return {"role": role, "text": text, "created_at": "2026-10-14T00:00:00Z", "tool_calls": [], "tool_results": [], "media": [], **extra}

    calls = [{"id": "w1", "name": "Widget", "arguments": {"title": "Loan calculator", "html": "<p>secret-markup</p>"}},
             {"id": "w2", "name": "Widget", "arguments": {"title": "Refused one", "html": "x"}}]
    views = [
        view("user", "Compare the loans", origin="operator"),
        view("assistant", "Here is a calculator", tool_calls=calls),
        view("tool", tool_results=[{"id": "w1", "content": "ok", "is_error": False}, {"id": "w2", "content": "no", "is_error": True}]),
        view("assistant", "Move the sliders."),
    ]
    page = public_messages(views, "slug")
    assert [item["widgets"] for item in page] == [[], ["Loan calculator"], []]
    assert page[1]["text"] == "", "the note beside the call stays folded, as for any other call"
    assert "secret-markup" not in repr(page)
