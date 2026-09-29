"""The evaluation's tasks: what the agent is asked, where it starts, and how the result is judged.

A task is judged by what the fixture's server saw (a form's POST, a page's own record of a click, a
file saved into the workspace) whenever there is something to see; only a question whose whole point
is reading is judged by the words of the answer. ``covers`` names the weakness of the browser tools a
failure points at, in the words of the notes that motivated the set, so a failing row says what to
fix and not only that something broke.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tests.browser_eval.site import CSV


@dataclass(frozen=True)
class Outcome:
    """Everything a checker may look at after the agent's last word."""

    answer: str
    submissions: list[dict[str, str]]
    events: list[dict[str, Any]]
    saved: dict[str, bytes]
    """Files the agent put in its workspace, by name."""


Check = Callable[[Outcome], tuple[bool, str]]


@dataclass(frozen=True)
class Task:
    id: str
    page: str
    """The path on the main origin the agent is told to open."""
    instruction: str
    check: Check
    covers: tuple[str, ...] = field(default_factory=tuple)


def said(*patterns: str, never: tuple[str, ...] = ()) -> Check:
    """The answer carries every pattern and none of ``never`` (case and spacing forgiven)."""

    def check(out: Outcome) -> tuple[bool, str]:
        text = re.sub(r"\s+", " ", out.answer)
        missing = [p for p in patterns if not re.search(p, text, re.IGNORECASE)]
        wrong = [p for p in never if re.search(p, text, re.IGNORECASE)]
        if missing:
            return False, f"the answer lacks {missing}"
        if wrong:
            return False, f"the answer also names {wrong}"
        return True, "answered"

    return check


def posted(**wanted: str | None) -> Check:
    """The last POST carries each field with that value; ``None`` means the field must be absent
    (an unchecked box), which is how a form says "off"."""

    def check(out: Outcome) -> tuple[bool, str]:
        if not out.submissions:
            return False, "the form was never sent"
        last = out.submissions[-1]
        bad = []
        for key, value in wanted.items():
            got = last.get(key)
            if value is None:
                if got is not None:
                    bad.append(f"{key}={got!r} (should be off)")
            elif (got or "").strip().lower() != value.lower():
                bad.append(f"{key}={got!r} (wanted {value!r})")
        return (not bad), ("sent as asked" if not bad else "sent with " + "; ".join(bad))

    return check


def recorded(name: str, test: Callable[[Any], bool] = lambda data: True, *, only: bool = False) -> Check:
    """The page recorded ``name`` with data that passes ``test``; ``only`` means every record of that
    name must pass, so acting on the wrong row as well is a failure."""

    def check(out: Outcome) -> tuple[bool, str]:
        hits = [e for e in out.events if e["name"] == name]
        if not hits:
            return False, f"the page never saw {name}"
        good = [e for e in hits if test(e["data"])]
        if not good:
            return False, f"{name} happened with {hits[-1]['data']!r}"
        if only and len(good) != len(hits):
            return False, f"{name} also happened with {[e['data'] for e in hits if not test(e['data'])]!r}"
        return True, f"{name} seen"

    return check


def both(*checks: Check) -> Check:
    def check(out: Outcome) -> tuple[bool, str]:
        for one in checks:
            ok, why = one(out)
            if not ok:
                return False, why
        return True, "all seen"

    return check


def saved_statement(out: Outcome) -> tuple[bool, str]:
    for name, data in out.saved.items():
        if data.decode("utf-8", "replace") == CSV:
            return True, f"saved as {name}"
    return False, "the statement is not in the workspace" if not out.saved else f"saved {sorted(out.saved)} but none is the statement"


def bin_label(n: int) -> str:
    """The label ``scroll-container.html`` computes for bin ``n``."""
    return "ABCDEFGHJK"[(n * 7) % 10] + "-" + str((n * 37) % 97).zfill(2) + "-" + str((n * 13) % 9 + 1)


def seeds_state(out: Outcome) -> tuple[bool, str]:
    """Only the seed-bank article ends saved: a toggle clicked twice, or on the wrong article, fails."""
    saved: dict[str, bool] = {}
    for event in out.events:
        if event["name"] == "save" and isinstance(event["data"], dict):
            saved[str(event["data"].get("id"))] = bool(event["data"].get("saved"))
    kept = sorted(k for k, v in saved.items() if v)
    return (kept == ["seeds"]), (f"saved: {kept}" if kept else "nothing ended saved")


TASKS: list[Task] = [
    Task(
        "form-contact", "form-contact",
        "Send a message through the contact form: name Maria Lindqvist, email maria.l@example.org, order number NW-4471, "
        "topic Returns, message \"The drill arrived without its battery.\", and ask for a copy to be sent to her.",
        posted(name="Maria Lindqvist", email="maria.l@example.org", order="NW-4471", topic="Returns", message="The drill arrived without its battery.", copy="yes"),
        ("forms with several fields", "5 multi-step act"),
    ),
    Task(
        "form-shipping", "form-shipping",
        "Fill in the shipping address and continue to payment: Tomas Berg, Rua das Flores 12, 1200-195 Lisbon, Portugal. "
        "Choose express delivery and mark it as a gift.",
        posted(first="Tomas", last="Berg", street="Rua das Flores 12", postcode="1200-195", city="Lisbon", country="Portugal", delivery="express", gift="yes"),
        ("forms with several fields", "radio and select", "5 multi-step act"),
    ),
    Task(
        "div-button", "div-button",
        "Add the Trail Runner 3 in size 43 to the bag.",
        recorded("add", lambda d: isinstance(d, dict) and d.get("size") == "43", only=True),
        ("2 clickable detection: script listeners, cursor:pointer",),
    ),
    Task(
        "icon-toggle", "icon-toggle",
        "Save the article about the seed bank for later (and only that one).",
        seeds_state,
        ("2 clickable detection: script listeners without cursor or role",),
    ),
    Task(
        "cookie-banner", "cookie-banner",
        "Subscribe to the weekly newsletter with the email reader@example.net.",
        recorded("subscribe", lambda d: isinstance(d, dict) and d.get("email") == "reader@example.net"),
        ("3 [covered by …] marking", "12 cookie banners: prefer reject"),
    ),
    Task(
        "modal-overlay", "modal-overlay",
        "Open the full annual report on this page and tell me by how much assets under management rose.",
        both(recorded("get"), said(r"6[.,]1\s*%")),
        ("3 [covered by …] marking", "modal overlay"),
    ),
    Task(
        "long-page", "long-page",
        "Tell the organisers I am coming to the October meetup with 3 guests in total.",
        recorded("rsvp", lambda d: isinstance(d, dict) and d.get("guests") == "3"),
        ("3 viewport and scroll hints, target far below the fold",),
    ),
    Task(
        "scroll-container", "scroll-container",
        "What is the location label of Bin 173?",
        said(re.escape(bin_label(173))),
        ("6 scroll inside a container by ref", "3 viewport hints"),
    ),
    Task(
        "virtual-list", "virtual-list",
        "Mark order #4372 as shipped (only that order).",
        recorded("shipped", lambda d: isinstance(d, dict) and d.get("order") == 4372, only=True),
        ("6 scroll inside a container by ref", "virtualised list"),
    ),
    Task(
        "iframe-read", "iframe-read",
        "What is the tracking number of my parcel?",
        said(r"PW-58213-QX"),
        ("4 read cross-origin iframes",),
    ),
    Task(
        "iframe-click", "iframe-click",
        "Save the insurer's quote, choosing to pay yearly.",
        recorded("save", lambda d: isinstance(d, dict) and d.get("term") == "yearly"),
        ("4 read and act in cross-origin iframes",),
    ),
    Task(
        "native-select", "native-select",
        "Set my country of residence to Portugal and the currency to CHF, then save.",
        posted(country="Portugal", currency="CHF"),
        ("6 select options inline",),
    ),
    Task(
        "combobox", "combobox",
        "Set the booking page's time zone to Lisbon and save it.",
        recorded("save", lambda d: isinstance(d, dict) and d.get("zone") == "Europe/Lisbon"),
        ("custom combobox", "6 options inline"),
    ),
    Task(
        "pagination", "shop",
        "How much does the Walnut desk organizer cost in this shop?",
        said(r"47[.,]35"),
        ("pagination", "6 find text"),
    ),
    Task(
        "load-more", "load-more",
        "Who wrote the field note called \"Kestrel migration notes\"?",
        said(r"Ines Varga"),
        ("load more", "2 clickable detection (the button is a div)"),
    ),
    Task(
        "new-tab", "new-tab",
        "Which version of Quillpad's terms of service is in effect?",
        said(r"4\.2\.1"),
        ("tabs opened by target=_blank", "5 stop on new tab"),
    ),
    Task(
        "download", "download",
        "Download the August 2026 statement into your workspace.",
        saved_statement,
        ("download",),
    ),
    Task(
        "table", "table",
        "What is the combined salary of the people in the Berlin office who earn more than €70,000?",
        # A sum, not a list of names: a right answer that explains whom it left out names them too,
        # and only the right set of people adds up to this figure (Hana Suzuki earns exactly 70,000).
        said(r"243[,. ]?600"),
        ("7 structured extraction", "table"),
    ),
    Task(
        "slow", "slow",
        "What is the status of order PW-77120 and when will it arrive?",
        said(r"out for delivery", r"14[:.]?00"),
        ("3 'page looks empty' hint", "slow page with a skeleton"),
    ),
    Task(
        "confirm-dialog", "confirm-dialog",
        "Reset the flight search filters to their defaults.",
        recorded("reset"),
        ("dialog (confirm)",),
    ),
    Task(
        "search-box", "search-box",
        "Find the Arctic Glide thermal paste on this site and tell me its SKU.",
        said(r"TP-4410-AG"),
        ("search box", "5 type and submit"),
    ),
    Task(
        "long-doc", "long-doc",
        "According to this reference, what is the default value of retry_backoff_ms?",
        said(r"\b275\b"),
        ("6 BrowserText find/grep", "3 truncation of a long page"),
    ),
    Task(
        "faq", "faq",
        "Until when can an unused gift card be refunded?",
        said(r"45 days"),
        ("6 find text", "collapsed <details>"),
    ),
    Task(
        "hover-menu", "hover-menu",
        "Turn off the weekly digest email in my notification settings, keep the other settings as they are, and save.",
        posted(sent="1", paid="on", overdue="on", digest=None, news=None),
        ("hover menu", "navigation"),
    ),
    Task(
        "label-checkbox", "label-checkbox",
        "Turn on the dark theme and week numbers, leave the other preferences as they are, and apply.",
        posted(sent="1", dark="on", numbers="on", weekends="on", compact=None),
        ("2 labels wrapping hidden inputs",),
    ),
    Task(
        "tabs-widget", "tabs-widget",
        "What is the packed weight of the Aerolite 2 tent?",
        said(r"1[.,]37\s*kg"),
        ("2 clickable detection: tabs made of divs",),
    ),
    Task(
        "shadow-dom", "shadow-dom",
        "Apply the voucher AUTUMN10 to my basket.",
        recorded("apply", lambda d: isinstance(d, dict) and d.get("voucher") == "AUTUMN10"),
        ("shadow DOM",),
    ),
    Task(
        "wizard", "wizard",
        "Start a trial on the Team plan, with the workspace name \"Harbor Analytics\" and a team size of 6–20 people.",
        posted(plan="team", workspace="Harbor Analytics", size="6–20"),
        ("5 multi-step act", "multi-step wizard"),
    ),
]

BY_ID = {t.id: t for t in TASKS}
