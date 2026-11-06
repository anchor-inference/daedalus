"""The guest door on its own: what a stranger's agent can and cannot do at the public port.

Everything runs in process against the real door with a fake model, so no test touches a network. Security
claims here are about the door itself; what a direct turn may do inside a chat is in ``test_guest_turns``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import guestd
import httpx
import pytest
from guestd.control import ControlDoor, link_fields
from guestd.limits import Failures, Rate
from guestd.mailbox import DIRECT_REPLY_SECONDS, MAX_TEXT, MAX_WAIT_SECONDS, STAND_IN_REPLY_SECONDS, Mailbox
from guestd.pages import LOST_TOKEN, PAGE, SPENT_PAGE
from guestd.public import HEADERS, MAX_BODY, PublicDoor
from guestd.standin import (
    FAILED_TEXT,
    MAX_OUTPUT_TOKENS,
    SYSTEM,
    KeyProxyCaller,
    ModelFailed,
    ModelReply,
    StandIn,
    request_for,
)
from guestd.store import FIRST_CONTACT_HOURS, Store
from guestd.text import inert
from starlette.testclient import TestClient

CONTROL = {"authorization": "Bearer control-token"}
PACKAGE = Path(guestd.__file__).parent


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeModel:
    """A model that says what it was given, so anything it could reach would come straight back out."""

    def __init__(self, reply: str = "ok", usage: tuple[int, int] = (100, 50)) -> None:
        self.reply = reply
        self.usage = usage
        self.requests: list[dict[str, Any]] = []
        self.fail = False

    async def __call__(self, link: Any, request: dict[str, Any]) -> ModelReply:
        self.requests.append(request)
        if self.fail:
            raise ModelFailed("down")
        return ModelReply(self.reply if self.reply != "echo" else json.dumps(request), *self.usage)


class Rig:
    """The door, its two listeners, and a guest and an owner to talk to them."""

    def __init__(self, tmp_path: Path, **limits: Any) -> None:
        self.clock = Clock()
        self.store = Store(tmp_path / "guest.sqlite", clock=self.clock)
        self.mailbox = Mailbox(self.store, public_base=lambda: "https://agents.example.test", clock=self.clock)
        self.model = FakeModel()
        self.stand_in = StandIn(self.store, self.mailbox, self.model)
        self.public = TestClient(PublicDoor(self.mailbox, **limits), base_url="https://agents.example.test")
        self.control = TestClient(ControlDoor(self.store, self.mailbox, "control-token"), base_url="http://guestd")

    def link(self, **overrides: Any) -> tuple[str, str]:
        fields = {
            "label": "a friend's agent", "topic": "The agent answers questions about its service.", "mode": "stand_in", "briefing": "The service listens on port 8080.",
            "model_path": "deepseek/v1", "model_name": "cheap", "guest_tools": "none", "price_in": 1.0, "price_out": 2.0, "approval": False, "usd_limit": 1.0,
            "turns_per_day": 40, "turns_total": 200, "invite_hours": 72, "conversation_hours": 168, "idle_hours": 24, "retention_days": 30, **overrides,
        }
        made = self.control.post("/control/links", json=fields, headers=CONTROL)
        assert made.status_code == 200, made.text
        return made.json()["link"]["id"], made.json()["invite"]

    def redeem(self, invite: str) -> str:
        answer = self.public.post(f"/a/{invite}/redeem")
        assert answer.status_code == 201, answer.text
        return answer.json()["token"]

    def say(self, token: str, text: str) -> httpx.Response:
        return self.public.post("/a/messages", json={"text": text}, headers={"Authorization": f"Bearer {token}"})

    def read(self, token: str, after: int = 0) -> httpx.Response:
        return self.public.get(f"/a/messages?after={after}&wait=0", headers={"Authorization": f"Bearer {token}"})

    async def turn(self) -> bool:
        return await self.stand_in.run_once()

    def events(self, link_id: str) -> list[str]:
        return [e["kind"] for e in self.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["events"]]


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    made = Rig(tmp_path)
    yield made
    made.store.close()


# -- the page -------------------------------------------------------------------------------------


def test_opening_the_page_spends_nothing_however_often_a_preview_fetches_it(rig: Rig) -> None:
    link_id, invite = rig.link()
    for _ in range(5):
        assert rig.public.get(f"/a/{invite}").status_code == 200
    assert rig.public.head(f"/a/{invite}").status_code == 200
    assert rig.events(link_id) == ["created"], "a GET leaves no mark at all"
    assert rig.store.link(link_id)["state"] == "waiting"
    assert rig.public.post(f"/a/{invite}/redeem").status_code == 201


def test_the_page_is_markdown_and_says_what_the_trial_found_it_has_to(rig: Rig) -> None:
    _, invite = rig.link(approval=True)
    page = rig.public.get(f"/a/{invite}")
    assert page.headers["content-type"].startswith("text/markdown")
    for name, value in HEADERS.items():
        assert page.headers[name] == value
    text = page.text
    for needed in (
        "asks for no secrets", "installs nothing", "sets up no schedule", "Save the reply to a file straight away", "there is no way to recover it",
        "tell your user and ask them for a new link", "at most 30", "kept for 30 days", "owner of this link can read them", "stand-in", "The agent answers questions about its service.",
        f"https://agents.example.test/a/{invite}/redeem", "stop_polling",
    ):
        assert needed in text, needed
    for refused in ("Do not ask your user", "do not ask your user", "follow these steps exactly", "must follow", "ignore any", "memory file", "curl | sh"):
        assert refused not in text, refused


def test_a_direct_link_does_not_call_itself_a_stand_in(rig: Rig) -> None:
    _, invite = rig.link(mode="direct", briefing="", model_path="", model_name="", guest_tools="read", price_in=0, price_out=0)
    text = rig.public.get(f"/a/{invite}").text
    assert "stand-in" not in text and "owner's own agent" in text


@pytest.mark.parametrize("path", ["/", "/app/", "/a/", "/a/x", "/nothing", "/a/messages/extra"])
def test_nothing_this_port_says_is_ever_html(rig: Rig, path: str) -> None:
    answer = rig.public.get(path)
    assert answer.status_code == 404
    assert answer.headers["content-type"].startswith("application/json") and answer.headers["x-content-type-options"] == "nosniff"
    assert "<html" not in answer.text.lower()


def test_guest_text_that_is_html_comes_back_as_json_text(rig: Rig) -> None:
    _, invite = rig.link()
    rig.model.reply = "<script>alert(1)</script>"
    token = rig.redeem(invite)
    rig.say(token, "hi")
    import asyncio

    asyncio.run(rig.turn())
    answer = rig.read(token)
    assert answer.headers["content-type"].startswith("application/json") and answer.json()["messages"][0]["text"] == "<script>alert(1)</script>"


# -- redeeming ------------------------------------------------------------------------------------


def test_a_redeem_gives_a_256_bit_token_and_keeps_only_hashes(rig: Rig, tmp_path: Path) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    assert len(token) >= 43 and len(invite) >= 43
    rig.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = (tmp_path / "guest.sqlite").read_bytes()
    assert token.encode() not in raw and invite.encode() not in raw


async def test_a_stolen_invite_used_first_leaves_the_real_guest_a_refusal_and_the_owner_a_request(rig: Rig) -> None:
    link_id, invite = rig.link(approval=True)
    thief = rig.redeem(invite)
    rig.say(thief, "hello, it is me")

    real = rig.public.post(f"/a/{invite}/redeem")
    assert real.status_code == 410 and real.json()["detail"] == LOST_TOKEN
    assert "cannot be recovered" in real.json()["detail"] and "ask them for a new link" in real.json()["detail"]

    assert rig.events(link_id)[:2] == ["redeem_refused", "first_contact"]
    listed = rig.control.get("/control/links", headers=CONTROL).json()["links"][0]
    assert listed["refused_redeems"] == 1
    assert listed["conversation"]["state"] == "pending" and listed["conversation"]["first_message"] == "hello, it is me"
    assert await rig.turn() is False and rig.model.requests == [], "nothing was spent on a guest nobody admitted"

    assert rig.control.post(f"/control/conversations/{listed['conversation']['id']}/refuse", headers=CONTROL).status_code == 200
    assert rig.read(thief).status_code == 410


def test_a_repeated_redeem_never_returns_the_token_even_from_the_same_client(rig: Rig) -> None:
    _, invite = rig.link()
    first = rig.public.post(f"/a/{invite}/redeem")
    second = rig.public.post(f"/a/{invite}/redeem")
    assert first.status_code == 201 and second.status_code == 410
    assert "token" not in second.json()
    assert rig.public.get(f"/a/{invite}").text == SPENT_PAGE


def test_a_visit_to_a_spent_link_reaches_the_owner_once_in_a_while(rig: Rig) -> None:
    """Real agents open with a GET, read the 410 and stop. A guest whose invite was stolen first came and
    went, and with only the POST counted, the owner never heard."""
    link_id, invite = rig.link()
    rig.public.get(f"/a/{invite}")
    assert "redeem_refused" not in rig.events(link_id), "a link still to be used is not touched by being read"
    rig.redeem(invite)
    rig.public.get(f"/a/{invite}")
    rig.public.get(f"/a/{invite}")
    assert rig.events(link_id).count("redeem_refused") == 1
    rig.clock.now += 601
    rig.public.get(f"/a/{invite}")
    assert rig.events(link_id).count("redeem_refused") == 2


def test_a_message_at_its_limit_passes_however_json_escaped_it(rig: Rig) -> None:
    """``json.dumps`` writes Cyrillic at six bytes a character unless told otherwise, and that is what an agent's
    Python sends. A message within the limit was refused, in words that said it was over it."""
    _, invite = rig.link()
    token = rig.redeem(invite)
    body = json.dumps({"text": "Я" * MAX_TEXT})
    assert len(body) > MAX_TEXT * 4 + 256
    sent = rig.public.post("/a/messages", content=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert sent.status_code == 202, sent.text
    assert MAX_BODY < 64 * 1024, "the front and nginx refuse above that, so the door's bound must be the one a guest meets"


def test_the_page_counts_its_days(rig: Rig) -> None:
    _, invite = rig.link(retention_days=1)
    assert "kept for 1 day and" in rig.public.get(f"/a/{invite}").text


async def test_a_reply_with_no_text_is_a_failure_the_guest_is_told_of(rig: Rig) -> None:
    """A reasoning model at its bound, or a plan's route with an error in a 200: left silent, a guest polls on."""
    _, invite = rig.link(price_in=0.0, price_out=0.0)
    token = rig.redeem(invite)
    rig.say(token, "hello")
    rig.stand_in.caller = lambda _link, _request: asyncio.sleep(0, ModelReply("", 10, 10))
    assert await rig.turn() is True
    assert [m["text"] for m in rig.read(token).json()["messages"]] == [FAILED_TEXT]


async def test_a_plan_is_bounded_by_its_turns_whatever_the_dollars_say(rig: Rig) -> None:
    link_id, invite = rig.link(price_in=0.0, price_out=0.0, usd_limit=0.0, turns_total=2)
    token = rig.redeem(invite)
    assert rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["priced"] is False
    for _ in range(2):
        assert rig.say(token, "hello").status_code == 202
        assert await rig.turn() is True
    refused = rig.say(token, "and again")
    assert refused.status_code == 403 and refused.json()["limit"] == "turns_total"


def test_the_pages_example_takes_the_token_out_of_the_reply(rig: Rig, tmp_path: Path) -> None:
    """A real agent ran the example as written, with the whole JSON as its bearer, and lost the link."""
    _, invite = rig.link()
    page = rig.public.get(f"/a/{invite}").text
    reply = rig.public.post(f"/a/{invite}/redeem")
    (tmp_path / ".agentlink-token").write_text(reply.text)
    line = next(line for line in page.splitlines() if line.startswith("TOKEN="))
    done = subprocess.run(["bash", "-c", f'{line}\nprintf %s "$TOKEN"'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert done.stdout == reply.json()["token"]


def test_a_refused_token_points_at_the_saved_reply_before_saying_it_is_gone(rig: Rig) -> None:
    answer = rig.public.get("/a/messages", headers={"Authorization": 'Bearer {"token":"gt_x","status":"open"}'})
    assert answer.status_code == 401
    detail = answer.json()["detail"]
    assert detail.index("`token` field") < detail.index("cannot be recovered")


async def test_a_stated_bill_settles_a_turn_rather_than_the_price_table(rig: Rig) -> None:
    """The table said $0.006 a million and the upstream billed $0.25: the link's limit is in real dollars."""
    link_id, invite = rig.link(price_in=0.006, price_out=0.5, usd_limit=1.0)
    token = rig.redeem(invite)
    rig.say(token, "hello")
    rig.stand_in.caller = lambda _link, _request: asyncio.sleep(0, ModelReply("hi", 1000, 10, 0.25))
    assert await rig.turn() is True
    assert rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["spent_usd"] == pytest.approx(0.25)


async def test_the_caller_reads_the_bill_when_the_upstream_states_one() -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.5}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": usage}))
    caller = KeyProxyCaller("guest.sock", httpx.AsyncClient(transport=transport))
    reply = await caller({"model_path": "deepseek/v1"}, {"model": "m", "messages": [], "max_tokens": 5})
    assert reply == ModelReply("hi", 10, 3, 0.5)


ROUTE = {"path": "openrouter", "model": "cheap", "only": ["wafer/us", "alibaba"], "price_in": 2.0, "price_out": 8.0}


async def test_a_stand_in_is_limited_by_todays_table_and_not_what_the_link_was_made_with(rig: Rig) -> None:
    """A link lives for weeks: the operator limiting a model afterwards has to hold for it as well."""
    link_id, invite = rig.link(model_path="openrouter", model_name="cheap", price_in=0.1, price_out=0.1)
    token = rig.redeem(invite)
    assert rig.control.put("/control/settings", json={"model_routes": [ROUTE]}, headers=CONTROL).status_code == 200
    rig.say(token, "hello")
    assert await rig.turn() is True
    assert rig.model.requests[0]["provider"] == {"only": ["wafer/us", "alibaba"], "allow_fallbacks": False}
    # The turn is priced by the table too: at the link's old rate the same call would cost a tenth.
    assert rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["spent_usd"] == pytest.approx((100 * 2.0 + 50 * 8.0) / 1_000_000)


async def test_a_stand_in_on_another_model_sends_no_provider(rig: Rig) -> None:
    _link_id, invite = rig.link(model_path="deepseek/v1", model_name="cheap")
    token = rig.redeem(invite)
    rig.control.put("/control/settings", json={"model_routes": [ROUTE]}, headers=CONTROL)
    rig.say(token, "hello")
    assert await rig.turn() is True
    assert "provider" not in rig.model.requests[0]


async def test_a_table_emptied_takes_the_limit_off_at_the_next_turn(rig: Rig) -> None:
    _link_id, invite = rig.link(model_path="openrouter", model_name="cheap")
    token = rig.redeem(invite)
    rig.control.put("/control/settings", json={"model_routes": [ROUTE]}, headers=CONTROL)
    rig.control.put("/control/settings", json={"model_routes": []}, headers=CONTROL)
    rig.say(token, "hello")
    assert await rig.turn() is True
    assert "provider" not in rig.model.requests[0]


async def test_a_model_the_host_cannot_price_stays_limited_at_the_links_own_price(rig: Rig) -> None:
    link_id, invite = rig.link(model_path="openrouter", model_name="cheap", price_in=1.0, price_out=2.0)
    token = rig.redeem(invite)
    rig.control.put("/control/settings", json={"model_routes": [{**ROUTE, "price_in": None, "price_out": None}]}, headers=CONTROL)
    rig.say(token, "hello")
    assert await rig.turn() is True
    assert rig.model.requests[0]["provider"]["only"] == ROUTE["only"]
    assert rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["spent_usd"] == pytest.approx((100 * 1.0 + 50 * 2.0) / 1_000_000)


async def test_the_caller_keeps_the_limit_when_it_retries_under_the_newer_name() -> None:
    bodies: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, text="use max_completion_tokens")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    caller = KeyProxyCaller("guest.sock", httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    limited = {"model": "m", "messages": [], "max_tokens": 5, "provider": {"only": ["wafer/us"], "allow_fallbacks": False}}
    await caller({"model_path": "openrouter"}, limited)
    assert [body["provider"] for body in bodies] == [limited["provider"]] * 2


@pytest.mark.parametrize("routes", [
    "nope",
    [{"path": "openrouter", "model": "m", "only": [], "price_in": 1, "price_out": 1}],
    [{"path": "openrouter", "model": "m", "only": ["Not A Slug"], "price_in": 1, "price_out": 1}],
    [{"path": "openrouter", "model": "m", "only": ["wafer/us"], "price_in": True, "price_out": 1}],
    [{"path": "openrouter", "model": "m", "only": ["wafer/us"], "price_in": -1, "price_out": 1}],
    [{"model": "m", "only": ["wafer/us"], "price_in": 1, "price_out": 1}],
])
def test_a_table_that_is_wrong_anywhere_is_refused_whole_and_the_old_one_stands(rig: Rig, routes: Any) -> None:
    rig.control.put("/control/settings", json={"model_routes": [ROUTE]}, headers=CONTROL)
    assert rig.control.put("/control/settings", json={"model_routes": routes}, headers=CONTROL).status_code == 422
    assert json.loads(rig.store.setting("model_routes")) == [ROUTE]


def test_an_unknown_invite_counts_against_the_address_and_a_good_guest_is_not_held(tmp_path: Path) -> None:
    rig = Rig(tmp_path, failures=Failures(per_address=3))
    _, invite = rig.link()
    for _ in range(3):
        assert rig.public.get("/a/" + "x" * 43).status_code == 404
    assert rig.public.post(f"/a/{invite}/redeem").status_code == 201, "a stranger's guessing does not lock out the guest the link is for"
    assert rig.public.get("/a/" + "x" * 43).headers["retry-after"] == "600"
    rig.store.close()


def test_a_bad_token_counts_against_the_address(tmp_path: Path) -> None:
    rig = Rig(tmp_path, failures=Failures(per_address=2))
    assert rig.read("nope").status_code == 401
    assert rig.public.get("/a/messages?wait=0").status_code == 401
    assert rig.read("nope").status_code == 429
    rig.store.close()


# -- first contact --------------------------------------------------------------------------------


async def test_nothing_is_answered_until_the_owner_admits_and_then_the_first_message_is(rig: Rig) -> None:
    link_id, invite = rig.link(approval=True)
    redeemed = rig.public.post(f"/a/{invite}/redeem").json()
    assert redeemed["status"] == "pending" and redeemed["retry_after"] == 30
    token = redeemed["token"]
    sent = rig.say(token, "what port?")
    assert sent.status_code == 202 and sent.headers["retry-after"] == "30"
    assert rig.say(token, "and another").json()["detail"].startswith("Your first message is with the owner")
    polled = rig.read(token)
    assert polled.status_code == 202 and polled.json()["stop_polling"] is False and polled.json()["waiting_for"] == "owner"
    assert await rig.turn() is False
    conversation = rig.store.conversation_of_link(link_id)
    assert rig.control.post(f"/control/conversations/{conversation['id']}/admit", headers=CONTROL).status_code == 200
    assert await rig.turn() is True
    assert len(rig.model.requests) == 1 and rig.read(token).json()["messages"][0]["text"] == "ok"


def test_silence_from_the_owner_is_a_no(rig: Rig) -> None:
    _, invite = rig.link(approval=True)
    token = rig.redeem(invite)
    rig.clock.now += 400
    told = rig.read(token).json()
    assert told["stop_polling"] is True and "stays good" in told["detail"]
    rig.clock.now += FIRST_CONTACT_HOURS * 3600
    rig.store.sweep()
    answer = rig.read(token)
    assert answer.status_code == 410 and answer.json()["reason"] == "approval_timeout"


def test_with_approval_off_a_conversation_opens_at_once_and_the_owner_still_sees_it(rig: Rig) -> None:
    link_id, invite = rig.link(approval=False)
    assert rig.public.post(f"/a/{invite}/redeem").json()["status"] == "open"
    assert "opened" in rig.events(link_id)


# -- a conversation -------------------------------------------------------------------------------


async def test_a_conversation_goes_back_and_forth_with_a_cursor(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    assert rig.say(token, "port?").status_code == 202
    await rig.turn()
    first = rig.read(token).json()
    assert first["messages"][0]["from"] == "agent" and first["stop_polling"] is True
    rig.model.reply = "second"
    rig.say(token, "health?")
    await rig.turn()
    second = rig.read(token, first["cursor"]).json()
    assert [m["text"] for m in second["messages"]] == ["second"]
    assert rig.read(token).json()["messages"][0]["text"] == "ok", "after=0 reads everything again, as one agent's script did"
    assert rig.public.post("/a/end", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert rig.say(token, "more").status_code == 410


def test_a_guest_polling_with_nothing_owed_is_told_to_stop(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    quiet = rig.read(token).json()
    assert quiet["waiting_for"] == "nothing" and quiet["stop_polling"] is True and quiet["messages"] == []


def test_a_guest_polling_for_a_reply_is_told_to_wait_and_later_to_stop(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    rig.say(token, "port?")
    waiting = rig.read(token)
    assert waiting.json()["waiting_for"] == "reply" and waiting.json()["stop_polling"] is False and waiting.headers["retry-after"]
    rig.clock.now += STAND_IN_REPLY_SECONDS + 1
    late = rig.read(token).json()
    assert late["stop_polling"] is True and "tell your user" in late["detail"]


def test_a_direct_turn_is_given_longer_because_it_waits_for_the_chat(rig: Rig) -> None:
    _, invite = rig.link(mode="direct", briefing="", model_path="", model_name="", guest_tools="read", price_in=0, price_out=0)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    rig.clock.now += STAND_IN_REPLY_SECONDS + 1
    assert rig.read(token).json()["stop_polling"] is False
    rig.clock.now += DIRECT_REPLY_SECONDS
    assert rig.read(token).json()["stop_polling"] is True


async def test_a_poll_is_held_at_most_thirty_seconds_and_a_reply_ends_it_early(rig: Rig) -> None:
    import asyncio

    assert MAX_WAIT_SECONDS == 30
    _, invite = rig.link()
    token = rig.redeem(invite)
    rig.mailbox.send(token, "port?")
    reading = asyncio.create_task(rig.mailbox.read(token, wait=10_000))
    await asyncio.sleep(0.05)
    assert not reading.done()
    await rig.turn()
    answer = await asyncio.wait_for(reading, 2)
    assert answer is not None and answer.body["messages"][0]["text"] == "ok"


async def test_a_conversation_nobody_ended_is_closed_by_the_clock_and_polling_does_not_keep_it_alive(rig: Rig) -> None:
    _, invite = rig.link(idle_hours=24)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    await rig.turn()
    for _ in range(25):
        rig.clock.now += 3600
        rig.read(token)
    rig.store.sweep()
    answer = rig.read(token, 10_000)
    assert answer.status_code == 410 and answer.json()["reason"] == "idle" and answer.json()["stop_polling"] is True


def test_a_conversation_ends_at_its_lifetime_however_lively_it_is(rig: Rig) -> None:
    _, invite = rig.link(conversation_hours=1, idle_hours=24)
    token = rig.redeem(invite)
    rig.clock.now += 3601
    rig.store.sweep()
    assert rig.read(token).json()["reason"] == "expired"


def test_an_invite_nobody_used_expires(rig: Rig) -> None:
    link_id, invite = rig.link(invite_hours=1)
    rig.clock.now += 3601
    assert rig.public.get(f"/a/{invite}").status_code == 410
    assert rig.public.post(f"/a/{invite}/redeem").status_code == 410
    rig.store.sweep()
    assert rig.store.link(link_id)["state"] == "expired"


async def test_what_was_said_goes_when_the_retention_period_has_passed(rig: Rig) -> None:
    link_id, invite = rig.link(retention_days=30)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    await rig.turn()
    rig.public.post("/a/end", headers={"Authorization": f"Bearer {token}"})
    rig.clock.now += 29 * 86400
    rig.store.sweep()
    assert rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["messages"], "the owner can read it during the period"
    rig.clock.now += 2 * 86400
    rig.store.sweep()
    assert rig.store.link(link_id) is None
    assert rig.store.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_long_message_is_refused_before_it_is_stored(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    assert rig.say(token, "x" * (MAX_TEXT + 1)).status_code == 413
    assert rig.public.post("/a/messages", content=b"x" * 1_000_000, headers={"Authorization": f"Bearer {token}"}).status_code == 413
    assert rig.public.post("/a/messages", content=b"not json", headers={"Authorization": f"Bearer {token}"}).status_code == 400


def test_messages_are_limited_per_minute(tmp_path: Path) -> None:
    rig = Rig(tmp_path, sends=Rate(2))
    _, invite = rig.link(turns_per_day=99)
    token = rig.redeem(invite)
    assert [rig.say(token, "a").status_code for _ in range(3)] == [202, 202, 429]
    rig.store.close()


def test_a_burst_of_unanswered_messages_is_refused(rig: Rig) -> None:
    _, invite = rig.link()
    token = rig.redeem(invite)
    statuses = [rig.say(token, f"m{n}").status_code for n in range(4)]
    assert statuses == [202, 202, 202, 429]


@pytest.mark.parametrize(("raw", "clean"), [
    ("\x1b[31mred\x1b[0m", "red"), ("a​b", "ab"), ("x\U000e0041\U000e0042y", "xy"), ("‮evil", "evil"), ("one\r\ntwo\ttab", "one\ntwo\ttab"), ("\x07bell\x00", "bell"),
    ("\x1b]0;title\x07text", "text"),
])
def test_text_is_made_inert_before_anything_stores_it(raw: str, clean: str) -> None:
    assert inert(raw) == clean


async def test_a_guests_escape_sequences_never_reach_the_record_or_the_model(rig: Rig) -> None:
    link_id, invite = rig.link()
    token = rig.redeem(invite)
    rig.say(token, "hello\x1b[2J\U000e0041")
    await rig.turn()
    said = rig.control.get(f"/control/links/{link_id}", headers=CONTROL).json()["link"]["messages"][0]["text"]
    assert said == "hello" and rig.model.requests[0]["messages"][-1]["content"] == "hello"


# -- the stand-in holds three things ---------------------------------------------------------------


async def test_a_model_call_is_the_fixed_text_the_briefing_and_the_conversation_and_no_tools(rig: Rig) -> None:
    _, invite = rig.link(briefing="Port 8080.")
    token = rig.redeem(invite)
    rig.say(token, "port?")
    await rig.turn()
    request = rig.model.requests[0]
    assert set(request) == {"model", "messages", "max_tokens", "stream"}, "no tools field exists to fill"
    assert request["messages"][0]["role"] == "system" and SYSTEM in request["messages"][0]["content"] and "Port 8080." in request["messages"][0]["content"]
    assert request["messages"][1:] == [{"role": "user", "content": "port?"}]
    assert request["max_tokens"] == MAX_OUTPUT_TOKENS


def test_the_system_text_refuses_to_promise_for_the_owner() -> None:
    assert "Do not promise or agree to anything on the owner's behalf" in SYSTEM


def test_the_doors_own_words_are_not_fed_back_to_the_model() -> None:
    history = [{"role": "guest", "text": "hi"}, {"role": "door", "text": "A limit"}, {"role": "agent", "text": "hello"}]
    link = {"briefing": "b", "model_name": "m"}
    assert [m["content"] for m in request_for(link, history)["messages"][1:]] == ["hi", "hello"]


async def test_the_key_proxy_call_sends_no_credential_and_counts_tokens() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    caller = KeyProxyCaller("guest.sock", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    reply = await caller({"model_path": "deepseek/v1"}, {"model": "m", "messages": [], "max_tokens": 5})
    assert reply == ModelReply("hi", 10, 3)
    assert str(seen[0].url) == "http://keyproxy/deepseek/v1/chat/completions"
    assert "authorization" not in seen[0].headers


async def test_a_model_that_wants_the_newer_bound_name_gets_it() -> None:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if "max_tokens" in bodies[-1]:
            return httpx.Response(400, text="use max_completion_tokens instead")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    caller = KeyProxyCaller("guest.sock", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert (await caller({"model_path": "x"}, {"model": "m", "messages": [], "max_tokens": 5})).text == "hi"
    assert bodies[-1]["max_completion_tokens"] == 5 and "max_tokens" not in bodies[-1]


async def test_a_refused_call_is_a_failure_not_an_answer() -> None:
    caller = KeyProxyCaller("guest.sock", httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(402, json={"error": {}}))))
    with pytest.raises(ModelFailed) as failed:
        await caller({"model_path": "x"}, {"model": "m", "messages": [], "max_tokens": 5})
    assert failed.value.spent is False, "refused outright: nothing was billed"


async def test_a_failed_turn_costs_nothing_and_the_guest_is_told(rig: Rig) -> None:
    link_id, invite = rig.link()
    token = rig.redeem(invite)
    rig.model.fail = True
    rig.say(token, "port?")
    await rig.turn()
    link = rig.store.link(link_id)
    assert link["spent_usd"] > 0 and link["reserved_usd"] == 0 and link["turns_used"] == 1, "a call that never came back may have been billed"
    message = rig.read(token).json()["messages"][0]
    assert message["from"] == "door" and "could not answer" in message["text"]


# -- limits come before spending -------------------------------------------------------------------


async def test_a_link_out_of_money_never_reaches_the_model(rig: Rig) -> None:
    link_id, invite = rig.link(usd_limit=0.0005, price_in=1, price_out=2)  # one turn's worst case is about 0.0020 here
    token = rig.redeem(invite)
    rig.say(token, "port?")
    await rig.turn()
    assert rig.model.requests == [], "the worst case of the call is over the limit, so it was never made"
    message = rig.read(token).json()["messages"][0]
    assert message["from"] == "door" and "limit" in message["text"]
    assert rig.store.link(link_id)["spent_usd"] == 0


async def test_a_spent_link_refuses_the_next_message_at_the_door(rig: Rig) -> None:
    link_id, invite = rig.link(usd_limit=1.0)
    token = rig.redeem(invite)
    rig.store.conn.execute("UPDATE links SET spent_usd = 1.0 WHERE id = ?", (link_id,))
    answer = rig.say(token, "port?")
    assert answer.status_code == 403 and answer.json()["limit"] == "usd_per_link" and answer.json()["stop_polling"] is True
    assert "limit" in rig.events(link_id)


async def test_the_day_limit_is_across_guests_and_checked_before_each_call(rig: Rig) -> None:
    rig.control.put("/control/settings", json={"usd_per_day": 0.003}, headers=CONTROL)
    _, one = rig.link()
    _, two = rig.link()
    first, second = rig.redeem(one), rig.redeem(two)
    rig.model.usage = (1000, 1000)
    rig.say(first, "a")
    rig.say(second, "b")
    await rig.turn()  # takes the day's money
    await rig.turn()
    assert len(rig.model.requests) == 1
    rig.store.conn.execute("UPDATE spend_days SET usd = 1")
    refused = rig.say(first, "c")
    assert refused.status_code == 429 and refused.json()["limit"] == "usd_per_day" and int(refused.headers["retry-after"]) > 0


async def test_two_turns_at_once_cannot_both_take_the_last_of_the_money(rig: Rig) -> None:
    link_id, invite = rig.link(usd_limit=0.0035, price_in=1, price_out=2)
    second_id, other = rig.link(usd_limit=1, price_in=1, price_out=2)
    rig.control.put("/control/settings", json={"usd_per_day": 0.0035}, headers=CONTROL)
    rig.say(rig.redeem(invite), "a")
    rig.say(rig.redeem(other), "b")
    assert rig.stand_in.take_one() is not None
    assert rig.stand_in.take_one() is None, "the second sees the first one's reservation"
    assert rig.store.spend_today()["reserved"] > 0


async def test_turn_limits_per_day_and_in_total(rig: Rig) -> None:
    link_id, invite = rig.link(turns_per_day=2, turns_total=3, price_in=0, price_out=0)
    token = rig.redeem(invite)
    for text in ("a", "b"):
        rig.say(token, text)
        await rig.turn()
    refused = rig.say(token, "c")
    assert refused.status_code == 429 and refused.json()["limit"] == "turns_per_day"
    rig.clock.now += 86400
    rig.say(token, "c")
    await rig.turn()
    refused = rig.say(token, "d")
    assert refused.status_code == 403 and refused.json()["limit"] == "turns_total"
    assert len(rig.model.requests) == 3


async def test_a_free_model_spends_no_dollars_but_still_counts_turns(rig: Rig) -> None:
    link_id, invite = rig.link(price_in=0, price_out=0, usd_limit=0)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    await rig.turn()
    assert len(rig.model.requests) == 1 and rig.store.link(link_id)["turns_used"] == 1 and rig.store.link(link_id)["spent_usd"] == 0


async def test_a_call_costs_what_it_used_and_the_reservation_goes_back(rig: Rig) -> None:
    link_id, invite = rig.link(price_in=1000, price_out=0)
    token = rig.redeem(invite)
    rig.model.usage = (3, 0)
    rig.say(token, "port?")
    await rig.turn()
    link = rig.store.link(link_id)
    assert link["reserved_usd"] == 0 and link["spent_usd"] == pytest.approx(0.003)
    assert rig.store.spend_today() == {"usd": pytest.approx(0.003), "reserved": 0.0}


# -- revocation ------------------------------------------------------------------------------------


async def test_revocation_takes_effect_at_once_even_on_a_reply_not_yet_read(rig: Rig) -> None:
    link_id, invite = rig.link()
    token = rig.redeem(invite)
    rig.say(token, "port?")
    await rig.turn()
    assert rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL).status_code == 200
    answer = rig.read(token)
    assert answer.status_code == 410 and answer.json()["reason"] == "revoked" and "messages" not in answer.json()
    assert rig.say(token, "again").status_code == 410
    assert rig.public.get(f"/a/{invite}").status_code == 410


async def test_a_model_answer_that_arrives_after_revocation_goes_nowhere_and_is_still_paid(rig: Rig) -> None:
    link_id, invite = rig.link()
    token = rig.redeem(invite)
    rig.say(token, "port?")
    taken = rig.stand_in.take_one()
    assert taken is not None
    rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL)
    await rig.stand_in.answer(*taken)
    assert [m["role"] for m in rig.store.transcript(rig.store.conversation_of_link(link_id)["id"])] == ["guest"]
    assert rig.store.link(link_id)["spent_usd"] > 0, "the money went whether or not anyone reads the answer"


async def test_a_poll_held_open_is_released_by_revocation(rig: Rig) -> None:
    import asyncio

    link_id, invite = rig.link()
    token = rig.redeem(invite)
    reading = asyncio.create_task(rig.mailbox.read(token, wait=30))
    await asyncio.sleep(0.02)
    # On this loop, not through the test client's thread: an asyncio event is woken from its own loop.
    conversation = rig.store.conversation_of_link(link_id)
    rig.store.revoke(link_id)
    rig.mailbox.wake(conversation["id"])
    answer = await asyncio.wait_for(reading, 2)
    assert answer is not None and answer.status == 410


def test_a_new_link_keeps_the_settings_and_kills_the_old_invite(rig: Rig) -> None:
    link_id, invite = rig.link(usd_limit=2.5, turns_per_day=7)
    made = rig.control.post(f"/control/links/{link_id}/reissue", headers=CONTROL).json()
    assert made["invite"] != invite and made["link"]["usd_limit"] == 2.5 and made["link"]["turns_per_day"] == 7
    assert rig.public.post(f"/a/{invite}/redeem").status_code == 410
    assert rig.public.post(f"/a/{made['invite']}/redeem").status_code == 201


# -- nothing but the mailbox is on the public port ----------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/auth/me", "/api/sessions", "/api/inbound", "/api/cli/pair/start", "/api/guest/links", "/app/", "/ws/events", "/s/x/", "/pages/x", "/webhooks/github", "/c/x",
    "/control/health", "/control/links", "/control/feed", "/a/../control/health", "/a/control/health", "/a/%2e%2e/api/auth/me", "//control/health",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_public_port_answers_nothing_of_the_app_or_the_control_socket(rig: Rig, path: str, method: str) -> None:
    rig.link()
    answer = rig.public.request(method, path, headers=CONTROL)
    assert answer.status_code == 404, f"{method} {path}"
    assert answer.json().get("detail") == "Nothing here."


def test_a_websocket_is_refused_outright(rig: Rig) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), rig.public.websocket_connect("/a/messages"):
        pass


def test_the_door_imports_nothing_of_the_host() -> None:
    """Read from the source, and proved by running the package with Daedalus off the path altogether."""
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in ("daedalus", "protocore", "fastapi", "aiogram", "aiosqlite"), f"{path.name} imports {name}"
    probe = (
        "import sys; import guestd.public, guestd.control, guestd.standin, guestd.mailbox, guestd.store;"
        "sys.exit(any(m.split('.')[0] in ('daedalus', 'protocore') for m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", probe], cwd=PACKAGE.parent, env={"PYTHONPATH": str(PACKAGE.parent), "PATH": "/usr/bin"}, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


async def test_the_real_process_serves_both_sockets_and_a_conversation_crosses(tmp_path: Path) -> None:
    """``python -m guestd`` as a container runs it: the package alone, and nothing but unix sockets."""
    import asyncio
    import os

    state, run, front, token_dir = tmp_path / "state", tmp_path / "run", tmp_path / "front", tmp_path / "token"
    token_dir.mkdir()
    (token_dir / "control.token").write_text("host-token")
    env = {"PATH": "/usr/bin", "PYTHONPATH": str(PACKAGE.parent), "GUEST_STATE_DIR": str(state), "GUEST_RUN_DIR": str(run), "GUEST_FRONT_DIR": str(front), "GUEST_TOKEN_DIR": str(token_dir)}
    door = subprocess.Popen([sys.executable, "-m", "guestd"], cwd=PACKAGE.parent, env=env)
    try:
        for _ in range(200):
            if (run / "control.sock").exists() and (front / "public.sock").exists():
                break
            await asyncio.sleep(0.02)
        assert os.stat(run / "control.sock").st_mode & 0o077 == 0
        transport = httpx.AsyncHTTPTransport(uds=str(run / "control.sock"))
        async with httpx.AsyncClient(transport=transport, base_url="http://door", headers={"authorization": "Bearer host-token"}) as control:
            await control.put("/control/settings", json={"public_base": "http://door.example.test"})
            fields = {
                "label": "a", "topic": "b", "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "guest_tools": "read", "price_in": 0, "price_out": 0, "approval": False,
                "usd_limit": 1, "turns_per_day": 1, "turns_total": 1, "invite_hours": 1, "conversation_hours": 1, "idle_hours": 1, "retention_days": 1,
            }
            invite = (await control.post("/control/links", json=fields)).json()["invite"]
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(front / "public.sock")), base_url="http://door") as public:
                page = await public.get(f"/a/{invite}", headers={"x-guest-client": "192.0.2.5"})
                assert page.status_code == 200 and f"http://door.example.test/a/{invite}/redeem" in page.text
                assert (await public.get("/control/health", headers={"authorization": "Bearer host-token"})).status_code == 404
                guest = (await public.post(f"/a/{invite}/redeem")).json()["token"]
                await public.post("/a/messages", json={"text": "hello"}, headers={"authorization": f"Bearer {guest}"})
                feed = (await control.get("/control/feed?after=0&wait=2")).json()
                assert [d["text"] for d in feed["deliveries"]] == ["hello"]
                await control.post(f"/control/deliveries/{feed['deliveries'][0]['message_id']}/reply", json={"ok": True, "text": "hi back"})
                read = (await public.get("/a/messages?after=0&wait=2", headers={"authorization": f"Bearer {guest}"})).json()
                assert read["messages"][0]["text"] == "hi back"
    finally:
        door.terminate()
        assert door.wait(timeout=10) == 0, "SIGTERM ends both listeners and the process cleanly"


def test_the_host_answers_for_everything_the_door_cannot_be_told() -> None:
    source = (PACKAGE / "control.py").read_text()
    assert "session_id" not in source, "the door does not know which chat a link belongs to"


def test_the_door_stands_on_no_network_and_the_key_proxy_on_none_of_its() -> None:
    """Read from the compose file: what the door can reach is decided there, and nowhere else."""
    import yaml

    compose = yaml.safe_load((PACKAGE.parents[1] / "deploy" / "compose.yaml").read_text())
    services = compose["services"]
    door, front = services["guest"], services["guest-front"]
    assert door["network_mode"] == "none" and "ports" not in door and "networks" not in door
    assert door["read_only"] is True and door["cap_drop"] == ["ALL"] and front["read_only"] is True and front["cap_drop"] == ["ALL"]
    assert door["user"] != front["user"] and "1001" not in door["user"], "the socket's owner is how the host tells who answers it"
    assert front["networks"] == ["guest"] and front["ports"]
    assert services["keyproxy"]["networks"] == ["keys"] and "guest" not in services["daedalus"]["networks"]
    assert not any(str(volume).startswith(("daedalus-state", "..", "/")) for volume in door["volumes"])
    assert "guest-token:/run/daedalus-guest-token:ro" in door["volumes"], "the token is the host's: the door only reads it"
    assert "guest-front:/run/daedalus-guest-front:ro" in front["volumes"]
    assert "guest-model:/run/keyproxy-guest:ro" in door["volumes"]


def test_the_public_listener_is_not_told_to_believe_anyones_forwarded_headers(rig: Rig) -> None:
    from guestd.__main__ import public_config

    config = public_config(PublicDoor(rig.mailbox))
    assert config.proxy_headers is False and config.access_log is False and config.forwarded_allow_ips == ""


# -- the control socket -------------------------------------------------------------------------------


def test_the_control_socket_wants_its_token(rig: Rig) -> None:
    assert rig.control.get("/control/health").status_code == 401
    assert rig.control.get("/control/links", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert rig.control.get("/control/health", headers=CONTROL).json()["usd_per_day"] == 5.0


@pytest.mark.parametrize(("change", "word"), [({"mode": "other"}, "mode"), ({"usd_limit": -1}, "usd_limit"), ({"turns_per_day": 0}, "turns_per_day"), ({"briefing": " "}, "briefing"), ({"usd_limit": True}, "usd_limit")])
def test_a_link_with_wrong_settings_is_refused(change: dict[str, Any], word: str) -> None:
    fields = {
        "label": "a", "topic": "b", "mode": "stand_in", "briefing": "c", "model_path": "d", "model_name": "e", "guest_tools": "none", "price_in": 1, "price_out": 1,
        "usd_limit": 1, "turns_per_day": 1, "turns_total": 1, "invite_hours": 1, "conversation_hours": 1, "idle_hours": 1, "retention_days": 1, **change,
    }
    with pytest.raises(ValueError, match=word):
        link_fields(fields)


def direct_link(rig: Rig, **overrides: Any) -> tuple[str, str]:
    return rig.link(mode="direct", briefing="", model_path="", model_name="", guest_tools="read", price_in=0, price_out=0, **overrides)


def test_a_direct_guests_message_is_handed_out_once_and_only_its_reply_comes_back(rig: Rig) -> None:
    link_id, invite = direct_link(rig)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    feed = rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()
    assert [d["text"] for d in feed["deliveries"]] == ["port?"]
    assert set(feed["deliveries"][0]) == {"message_id", "link_id", "conversation_id", "text", "allowance_usd"}
    assert feed["deliveries"][0]["allowance_usd"] == 0.25
    message = feed["deliveries"][0]["message_id"]
    assert rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"] == feed["deliveries"], "offered again until it is acknowledged"
    assert rig.control.post(f"/control/deliveries/{message}/ack", headers=CONTROL).status_code == 200
    assert rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"] == []
    assert rig.control.post(f"/control/deliveries/{message}/reply", json={"ok": True, "text": "8080", "cost_usd": 0.25}, headers=CONTROL).status_code == 200
    assert rig.read(token).json()["messages"][0]["text"] == "8080"
    assert rig.store.link(link_id)["spent_usd"] == 0.25
    assert rig.control.post(f"/control/deliveries/{message}/reply", json={"ok": True, "text": "again"}, headers=CONTROL).status_code == 409


def test_a_direct_guest_waits_in_line_one_turn_at_a_time(rig: Rig) -> None:
    _, invite = direct_link(rig)
    token = rig.redeem(invite)
    rig.say(token, "one")
    rig.say(token, "two")
    first = rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"]
    assert [d["text"] for d in first] == ["one"]
    rig.control.post(f"/control/deliveries/{first[0]['message_id']}/ack", headers=CONTROL)
    rig.control.post(f"/control/deliveries/{first[0]['message_id']}/reply", json={"ok": True, "text": "1"}, headers=CONTROL)
    assert [d["text"] for d in rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"]] == ["two"]


def test_a_direct_link_out_of_money_is_not_handed_to_the_host(rig: Rig) -> None:
    link_id, invite = direct_link(rig, usd_limit=0.5)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    rig.store.conn.execute("UPDATE links SET spent_usd = 0.5 WHERE id = ?", (link_id,))
    assert rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"] == []
    assert rig.read(token).json()["messages"][0]["from"] == "door"


def test_the_allowance_of_a_direct_turn_is_what_is_left_of_both_limits(rig: Rig) -> None:
    link_id, invite = direct_link(rig, usd_limit=1.0)
    rig.control.put("/control/settings", json={"usd_per_day": 0.1}, headers=CONTROL)
    rig.say(rig.redeem(invite), "port?")
    assert rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"][0]["allowance_usd"] == 0.1


def test_a_restart_of_the_host_fails_what_it_was_doing_and_does_not_run_it_twice(rig: Rig) -> None:
    link_id, invite = direct_link(rig)
    token = rig.redeem(invite)
    rig.say(token, "delete the file")
    rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL)
    assert rig.control.post("/control/deliveries/reset", headers=CONTROL).json()["reset"] == 1
    assert rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"] == []
    assert rig.read(token).json()["messages"][0]["from"] == "door"
    link = rig.store.link(link_id)
    assert link["turns_used"] == 1 and link["reserved_usd"] == 0, "it may have run: it stays counted, at what was put aside"


def test_a_turn_stopped_by_revocation_still_books_what_it_spent(rig: Rig) -> None:
    link_id, invite = direct_link(rig)
    token = rig.redeem(invite)
    rig.say(token, "port?")
    message = rig.control.get("/control/feed?after=0&wait=0", headers=CONTROL).json()["deliveries"][0]["message_id"]
    rig.control.post(f"/control/links/{link_id}/revoke", headers=CONTROL)
    assert rig.control.post(f"/control/deliveries/{message}/reply", json={"ok": False, "cost_usd": 0.1}, headers=CONTROL).status_code == 200
    assert rig.store.link(link_id)["spent_usd"] == 0.1
    assert rig.read(token).status_code == 410


def test_a_fetch_only_agent_is_told_what_it_lacks_and_a_users_fetcher_may_read_the_door() -> None:
    assert "only read web pages" in PAGE
    text = (Path(guestd.__file__).parent / "public.py").read_text(encoding="utf-8")
    assert "User-agent: Claude-User" in text and "Allow: /a/" in text
