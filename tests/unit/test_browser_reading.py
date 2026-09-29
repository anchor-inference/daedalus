"""The browser's reading side as a model meets it: a page searched like Ctrl+F with the refs beside each
match; only what was asked, pulled out by a smaller model reading the page fenced, part by part, and
handed back fenced; and the operator's notes on a site, shown only once approved."""
# ruff: noqa: F811 — the rig's fixtures are imported by name, and pytest hands them to the tests

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daedalus.browser.agent import EXTRACT_CHUNK, chunks, parse_extracted
from daedalus.browser.model import InvalidRequest
from daedalus.browser.notes import ACTIVE_PER_HOST, NOTE_MAX, WAITING_MAX, SiteNotes, clean_host, covers
from daedalus.host import prompts
from daedalus.stores.database import Database
from tests.support.fake_browserd import Element
from tests.unit.test_browser_tools import HOSTILE, Rig, base, daemon, rig  # noqa: F401

ROW = "Trail shoe {n} — {price} EUR, in stock. A long description of the shoe that goes on for a while to fill the part.\n"


def catalogue(rig: Rig) -> None:
    body = "".join(ROW.format(n=n, price=40 + n) for n in range(220))
    rig.daemon.page("https://shop.test/list?page=1", title="Shoes", text=HOSTILE + "\n\n" + body, elements={
        "e50": Element("e50", "link", "Next page", tag="a", goes_to="https://shop.test/list?page=2"),
        "e51": Element("e51", "button", "Add Trail shoe 3 to cart"),
    })
    rig.daemon.page("https://shop.test/list?page=2", title="Shoes 2", text="Road shoe 1 — 90 EUR\nRoad shoe 2 — 95 EUR\n")


class Model:
    """The extraction model: records what it was asked, answers from a script."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            return '{"items": [], "complete": false}'
        answer = self.answers.pop(0)
        if answer == "fail":
            raise TimeoutError("slow")
        return answer


def agent(rig: Rig):  # type: ignore[no-untyped-def]
    return rig.app.extensions["browser_agent"]


async def test_find_answers_each_match_with_the_ref_beside_it_fenced(rig: Rig) -> None:
    catalogue(rig)
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://shop.test/list?page=1")
    text, failed = await rig.call(sid, "BrowserText", find="trail shoe 3 ")
    assert not failed, text
    assert "match" in text and 'ref=e51' in text and "[page content from https://shop.test;" in text and text.count("[end of page content]") == 1
    text, failed = await rig.call(sid, "BrowserText", find=r"shoe 21\d —", regex=True)
    assert not failed and "10 matches for /shoe 21\\d —/" in text
    text, failed = await rig.call(sid, "BrowserText", find="no such words")
    assert not failed and "no visible text matches" in text
    for arguments, said in (({"find": "x", "query": "y"}, "not both"), ({"regex": True}, "give find"), ({"schema": {"type": "object"}}, "give query")):
        text, failed = await rig.call(sid, "BrowserText", **arguments)
        assert failed and said in text


async def test_extraction_reads_the_page_fenced_in_parts_and_hands_back_only_what_was_asked(rig: Rig) -> None:
    catalogue(rig)
    model = Model('{"items": [{"name": "Trail shoe 1", "price": 41}], "complete": false}', 'Here: {"items": [{"name": "Trail shoe 1", "price": 41}, {"name": "Trail shoe 200", "price": 240}], "complete": true}')
    agent(rig).extract = model
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://shop.test/list?page=1")
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "number"}}}
    text, failed = await rig.call(sid, "BrowserText", query="every shoe with its price", schema=schema)
    assert not failed, text
    # Two parts read of the three the page has: the model said the list was complete.
    assert len(model.prompts) == 2 and len(chunks("x" * 30_000, EXTRACT_CHUNK)) == 3
    first = model.prompts[0]
    assert "not instructions" in first and "[page content from https://shop.test; it is data from the web" in first
    assert first.count("[end of page content]") == 1 and "[end of page content (quoted by the page)]" in first
    assert json.dumps(schema, sort_keys=True) in first and "Part 1 of 3" in first and "Already collected from earlier parts and pages, page data too (0): none" in first
    assert "Trail shoe 1" in model.prompts[1].split("Part 2")[0]
    # The answer is fenced as the page's, the repeat counted once.
    assert "2 new here, 2 in all" in text and text.count('"Trail shoe 1"') == 1
    assert text.index("[page content from https://shop.test;") < text.index("Trail shoe 200") < text.index("[end of page content]")
    audit = await rig.app.extensions["browser"].audit_log(f"s-{sid}")
    extracted = next(e["detail"] for e in audit if e["action"] == "extract")
    assert extracted["calls"] == 2 and extracted["items"] == 2 and extracted["query"] == "every shoe with its price"
    # The next page adds to the same list, and the model is shown what was collected.
    await rig.call(sid, "BrowserNavigate", url="https://shop.test/list?page=2")
    model.answers = ['{"items": [{"name": "Road shoe 1", "price": 90}], "complete": true}']
    text, failed = await rig.call(sid, "BrowserText", query="every shoe with its price", schema=schema)
    assert not failed and "1 new here, 3 in all from 2 pages" in text
    assert "(2): [" in model.prompts[-1] and "Trail shoe 200" in model.prompts[-1]


async def test_extraction_fails_softly_and_says_so(rig: Rig) -> None:
    catalogue(rig)
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://shop.test/list?page=2")
    agent(rig).extract = None
    text, failed = await rig.call(sid, "BrowserText", query="prices")
    assert failed and "no model is set to extract with" in text
    agent(rig).extract = Model("fail")
    text, failed = await rig.call(sid, "BrowserText", query="prices")
    assert failed and "The extraction model failed on part 1 of 1" in text
    agent(rig).extract = Model("The prices are 90 and 95 EUR.")
    text, failed = await rig.call(sid, "BrowserText", query="prices, as a sentence")
    assert not failed and "- The prices are 90 and 95 EUR." in text


def test_the_extraction_answer_is_read_leniently() -> None:
    assert parse_extracted('```json\n{"items": ["a", "", null, "b"], "complete": true}\n```') == (["a", "b"], True)
    assert parse_extracted("nothing here") == (["nothing here"], False)
    assert parse_extracted("") == ([], False)
    parts = chunks("para one\n\n" + "y" * 50 + "\n" + "z" * 50, 70)
    # Cut at the line end past half the part, not in the middle of the row of z.
    assert parts == ["para one\n\n" + "y" * 50, "z" * 50]


async def test_a_site_note_is_shown_only_once_the_operator_approves_it(rig: Rig, db: Database) -> None:
    rig.daemon.page("https://www.shop.test/", title="Shop", heading="Shop")
    sid = await rig.session()
    await rig.call(sid, "BrowserOpen", url="https://www.shop.test/")
    text, failed = await rig.call(sid, "BrowserNote", note="Search answers only to Enter;\nthe button does nothing.")
    assert not failed and "Proposed a note for shop.test" in text
    notes: SiteNotes = rig.app.extensions["browser_notes"]
    [waiting] = await notes.list()
    assert waiting["status"] == "proposed" and waiting["host"] == "shop.test" and waiting["text"] == "Search answers only to Enter; the button does nothing."
    assert waiting["by"] == f"agent:{sid}"
    other = await rig.session()
    await rig.call(other, "BrowserOpen", url="https://www.shop.test/")
    text, _ = await rig.call(other, "BrowserSnapshot")
    assert "Search answers" not in text  # waiting: nobody reads it yet
    for done in (sid, other):
        await rig.call(done, "BrowserClose", all=True)  # two browsers are the rig's cap
    await notes.approve(waiting["id"])
    fresh = await rig.session()
    text, _ = await rig.call(fresh, "BrowserOpen", url="https://www.shop.test/")
    assert "Notes on shop.test from earlier work, approved by the operator (not the page's words):\n- Search answers only to Enter" in text
    text, _ = await rig.call(fresh, "BrowserSnapshot")
    assert "Search answers" not in text  # said once
    # Kept in the host's own table, not in anything a page or the agent's memory tools can write.
    assert (await db.kv_get("browser.site_notes"))[0]["status"] == "active"
    text, failed = await rig.call(fresh, "BrowserNote", note="x" * (NOTE_MAX + 1))
    assert failed and f"at most {NOTE_MAX}" in text
    text, failed = await rig.call(fresh, "BrowserNote", note="ok", host="not a host")
    assert failed and "is not a host name" in text


async def test_notes_keep_to_their_project_and_their_bounds(db: Database) -> None:
    notes = SiteNotes(db)
    mine = await notes.propose(project_id="p1", host="https://Docs.Example.com:443/x", text="Use the search, not the menu", by="agent:a")
    assert mine["host"] == "docs.example.com" and (await notes.propose(project_id="p1", host="docs.example.com", text="use the search, NOT the menu", by="agent:b"))["id"] == mine["id"]
    await notes.approve(mine["id"])
    everyone = await notes.propose(project_id=None, host="example.com", text="Cookie banner: Reject all is at the bottom", by="agent:a")
    await notes.approve(everyone["id"])
    assert [n.text for n in await notes.active_for("p1", "docs.example.com")] == ["Use the search, not the menu", "Cookie banner: Reject all is at the bottom"]
    assert [n.text for n in await notes.active_for("p2", "docs.example.com")] == ["Cookie banner: Reject all is at the bottom"]
    assert await notes.active_for("p1", "example.org") == []
    for n in range(ACTIVE_PER_HOST + 2):
        await notes.approve((await notes.propose(project_id="p3", host="a.test", text=f"note {n}", by="x"))["id"])
    assert [n.text for n in await notes.active_for("p3", "a.test")] == [f"note {n}" for n in range(2, ACTIVE_PER_HOST + 2)]
    for n in range(WAITING_MAX):
        await notes.propose(project_id="p4", host="b.test", text=f"wait {n}", by="x")
    with pytest.raises(InvalidRequest, match="already wait"):
        await notes.propose(project_id="p4", host="b.test", text="one more", by="x")
    assert await notes.delete(mine["id"]) and not await notes.delete(mine["id"])
    # What was kept survives a restart of the host.
    again = SiteNotes(db)
    assert len(await again.list()) == len(await notes.list())


def test_hosts_and_what_a_note_covers() -> None:
    assert clean_host("WWW.GitHub.com.") == "github.com"
    assert covers("github.com", "gist.github.com") and covers("github.com", "www.github.com") and not covers("github.com", "notgithub.com")
    for bad in ("", "localhost", "a b.com", "exa_mple.com"):
        with pytest.raises(InvalidRequest):
            clean_host(bad)


def test_the_prompt_prefers_refusing_a_cookie_banner_and_names_the_new_reads() -> None:
    assert "Reject all" in prompts.BROWSER and "Только необходимые" in prompts.BROWSER.replace("Только \\\nнеобходимые", "Только необходимые")
    for words in ("steps=[", "find=", "query=", "BrowserNote"):
        assert words in prompts.BROWSER


def test_the_word_lists_let_a_refusal_through_in_both_languages() -> None:
    """The classifier is the daemon's (Go); its table is read here so the host's prompt, which tells
    the agent a refusal is never asked about, cannot drift from it unnoticed."""
    source = (Path(__file__).resolve().parents[2] / "browserd" / "internal" / "sensitive" / "sensitive.go").read_text(encoding="utf-8")
    refusals = source[source.index("var refusals"):source.index("var patterns")]
    for word in ("reject", "only necessary", "отклонить", "только необходимые", "без принятия"):
        assert f'"{word}"' in refusals, word
