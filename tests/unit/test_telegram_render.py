from __future__ import annotations

from pathlib import Path

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.transport.telegram.markdown import markdown_to_html, split_message
from daedalus.transport.telegram.render import RunRenderer, RunView, flatten_evidence
from tests.support.waiting import grows_to


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.edits: list[tuple[int, str]] = []
        self.documents: list[Path] = []
        self.media: list[tuple[str, object, str | None]] = []

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.sent.append((text, markdown))
        return len(self.sent)

    async def send_html(self, html: str) -> int:
        self.sent.append((html, False))
        return len(self.sent)

    async def edit_text(self, message_id: int, text: str, *, html: bool = False) -> None:
        self.edits.append((message_id, text))

    async def send_document(self, path: Path, caption: str | None = None) -> int:
        self.documents.append(path)
        return 99

    async def send_photo(self, path: Path, caption: str | None = None) -> int:
        self.media.append(("photo", path, caption))
        return 100

    async def send_video(self, path: Path, caption: str | None = None) -> int:
        self.media.append(("video", path, caption))
        return 101

    async def send_audio(self, path: Path, caption: str | None = None) -> int:
        self.media.append(("audio", path, caption))
        return 102

    async def send_animation(self, path: Path, caption: str | None = None) -> int:
        self.media.append(("animation", path, caption))
        return 103

    async def send_album(self, paths: list[Path], caption: str | None = None) -> list[int]:
        self.media.append(("album", paths, caption))
        return list(range(104, 104 + len(paths)))

    async def delete(self, message_id: int) -> None:
        return None

    drafts_ok = True
    drafts: list[tuple[int, str]] = []

    async def send_draft(self, draft_id: int, text: str) -> bool:
        self.drafts.append((draft_id, text))
        return self.drafts_ok


def _evt(t: EventType, **payload):  # type: ignore[no-untyped-def]
    return TurnEvent(type=t, run_id="r1", payload=payload)


async def test_renderer_sends_final_answer_and_freezes_status(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.TOOL_USE_START, tool_call_id="c1", tool_name="Exec"))
    await renderer.handle(_evt(EventType.TOOL_USE_STOP, tool_call_id="c1", final_input={"command": "ls"}))
    await renderer.handle(_evt(EventType.TOOL_RESULT, tool_call_id="c1", content="a\nb"))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="tool_use", tokens_used={"total": 10}))
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "All **done**"}))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="end_turn", tokens_used={"total": 20}))
    await renderer.flush()
    await renderer.finish("completed", workspace=tmp_path)
    finals = [t for t, md in outbox.sent if md]
    assert finals == ["All **done**"]
    status_texts = [t for t, md in outbox.sent if not md] + [t for _, t in outbox.edits]
    assert any("Exec ls" in t and "<details" in t for t in status_texts)
    assert "✅ completed" in outbox.edits[-1][1]


async def test_long_answer_becomes_a_document(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "x" * 20_000}))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="end_turn"))
    await renderer.finish("completed", workspace=tmp_path)
    assert outbox.documents and outbox.documents[0].read_text() == "x" * 20_000


def test_split_keeps_code_fences_balanced() -> None:
    text = "intro\n```python\n" + "\n".join(f"line {i}" for i in range(800)) + "\n```\noutro"
    chunks = split_message(text, limit=2000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
    assert "".join(c.replace("```python\n", "").replace("```\n", "") for c in chunks).count("line 799") == 1


def test_markdown_to_html_basics() -> None:
    html = markdown_to_html("# Title\n**bold** and `code` and [x](https://e.com)\n```sh\necho <hi>\n```")
    assert "<b>Title</b>" in html and "<b>bold</b>" in html and "<code>code</code>" in html
    assert '<a href="https://e.com">x</a>' in html
    assert '<pre><code class="language-sh">echo &lt;hi&gt;</code></pre>' in html


async def test_streaming_sends_drafts_then_the_final_message(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    outbox.drafts = []
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0, streaming=True, draft_interval=0.01)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "Hello, "}))
    await grows_to(outbox.drafts, 1, "the first draft went out")
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "world"}))
    await grows_to(outbox.drafts, 2, "the second draft went out")
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="end_turn"))
    await renderer.finish("completed", workspace=tmp_path)
    assert [t for _, t in outbox.drafts] == ["Hello,", "Hello, world", ""]  # the empty draft clears the preview
    assert len({d for d, _ in outbox.drafts}) == 1  # one draft id per answer keeps the animation
    assert [t for t, md in outbox.sent if md] == ["Hello, world"]


async def test_streaming_disables_itself_when_drafts_are_refused(tmp_path: Path) -> None:
    import asyncio

    outbox = FakeOutbox()
    outbox.drafts = []
    outbox.drafts_ok = False
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0, streaming=True, draft_interval=0.01)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "x"}))
    await grows_to(outbox.drafts, 1, "the refused draft was attempted")
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "y"}))
    # Nothing to wait for after a refusal: what is asserted is that no second draft is ever tried.
    await asyncio.sleep(0.05)
    assert len(outbox.drafts) == 1 and renderer.streaming is False


def test_evidence_tags_become_readable_text() -> None:
    """Telegram has nothing to click, so a file tag degrades to path:lines and a run tag to its label."""
    assert flatten_evidence('fixed in <file path="daedalus/tools/web.py" lines="20-40"/>.') == "fixed in daedalus/tools/web.py:20-40."
    assert flatten_evidence('see <file path="notes.md" />') == "see notes.md"
    assert flatten_evidence('<run id="v12" label="14 passed"/> now') == "14 passed (run v12) now"
    assert flatten_evidence('<run id="job-3-1"/>') == "run job-3-1"
    assert flatten_evidence("a <b>bold</b> tag and <file/> stay out of it") == "a <b>bold</b> tag and <file/> stay out of it"
    assert flatten_evidence("before\n\n![screen](daedalus-media:11111111-1111-1111-1111-111111111111)\n\nafter") == "before\n\nafter"


async def test_the_final_answer_carries_no_raw_evidence_tags(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": 'done: <file path="a/b.py" lines="1-3"/>'}))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="end_turn"))
    await renderer.finish("completed", workspace=tmp_path)
    assert [t for t, md in outbox.sent if md] == ["done: a/b.py:1-3"]


def test_cards_and_citations_read_as_tidy_text_in_a_chat() -> None:
    """The Mini App draws them as cards and chips; a chat gets the title as a link with its lines
    under it, and the sites a sentence cites as links in brackets — no block markers, no key names."""
    answer = (
        "Pick:\n\n:::card\ntitle: 1. Pantum P2500NW\nurl: https://kaspi.kz/p/1\nimage: https://img.example/p.jpg\nbadge: My pick\n"
        "meta: ≈ 45 000 ₸ · mono laser\n- 22 pages a minute\nlinks: [Kaspi](https://kaspi.kz/p/1) · [DNS](https://dns.example/p)\n"
        "note: check delivery\n:::\n:::entity\ntitle: Anchor Inference\nurl: https://example.org\nmeta: AI systems · Astana\n:::\n\n"
        "Order it.[^](https://kaspi.kz/p/1) [^DNS](https://www.dns-shop.kz/x) Quoted: `[^](https://a.example)`"
    )
    flat = flatten_evidence(answer)
    assert ":::" not in flat and "image:" not in flat and "title:" not in flat
    assert "**[1. Pantum P2500NW](https://kaspi.kz/p/1)** · *My pick*\n≈ 45 000 ₸ · mono laser\n- 22 pages a minute\n" in flat
    assert "[Kaspi](https://kaspi.kz/p/1) · [DNS](https://dns.example/p)\n*check delivery*" in flat
    assert "### [Anchor Inference](https://example.org)\nAI systems · Astana" in flat
    assert "Order it. ([kaspi.kz](https://kaspi.kz/p/1), [DNS](https://www.dns-shop.kz/x))" in flat
    assert "`[^](https://a.example)`" in flat
    assert flatten_evidence("```\n:::card\ntitle: x\n:::\n```") == "```\n:::card\ntitle: x\n:::\n```"


def test_a_flow_reads_as_rows_and_arrows_in_a_chat() -> None:
    """The Mini App draws a flow as a panel of nodes; a chat gets the rows one per line with the arrows
    between them, the header bold, the highlighted node bold and the caption muted."""
    answer = (
        "The plan:\n\n:::flow 💡 Pilot project\n💬 Customer — writes in the chat\n\n🤖 ==AI assistant== — answers\n"
        "↓ when it cannot answer\n✅ Solved -> 🎧 Operator | 📊 Report\nv\nDone\nnote: Two weeks\n:::\nAfter."
    )
    flat = flatten_evidence(answer)
    assert flat == (
        "The plan:\n\n**💡 Pilot project**\n💬 Customer — writes in the chat\n↓\n🤖 **AI assistant** — answers\n"
        "↓ when it cannot answer\n✅ Solved → 🎧 Operator · 📊 Report\n↓\nDone\n*Two weeks*\n\nAfter."
    )


def test_a_mermaid_fence_and_a_quoted_flow_stay_code_in_a_chat() -> None:
    fence = "```mermaid\nflowchart TD\n  A --> B\n```"
    assert flatten_evidence(fence) == fence
    quoted = "```\n:::flow\nA -> B\n:::\n```"
    assert flatten_evidence(quoted) == quoted
