"""The routing decision as a table: every rule of ``decide`` against the notification, the
preferences, presence, Telegram and the clock, with no host behind it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from daedalus.config import NOTIFICATION_CATEGORIES, NotificationsConfig, NotifyCells
from daedalus.extensions.notifications import CATEGORIES
from daedalus.host.notify_routing import Candidate, Channels, RateLimiter, TelegramFacts, decide, in_quiet_hours, muted
from daedalus.host.presence import PresenceSnapshot

NOON = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
AWAY = PresenceSnapshot(present=False, desktop=True)
PRESENT = PresenceSnapshot(present=True, sessions=frozenset({"other"}), desktop=True)
ATTENDING = PresenceSnapshot(present=True, sessions=frozenset({"s1"}), desktop=True)
BOT = TelegramFacts(front=True)
DEVICE = Channels(push=True)


def route(c: Candidate, prefs: NotificationsConfig | None = None, presence: PresenceSnapshot = AWAY, telegram: TelegramFacts = BOT,
          channels: Channels = DEVICE, now: datetime = NOON, **kw):  # type: ignore[no-untyped-def]
    return decide(c, prefs or NotificationsConfig(), presence, telegram, channels, now, **kw)


def test_the_category_lists_are_one_list() -> None:
    assert CATEGORIES == NOTIFICATION_CATEGORIES
    assert set(NotificationsConfig().matrix) == set(CATEGORIES)


@pytest.mark.parametrize(
    ("category", "push", "desktop", "telegram"),
    [
        ("run_finished", True, True, "off"),
        ("question", False, False, "session"),  # Telegram carries it, so nothing else buzzes
        ("permission", False, False, "session"),
        ("run_failed", False, False, "session"),
        ("staff_turn", False, False, "off"),
        ("staff_review", True, True, "off"),
        ("orchestrator_report", False, False, "session"),
        ("agent_notify", True, True, "off"),  # Telegram only at urgent
        ("reminder", False, False, "session"),
        ("spend", False, False, "session"),
        ("system", True, True, "off"),
    ],
)
def test_the_default_matrix_for_an_operator_who_is_away(category: str, push: bool, desktop: bool, telegram: str) -> None:
    d = route(Candidate(category, "normal", session_id="s1"))
    assert (d.record, d.toast, d.push, d.desktop, d.telegram) == (True, True, push, desktop, telegram)


def test_an_urgent_cell_carries_only_urgent_notifications() -> None:
    assert route(Candidate("agent_notify", "normal")).telegram == "off"
    assert route(Candidate("agent_notify", "urgent")).telegram == "general"


def test_a_cell_switched_off_and_the_app_cell_off_records_as_seen() -> None:
    prefs = NotificationsConfig(matrix={"run_finished": NotifyCells(in_app="off", push="off", desktop="urgent")})
    d = route(Candidate("run_finished", "normal", session_id="s1"), prefs)
    assert (d.seen_now, d.toast, d.push, d.desktop) == (True, False, False, False)
    assert d.reasons["push"] == "skipped: off" and d.reasons["in_app"] == "seen: off"
    assert route(Candidate("run_finished", "urgent", session_id="s1"), prefs).desktop is True


def test_a_quiet_notification_is_a_record_only() -> None:
    d = route(Candidate("system", "quiet"))
    assert (d.record, d.seen_now, d.toast, d.push, d.desktop, d.telegram) == (True, False, False, False, False, "off")
    assert route(Candidate("reminder", "quiet", handled=frozenset({"telegram"}))).telegram == "handled"


def test_attending_the_session() -> None:
    answer = route(Candidate("run_finished", "normal", session_id="s1"), presence=ATTENDING)
    assert answer.record is False  # the answer is on the screen already
    failure = route(Candidate("run_failed", "normal", session_id="s1"), presence=ATTENDING)
    assert (failure.record, failure.seen_now, failure.toast, failure.push, failure.telegram) == (True, True, False, False, "off")
    ask = route(Candidate("question", "normal", actionable=True, session_id="s1", handled=frozenset({"telegram"})), presence=ATTENDING)
    assert (ask.seen_now, ask.toast, ask.push, ask.desktop, ask.telegram) == (False, True, False, False, "handled")


def test_attending_a_terminal_or_a_project() -> None:
    snapshot = PresenceSnapshot(present=True, terminals=frozenset({"t1"}), projects=frozenset({"p1"}))
    assert route(Candidate("agent_notify", "normal", terminal_id="t1"), presence=snapshot).seen_now is True
    assert route(Candidate("staff_review", "normal", project_id="p1"), presence=snapshot).seen_now is True
    # A project open on the desk is not the operator reading one staff member's session.
    assert route(Candidate("question", "normal", actionable=True, session_id="s9", project_id="p1"), presence=snapshot).seen_now is False


def test_present_elsewhere_is_a_toast_without_push_or_desktop() -> None:
    d = route(Candidate("run_finished", "normal", session_id="s1"), presence=PRESENT)
    assert (d.toast, d.push, d.desktop) == (True, False, False)
    assert d.reasons["push"] == "skipped: present"


def test_no_device_and_no_launcher() -> None:
    d = route(Candidate("run_finished", "normal", session_id="s1"), presence=PresenceSnapshot(present=False), channels=Channels())
    assert (d.push, d.desktop) == (False, False)
    assert (d.reasons["push"], d.reasons["desktop"]) == ("skipped: no device", "skipped: no launcher")


def test_a_muted_project_speaks_only_at_urgent() -> None:
    later = (NOON + timedelta(hours=1)).isoformat()
    earlier = (NOON - timedelta(hours=1)).isoformat()
    prefs = NotificationsConfig(muted_projects={"p1": later, "p2": "", "p3": earlier})
    quiet = route(Candidate("run_failed", "normal", session_id="s1", project_id="p1"), prefs)
    assert (quiet.record, quiet.toast, quiet.push, quiet.telegram) == (True, False, False, "off")
    assert route(Candidate("run_failed", "normal", project_id="p2"), prefs).toast is False
    assert route(Candidate("run_failed", "normal", project_id="p3"), prefs).toast is True  # the mute ended
    loud = route(Candidate("permission", "urgent", actionable=True, session_id="s1", project_id="p1"), prefs, telegram=TelegramFacts())
    assert (loud.toast, loud.push) == (True, True)
    assert muted(prefs, "p2", NOON) and not muted(prefs, "p3", NOON) and not muted(prefs, None, NOON)


@pytest.mark.parametrize(
    ("spec", "zone", "moment", "inside"),
    [
        ("22:00-07:00", "UTC", datetime(2026, 9, 24, 23, 30, tzinfo=UTC), True),
        ("22:00-07:00", "UTC", datetime(2026, 9, 24, 6, 59, tzinfo=UTC), True),
        ("22:00-07:00", "UTC", datetime(2026, 9, 24, 7, 0, tzinfo=UTC), False),
        ("22:00-07:00", "UTC", NOON, False),
        ("13:00-15:00", "UTC", NOON, False),
        ("13:00-15:00", "Europe/Berlin", NOON, True),  # noon in UTC is two in the afternoon in Berlin in September
        ("", "UTC", NOON, False),
        ("09:00-09:00", "UTC", datetime(2026, 9, 24, 9, 0, tzinfo=UTC), False),
        ("22:00-07:00", "Nowhere/Invalid", datetime(2026, 9, 24, 23, 0, tzinfo=UTC), True),  # an unknown zone reads as UTC
        # Across the autumn change in Berlin (25 October 2026, 03:00 CEST → 02:00 CET): 06:30 local is
        # 04:30 UTC before the change and 05:30 UTC after it; the window means the wall clock both times.
        ("23:00-07:00", "Europe/Berlin", datetime(2026, 10, 24, 4, 30, tzinfo=UTC), True),
        ("23:00-07:00", "Europe/Berlin", datetime(2026, 10, 26, 5, 30, tzinfo=UTC), True),
        ("23:00-07:00", "Europe/Berlin", datetime(2026, 10, 26, 6, 30, tzinfo=UTC), False),
        ("23:00-07:00", "Europe/Berlin", datetime(2026, 10, 24, 5, 30, tzinfo=UTC), False),
    ],
)
def test_quiet_hours(spec: str, zone: str, moment: datetime, inside: bool) -> None:
    assert in_quiet_hours(spec, zone, moment) is inside


def test_quiet_hours_hold_back_everything_below_urgent() -> None:
    prefs = NotificationsConfig(quiet_hours="11:00-13:00")
    normal = route(Candidate("run_failed", "normal"), prefs, telegram=BOT)
    assert (normal.toast, normal.push, normal.desktop, normal.telegram) == (True, False, False, "off")
    assert normal.reasons["push"] == "skipped: quiet hours" and normal.reasons["telegram"] == "skipped: quiet hours"
    urgent = route(Candidate("permission", "urgent", actionable=True, session_id="s1"), prefs, telegram=TelegramFacts())
    assert (urgent.push, urgent.desktop) == (True, True)


def test_the_quiet_hours_setting_is_checked() -> None:
    with pytest.raises(ValueError):
        NotificationsConfig(quiet_hours="late")
    with pytest.raises(ValueError):
        NotificationsConfig(quiet_hours="25:00-07:00")
    with pytest.raises(ValueError):
        NotificationsConfig(matrix={"nonsense": NotifyCells()})
    assert NotificationsConfig(matrix={"system": NotifyCells(push="off")}).cells("question").telegram == "on"  # the rest keep their defaults


@pytest.mark.parametrize(
    ("candidate", "facts", "expected"),
    [
        (Candidate("question", "normal", session_id="s1"), TelegramFacts(front=True), "session"),
        (Candidate("question", "normal", session_id="s1", handled=frozenset({"telegram"})), TelegramFacts(front=True), "handled"),
        # A site session stays off Telegram, urgent or not: push and the app carry it.
        (Candidate("permission", "urgent", actionable=True, session_id="s1"), TelegramFacts(front=True, detached=True), "detached"),
        # Telegram is optional: with no bot there is nothing to decide.
        (Candidate("question", "normal", session_id="s1"), TelegramFacts(front=False), "none"),
        (Candidate("spend", "normal"), TelegramFacts(front=True), "general"),
        (Candidate("spend", "normal"), TelegramFacts(front=False), "none"),
        # A project's topic is the orchestrator's: the router never posts into it.
        (Candidate("question", "normal", actionable=True, session_id="s1", project_id="p1", staff_id="st"), TelegramFacts(front=True, orchestrated=True), "orchestrator"),
        (Candidate("orchestrator_report", "normal", project_id="p1"), TelegramFacts(front=True, orchestrated=True, from_orchestrator=True), "orchestrator"),
        (Candidate("orchestrator_report", "normal", project_id="p1", handled=frozenset({"telegram"})), TelegramFacts(front=True, orchestrated=True, from_orchestrator=True), "handled"),
        (Candidate("reminder", "normal", project_id="p1"), TelegramFacts(front=True), "project"),
    ],
)
def test_the_telegram_routes(candidate: Candidate, facts: TelegramFacts, expected: str) -> None:
    assert route(candidate, telegram=facts).telegram == expected


def test_what_telegram_delivered_is_not_pushed_as_well_unless_the_operator_wants_both() -> None:
    bound = Candidate("question", "normal", actionable=True, session_id="s1", handled=frozenset({"telegram"}))
    covered = route(bound)
    assert (covered.push, covered.desktop) == (False, False) and covered.reasons["push"] == "skipped: sent to Telegram"
    both = route(bound, NotificationsConfig(telegram_covers_push=False))
    assert (both.push, both.desktop) == (True, True)
    detached = route(Candidate("question", "normal", actionable=True, session_id="s1"), telegram=TelegramFacts(front=True, detached=True))
    assert detached.push is True  # it did not go to Telegram, so it buzzes


def test_a_repeat_sounds_again_only_when_its_level_rose() -> None:
    again = route(Candidate("system", "normal", merged=True), telegram=TelegramFacts())
    assert (again.toast, again.push, again.desktop) == (True, False, False) and again.reasons["push"] == "skipped: repeat"
    rose = route(Candidate("system", "urgent", merged=True, level_rose=True), telegram=TelegramFacts())
    assert rose.push is True
    assert route(Candidate("spend", "normal", merged=True)).telegram == "off"  # no second line in General either


def test_a_staff_request_in_an_orchestrated_project_is_held_for_the_orchestrator() -> None:
    staff = Candidate("permission", "urgent", actionable=True, session_id="s1", project_id="p1", staff_id="st")
    facts = TelegramFacts(front=True, orchestrated=True)
    held = route(staff, telegram=facts, hold_seconds=60)
    assert held.hold_until == NOON + timedelta(seconds=60)
    assert route(staff, telegram=facts, hold_seconds=0).hold_until is None  # autonomy "ask": straight to the operator
    assert route(staff, telegram=TelegramFacts(front=True), hold_seconds=60).hold_until is None  # not orchestrated
    own = TelegramFacts(front=True, orchestrated=True, from_orchestrator=True)
    assert route(staff, telegram=own, hold_seconds=60).hold_until is None  # the orchestrator's own question


def test_the_rate_limit_swallows_a_burst_and_counts_it_into_the_next_sound() -> None:
    limiter = RateLimiter(per_scope=2, window=timedelta(minutes=10), per_hour=100)
    c = Candidate("run_finished", "normal", session_id="s1")
    outcomes = [route(c, limiter=limiter, now=NOON + timedelta(seconds=n)) for n in range(5)]
    assert [d.push for d in outcomes] == [True, True, False, False, False]
    assert outcomes[2].reasons["push"] == "skipped: rate limit"
    after = route(c, limiter=limiter, now=NOON + timedelta(minutes=11))
    assert (after.push, after.more) == (True, 3)
    assert route(Candidate("run_finished", "normal", session_id="s2"), limiter=limiter).push is True  # another session has its own budget


def test_the_hourly_ceiling_holds_across_sessions() -> None:
    limiter = RateLimiter(per_scope=10, window=timedelta(minutes=10), per_hour=3)
    pushed = [route(Candidate("run_finished", "normal", session_id=f"s{n}"), limiter=limiter).push for n in range(5)]
    assert pushed == [True, True, True, False, False]


def test_the_test_button_reaches_every_channel_there_is() -> None:
    d = route(Candidate("system", "quiet", force=True), NotificationsConfig(quiet_hours="00:00-23:59"), presence=PRESENT)
    assert (d.toast, d.push, d.desktop, d.telegram) == (True, True, True, "general")
    none = route(Candidate("system", "normal", force=True), presence=PresenceSnapshot(present=False), telegram=TelegramFacts(), channels=Channels())
    assert (none.push, none.desktop, none.telegram) == (False, False, "none")


def test_a_conversation_followed_in_chrome_sounds_there_once_instead_of_push_and_desktop() -> None:
    d = route(Candidate("run_finished", "normal", session_id="s1", chrome=True))
    assert (d.chrome, d.push, d.desktop, d.toast) == (True, False, False, True)
    assert d.reasons["chrome"] == "sent" and d.reasons["push"] == d.reasons["desktop"] == "skipped: shown in Chrome"
    # Without a launcher or a device it still sounds: the extension is the way out then.
    alone = route(Candidate("run_finished", "normal", session_id="s1", chrome=True), presence=PresenceSnapshot(present=False), channels=Channels())
    assert alone.chrome is True
    # Any other conversation goes as before, and nothing is shown in Chrome.
    other = route(Candidate("run_finished", "normal", session_id="s1"))
    assert (other.chrome, other.push, other.desktop) == (False, True, True) and "chrome" not in other.reasons


def test_chrome_keeps_the_rules_of_the_push_it_replaces() -> None:
    def sounds(**kw) -> bool:  # type: ignore[no-untyped-def]
        candidate = kw.pop("candidate", Candidate("run_finished", "normal", session_id="s1", chrome=True))
        return route(candidate, **kw).chrome

    assert not sounds(presence=PRESENT)  # a window in front of the operator says it already
    assert not sounds(presence=ATTENDING)
    assert not sounds(prefs=NotificationsConfig(quiet_hours="11:00-13:00"))
    assert not sounds(prefs=NotificationsConfig(matrix={"run_finished": NotifyCells(push="off", desktop="off")}))
    assert not sounds(candidate=Candidate("run_finished", "normal", session_id="s1", chrome=True, merged=True))
    assert not sounds(candidate=Candidate("question", "normal", session_id="s1", chrome=True), prefs=NotificationsConfig(telegram_covers_push=True))
    limiter = RateLimiter(1, timedelta(minutes=10), 100)
    assert sounds(limiter=limiter) and not sounds(limiter=limiter)
    assert route(Candidate("run_finished", "normal", session_id="s1", chrome=True), prefs=NotificationsConfig(quiet_hours="11:00-13:00")).reasons["chrome"] == "skipped: quiet hours"
