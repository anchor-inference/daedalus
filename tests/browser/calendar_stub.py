"""The calendar and planner API, answered the way the host answers it, and kept between requests.

An invented week of somebody's work and life, laid around the day the harness runs on: three local
calendars and two connected ones, a weekly meeting, a daily run, a conference across three days, three
events that overlap at lunch, a holiday from a read-only subscription, and tasks due today, overdue,
upcoming, undated and one blocked out on today's afternoon. One connected account has failed its last
sync and holds a conflict, so the screen has something to say about both.

Recurring events are expanded into occurrences here as the host does, on the wall clock of the
settings' time zone, so a weekly meeting stays at ten across a daylight-saving change. A move of
"this event" is kept as an override of that occurrence, a move of "all events" changes the series.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

ZONE = "Europe/Amsterdam"
BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def iso(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class CalendarStub:
    """``answer(method, path, query, body)`` for every ``/api/calendar*`` and ``/api/planner*`` route."""

    def __init__(self, *, zone: str = ZONE, today: date | None = None, lang: str = "en") -> None:
        self.zone = ZoneInfo(zone)
        self.today = today or datetime.now(self.zone).date()
        self.lang = lang
        self.writes: list[tuple[str, str, dict]] = []
        """Every write the screen sent, ``(method, path, body)``, for a check to read back."""
        ru = lang == "ru"
        self.settings: dict = {"week_start": 1, "work_start": "09:00", "work_end": "18:00", "default_view": "week", "default_duration": 60, "default_reminders": [10], "timezone": zone, "show_weekends": True}

        def sync(status: str, error: str = "", minutes: int = 6) -> dict:
            at = None if status == "never" else iso(datetime.now(self.zone) - timedelta(minutes=minutes))
            return {"last_sync_at": at, "error": error, "status": status}

        self.calendars: list[dict] = [
            {"id": "cal-personal", "name": "Личный" if ru else "Personal", "color": "#4f8ff7", "kind": "local", "account_id": None, "visible": True, "writable": True, "position": 0, "default_reminders": [10], "sync": None},
            {"id": "cal-work", "name": "Работа" if ru else "Work", "color": "#14b8a6", "kind": "local", "account_id": None, "visible": True, "writable": True, "position": 1, "default_reminders": [10], "sync": None},
            {"id": "cal-family", "name": "Семья" if ru else "Family", "color": "#e3608f", "kind": "local", "account_id": None, "visible": True, "writable": True, "position": 2, "default_reminders": [60], "sync": None},
            {"id": "cal-team", "name": "Команда (Google)" if ru else "Team (Google)", "color": "#f2782f", "kind": "google", "account_id": "acc-google", "visible": True, "writable": True, "position": 3, "default_reminders": [10], "sync": sync("ok")},
            {"id": "cal-yandex", "name": "Яндекс" if ru else "Yandex", "color": "#9b6ef3", "kind": "caldav", "account_id": "acc-yandex", "visible": True, "writable": True, "position": 4, "default_reminders": [], "sync": sync("error", "the server refused the change: 412 Precondition Failed", 95)},
            {"id": "cal-holidays", "name": "Праздники" if ru else "Public holidays", "color": "#34a853", "kind": "ics", "account_id": "acc-ics", "visible": True, "writable": False, "position": 5, "default_reminders": [], "sync": sync("ok", minutes=240)},
        ]
        self.accounts: list[dict] = [
            {"id": "acc-google", "provider": "google", "preset": None, "name": "Команда (Google)" if ru else "Team (Google)", "remote_calendar_id": "primary", "calendar_id": "cal-team", "last_sync_at": self.calendars[3]["sync"]["last_sync_at"], "sync_error": "", "status": "ok", "conflicts": []},
            {"id": "acc-yandex", "provider": "caldav", "preset": "yandex", "name": "Яндекс" if ru else "Yandex", "remote_calendar_id": "", "server_url": "https://caldav.yandex.ru", "calendar_id": "cal-yandex", "last_sync_at": self.calendars[4]["sync"]["last_sync_at"], "sync_error": "the server refused the change: 412 Precondition Failed", "status": "error",
             "conflicts": [{"event_id": "ev-planning", "title": "Квартальное планирование" if ru else "Quarterly planning", "remote_title": "Квартальное планирование (перенесено)" if ru else "Quarterly planning (moved)", "detected_at": iso(datetime.now(self.zone) - timedelta(minutes=95))}]},
            {"id": "acc-ics", "provider": "ics", "preset": None, "name": "Праздники" if ru else "Public holidays", "calendar_id": "cal-holidays", "last_sync_at": self.calendars[5]["sync"]["last_sync_at"], "sync_error": "", "status": "ok", "conflicts": []},
        ]
        d = self.today

        def at(day: date, hour: int, minute: int = 0) -> str:
            return iso(datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.zone))

        def event(id_: str, calendar: str, title: str, start: str, end: str, **over: object) -> dict:
            row = {"id": id_, "calendar_id": calendar, "title": title, "description": "", "location": "", "start_at": start, "end_at": end, "all_day": False, "start_date": None, "end_date": None,
                   "timezone": zone, "color_override": None, "recurrence": "", "reminders": [10], "version": 1, "pending_sync": False, "conflict": None}
            row.update(over)
            return row

        def all_day(id_: str, calendar: str, title: str, first: date, days: int, **over: object) -> dict:
            end = first + timedelta(days=days)
            return event(id_, calendar, title, f"{first}T00:00:00Z", f"{end}T00:00:00Z", all_day=True, start_date=str(first), end_date=str(end), reminders=[], **over)

        monday = d - timedelta(days=d.weekday())
        words = {
            "sync": ("Еженедельная планёрка", "Weekly team sync"), "run": ("Утренняя пробежка", "Morning run"), "conf": ("Конференция в Лиссабоне", "Conference in Lisbon"),
            "lunch": ("Обед с Анной", "Lunch with Anna"), "review": ("Ревью дизайна", "Design review"), "bank": ("Звонок в банк", "Call with the bank"),
            "dentist": ("Стоматолог", "Dentist"), "cinema": ("Кино", "Cinema"), "holiday": ("Праздничный день", "Public holiday"), "planning": ("Квартальное планирование", "Quarterly planning"),
            "yoga": ("Йога", "Yoga class"), "focus": ("Глубокая работа", "Focus time"), "dinner": ("Ужин с родителями", "Dinner with parents"), "flight": ("Перелёт в Лиссабон", "Flight to Lisbon"),
            "clinic": ("ул. Садовая, 12", "12 Garden Street"), "room": ("Переговорная «Север»", "North meeting room"),
        }
        w = {k: v[0] if ru else v[1] for k, v in words.items()}
        self.events: list[dict] = [
            event("ev-sync", "cal-work", w["sync"], at(monday - timedelta(days=21), 10), at(monday - timedelta(days=21), 10, 45), recurrence="FREQ=WEEKLY;BYDAY=MO,TH", location=w["room"], description="Agenda in the team notes."),
            event("ev-run", "cal-personal", w["run"], at(d - timedelta(days=30), 7, 30), at(d - timedelta(days=30), 8, 0), recurrence="FREQ=DAILY", reminders=[]),
            all_day("ev-conf", "cal-work", w["conf"], d + timedelta(days=2), 3, location="Lisbon"),
            event("ev-lunch", "cal-family", w["lunch"], at(d, 13), at(d, 14)),
            event("ev-review", "cal-team", w["review"], at(d, 13, 30), at(d, 14, 30), location=w["room"]),
            event("ev-bank", "cal-personal", w["bank"], at(d, 13, 45), at(d, 14, 15)),
            event("ev-focus", "cal-work", w["focus"], at(d, 10, 0), at(d, 12, 0), pending_sync=False),
            event("ev-dentist", "cal-personal", w["dentist"], at(d + timedelta(days=1), 16), at(d + timedelta(days=1), 17, 30), location=w["clinic"], reminders=[60, 1440]),
            event("ev-cinema", "cal-family", w["cinema"], at(d - timedelta(days=1), 19), at(d - timedelta(days=1), 21, 30)),
            all_day("ev-holiday", "cal-holidays", w["holiday"], d + timedelta(days=6), 1),
            event("ev-planning", "cal-yandex", w["planning"], at(d + timedelta(days=1), 11), at(d + timedelta(days=1), 12, 30), pending_sync=True,
                  conflict={"event_id": "ev-planning", "title": w["planning"], "remote_title": self.accounts[1]["conflicts"][0]["remote_title"], "detected_at": self.accounts[1]["conflicts"][0]["detected_at"]}),
            event("ev-yoga", "cal-personal", w["yoga"], at(d - timedelta(days=2), 18, 30), at(d - timedelta(days=2), 19, 30)),
            event("ev-dinner", "cal-family", w["dinner"], at(d + timedelta(days=3), 19), at(d + timedelta(days=3), 21)),
            event("ev-flight", "cal-work", w["flight"], at(d + timedelta(days=1), 20, 15), at(d + timedelta(days=1), 23, 40)),
        ]
        self.overrides: dict[tuple[str, str], dict] = {}
        self.exdates: set[tuple[str, str]] = set()
        self.lists: list[dict] = [
            {"id": "list-inbox", "name": "Входящие" if ru else "Inbox", "color": "#7d8798", "is_default": True, "position": 0},
            {"id": "list-home", "name": "Дом" if ru else "Home", "color": "#e0a526", "is_default": False, "position": 1},
        ]
        task_words = {
            "bill": ("Оплатить счёт за электричество", "Pay the electricity bill"), "invoice": ("Отправить счёт в Acme", "Send the invoice to Acme"),
            "flights": ("Забронировать перелёт на конференцию", "Book flights for the conference"), "doc": ("Прочитать дизайн-документ", "Read the design doc"),
            "report": ("Написать квартальный отчёт", "Write the quarterly report"), "grandma": ("Позвонить бабушке", "Call grandma"),
            "photos": ("Сделать фото на паспорт", "Get passport photos"), "plants": ("Полить цветы", "Water the plants"),
        }
        tw = {k: v[0] if ru else v[1] for k, v in task_words.items()}

        def task(id_: str, title: str, **over: object) -> dict:
            row = {"id": id_, "list_id": "list-inbox", "title": title, "notes": "", "due_date": None, "due_time": None, "scheduled_start": None, "scheduled_end": None, "duration": None,
                   "priority": 0, "done_at": None, "reminders": [], "recurrence": "", "position": 0, "version": 1}
            row.update(over)
            return row

        self.tasks: list[dict] = [
            task("task-bill", tw["bill"], due_date=str(d), priority=3, list_id="list-home"),
            task("task-invoice", tw["invoice"], due_date=str(d - timedelta(days=2)), priority=2),
            task("task-flights", tw["flights"], due_date=str(d + timedelta(days=1)), priority=1),
            task("task-doc", tw["doc"], duration=45),
            task("task-report", tw["report"], due_date=str(d), scheduled_start=at(d, 15), scheduled_end=at(d, 16, 30), duration=90, priority=2),
            task("task-grandma", tw["grandma"], due_date=str(d + timedelta(days=3)), list_id="list-home"),
            task("task-photos", tw["photos"], due_date=str(d), done_at=iso(datetime.now(self.zone) - timedelta(hours=2))),
            task("task-plants", tw["plants"], list_id="list-home", duration=15),
        ]
        self.seq = 0

    # ── helpers ──

    def _id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-new{self.seq}"

    def _calendar(self, calendar_id: str) -> dict | None:
        return next((c for c in self.calendars if c["id"] == calendar_id), None)

    def _view(self, row: dict, occurrence_start: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        calendar = self._calendar(row["calendar_id"]) or {"color": "#4f8ff7", "writable": True}
        recurring = bool(row["recurrence"])
        view = {k: v for k, v in row.items() if k != "id"}
        view.update(
            id=f"{row['id']}:{occurrence_start}" if recurring else row["id"], event_id=row["id"], color=row["color_override"] or calendar["color"], recurring=recurring,
            occurrence_start=occurrence_start or row["start_at"], writable=bool(calendar["writable"]),
        )
        if start:
            view["start_at"], view["end_at"] = start, end
        if recurring and occurrence_start:
            view.update(self.overrides.get((row["id"], occurrence_start), {}))
        if view.get("all_day"):
            view["start_date"], view["end_date"] = view["start_at"][:10], view["end_at"][:10]
        return view

    def _rule(self, text: str) -> dict:
        fields = dict(part.split("=", 1) for part in text.replace("RRULE:", "").split(";") if "=" in part)
        return {"freq": fields.get("FREQ", "DAILY"), "interval": int(fields.get("INTERVAL", "1")), "byday": [b for b in fields.get("BYDAY", "").split(",") if b],
                "count": int(fields["COUNT"]) if "COUNT" in fields else None, "until": fields.get("UNTIL", "")[:8] or None, "bymonthday": int(fields["BYMONTHDAY"]) if "BYMONTHDAY" in fields else None}

    def _expand(self, row: dict, start: datetime, end: datetime) -> list[dict]:
        if not row["recurrence"]:
            if parse(row["start_at"]) < end and parse(row["end_at"]) > start:
                return [self._view(row)]
            return []
        rule = self._rule(row["recurrence"])
        first = parse(row["start_at"]).astimezone(self.zone)
        length = parse(row["end_at"]) - parse(row["start_at"])
        out: list[dict] = []
        day = first.date()
        made = 0
        until = datetime.strptime(rule["until"], "%Y%m%d").date() if rule["until"] else None
        while True:
            if until and day > until:
                break
            if parse(iso(datetime(day.year, day.month, day.day, tzinfo=self.zone))) >= end:
                break
            weeks = (day - (first.date() - timedelta(days=first.weekday()))).days // 7
            hit = False
            if rule["freq"] == "DAILY":
                hit = (day - first.date()).days % rule["interval"] == 0
            elif rule["freq"] == "WEEKLY":
                days = rule["byday"] or [BYDAY[first.weekday()]]
                hit = BYDAY[day.weekday()] in days and weeks % rule["interval"] == 0
            elif rule["freq"] == "MONTHLY":
                hit = day.day == (rule["bymonthday"] or first.day)
            elif rule["freq"] == "YEARLY":
                hit = (day.month, day.day) == (first.month, first.day)
            if hit:
                made += 1
                if rule["count"] and made > rule["count"]:
                    break
                local = datetime(day.year, day.month, day.day, first.hour, first.minute, tzinfo=self.zone)
                occurrence = iso(local)
                if (row["id"], occurrence) not in self.exdates:
                    view = self._view(row, occurrence, occurrence, iso(local + length))
                    if parse(view["start_at"]) < end and parse(view["end_at"]) > start:
                        out.append(view)
            day += timedelta(days=1)
            if (day - first.date()).days > 4000:
                break
        return out

    def occurrences(self, start: datetime, end: datetime, named: list[str] | None = None) -> list[dict]:
        shown = {c["id"] for c in self.calendars if c["visible"]} | set(named or [])
        found = [o for row in self.events if row["calendar_id"] in shown for o in self._expand(row, start, end)]
        return sorted(found, key=lambda o: (o["start_at"], o["title"]))

    def _tasks(self, query: dict[str, list[str]]) -> list[dict]:
        view = query.get("view", [""])[0]
        today = str(datetime.now(self.zone).date())
        tomorrow = str(datetime.now(self.zone).date() + timedelta(days=1))
        if "start" in query and "end" in query:
            start, end = parse(query["start"][0]), parse(query["end"][0])
            first, last = start.astimezone(self.zone).date().isoformat(), (end.astimezone(self.zone) - timedelta(seconds=1)).date().isoformat()
            return [t for t in self.tasks if (t["scheduled_start"] and parse(t["scheduled_start"]) < end and parse(t["scheduled_end"]) > start) or (t["due_date"] and first <= t["due_date"] <= last)]
        if view == "inbox":
            return [t for t in self.tasks if not t["due_date"] and not t["done_at"]]
        if view == "today":
            return [t for t in self.tasks if t["due_date"] == today or (t["scheduled_start"] and parse(t["scheduled_start"]).astimezone(self.zone).date().isoformat() == today)]
        if view == "upcoming":
            return sorted([t for t in self.tasks if t["due_date"] and t["due_date"] >= tomorrow and not t["done_at"]], key=lambda t: t["due_date"])
        if view == "overdue":
            return [t for t in self.tasks if t["due_date"] and t["due_date"] < today and not t["done_at"]]
        if view == "done":
            return [t for t in self.tasks if t["done_at"]]
        return list(self.tasks)

    # ── the routes ──

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        if not (path.startswith("/api/calendar") or path.startswith("/api/planner")):
            return None
        q = parse_qs(query)
        body = body or {}
        if method != "GET":
            self.writes.append((method, path + (f"?{query}" if query else ""), copy.deepcopy(body)))
        parts = path.split("/")
        if path == "/api/calendar/settings":
            if method == "PUT":
                self.settings.update(body)
            return 200, self.settings
        if path == "/api/calendar/calendars":
            if method == "POST":
                row = {"id": self._id("cal"), "name": body.get("name", ""), "color": body.get("color", "#4f8ff7"), "kind": "local", "account_id": None, "visible": True, "writable": True, "position": len(self.calendars), "default_reminders": [10], "sync": None}
                self.calendars.append(row)
                return 201, row
            return 200, sorted(self.calendars, key=lambda c: c["position"])
        if path.startswith("/api/calendar/calendars/"):
            row = self._calendar(parts[4])
            if row is None:
                return 404, {"detail": "no such calendar"}
            if method == "PATCH":
                row.update({k: v for k, v in body.items() if k in ("name", "color", "visible", "position", "default_reminders")})
                return 200, row
            if method == "DELETE":
                if row["kind"] != "local" or sum(1 for c in self.calendars if c["kind"] == "local") <= 1:
                    return 409, {"detail": "the last local calendar cannot be deleted"}
                self.calendars.remove(row)
                self.events = [e for e in self.events if e["calendar_id"] != row["id"]]
                return 200, {"ok": True}
        if path == "/api/calendar/events":
            if method == "GET":
                start, end = parse(q["start"][0]), parse(q["end"][0])
                named = q.get("calendars", [""])[0].split(",") if q.get("calendars") else None
                return 200, self.occurrences(start, end, named)
            if method == "POST":
                row = {"id": self._id("ev"), "description": "", "location": "", "all_day": False, "start_date": None, "end_date": None, "timezone": self.settings["timezone"], "color_override": body.get("color"),
                       "recurrence": "", "reminders": [], "version": 1, "pending_sync": False, "conflict": None}
                row.update({k: v for k, v in body.items() if k in ("calendar_id", "title", "description", "location", "start_at", "end_at", "all_day", "start_date", "end_date", "timezone", "recurrence", "reminders")})
                self.events.append(row)
                return 201, self._view(row)
        if path == "/api/calendar/search":
            words = q.get("q", [""])[0].lower()
            now = datetime.now(self.zone)
            window = self.occurrences(now - timedelta(days=60), now + timedelta(days=120))
            seen: dict[str, dict] = {}
            for o in window:
                if words not in f"{o['title']} {o['description']} {o['location']}".lower():
                    continue
                # One result per series: the occurrence nearest to now.
                best = seen.get(o["event_id"])
                if best is None or abs(parse(o["start_at"]) - now) < abs(parse(best["start_at"]) - now):
                    seen[o["event_id"]] = o
            found = sorted(seen.values(), key=lambda o: abs(parse(o["start_at"]) - now))
            return 200, found[: int(q.get("limit", ["20"])[0])]
        if path.startswith("/api/calendar/events/"):
            event_id = parts[4]
            row = next((e for e in self.events if e["id"] == event_id), None)
            if row is None:
                return 404, {"detail": "no such event"}
            if len(parts) == 6 and parts[5] == "resolve":
                row["conflict"] = None
                row["pending_sync"] = False
                for account in self.accounts:
                    account["conflicts"] = [c for c in account.get("conflicts", []) if c["event_id"] != event_id]
                    if not account["conflicts"] and account["status"] == "error":
                        account.update(status="ok", sync_error="", last_sync_at=iso(datetime.now(self.zone)))
                for c in self.calendars:
                    if c["account_id"] == "acc-yandex" and c["sync"]:
                        c["sync"] = {"last_sync_at": iso(datetime.now(self.zone)), "error": "", "status": "ok"}
                return 200, self._view(row)
            if method == "GET":
                return 200, {**self._view(row), "id": row["id"]}
            if method == "PUT":
                if body.get("version") != row["version"]:
                    return 409, {"detail": "the event changed since it was opened"}
                fields = {k: v for k, v in body.items() if k in ("calendar_id", "title", "description", "location", "start_at", "end_at", "all_day", "start_date", "end_date", "timezone", "recurrence", "reminders")}
                if "color" in body:
                    fields["color_override"] = body["color"]
                if body.get("scope") == "this" and row["recurrence"]:
                    self.overrides[(row["id"], body["occurrence_start"])] = {k: v for k, v in fields.items() if k != "recurrence"}
                else:
                    row.update(fields)
                row["version"] += 1
                return 200, self._view(row)
            if method == "DELETE":
                if q.get("scope", ["all"])[0] == "this" and row["recurrence"]:
                    self.exdates.add((row["id"], q["occurrence_start"][0]))
                else:
                    self.events.remove(row)
                return 200, {"ok": True}
        if path == "/api/calendar/accounts":
            if method == "POST":
                calendar_id = self._id("cal")
                row = {"id": self._id("acc"), "provider": body.get("provider", "caldav"), "preset": body.get("preset"), "name": body.get("name", ""), "calendar_id": calendar_id, "last_sync_at": None, "sync_error": "", "status": "never", "conflicts": []}
                self.accounts.append(row)
                self.calendars.append({"id": calendar_id, "name": row["name"], "color": "#7d8798", "kind": "caldav", "account_id": row["id"], "visible": True, "writable": True, "position": len(self.calendars), "default_reminders": [], "sync": {"last_sync_at": None, "error": "", "status": "never"}})
                return 201, row
            return 200, self.accounts
        if path == "/api/calendar/subscriptions" and method == "POST":
            calendar_id = self._id("cal")
            row = {"id": self._id("acc"), "provider": "ics", "preset": None, "name": body.get("name", ""), "calendar_id": calendar_id, "last_sync_at": None, "sync_error": "", "status": "never", "conflicts": []}
            self.accounts.append(row)
            self.calendars.append({"id": calendar_id, "name": row["name"], "color": body.get("color", "#34a853"), "kind": "ics", "account_id": row["id"], "visible": True, "writable": False, "position": len(self.calendars), "default_reminders": [], "sync": {"last_sync_at": None, "error": "", "status": "never"}})
            return 201, row
        if path == "/api/calendar/oauth/start" and method == "POST":
            # Somewhere the browser can go without leaving the harness: the app itself.
            return 200, {"url": "/app/calendar?linked=1"}
        if path.startswith("/api/calendar/accounts/"):
            account = next((a for a in self.accounts if a["id"] == parts[4]), None)
            if account is None:
                return 404, {"detail": "no such account"}
            if len(parts) == 6 and parts[5] == "sync":
                if account["conflicts"]:
                    return 502, {"detail": "calendar synchronization failed: an event changed in both places"}
                account.update(status="ok", sync_error="", last_sync_at=iso(datetime.now(self.zone)))
                return 200, account
            if method == "DELETE":
                self.accounts.remove(account)
                gone = {c["id"] for c in self.calendars if c["account_id"] == account["id"]}
                self.calendars = [c for c in self.calendars if c["id"] not in gone]
                self.events = [e for e in self.events if e["calendar_id"] not in gone]
                return 200, {"ok": True}
        if path == "/api/planner/lists":
            if method == "POST":
                row = {"id": self._id("list"), "name": body.get("name", ""), "color": body.get("color", "#7d8798"), "is_default": False, "position": len(self.lists)}
                self.lists.append(row)
                return 201, row
            return 200, self.lists
        if path == "/api/planner/tasks":
            if method == "POST":
                row = {"id": self._id("task"), "list_id": "list-inbox", "title": "", "notes": "", "due_date": None, "due_time": None, "scheduled_start": None, "scheduled_end": None, "duration": None,
                       "priority": 0, "done_at": None, "reminders": [], "recurrence": "", "position": len(self.tasks), "version": 1}
                row.update({k: v for k, v in body.items() if k in row and k not in ("id", "version", "done_at")})
                row["list_id"] = row["list_id"] or "list-inbox"
                self.tasks.append(row)
                return 201, row
            return 200, self._tasks(q)
        if path.startswith("/api/planner/tasks/"):
            row = next((t for t in self.tasks if t["id"] == parts[4]), None)
            if row is None:
                return 404, {"detail": "no such task"}
            if len(parts) == 6 and parts[5] == "complete":
                row["done_at"] = iso(datetime.now(self.zone)) if body.get("done", True) else None
                row["version"] += 1
                return 200, row
            if method == "PUT":
                if body.get("version") != row["version"]:
                    return 409, {"detail": "the task changed since it was opened"}
                row.update({k: v for k, v in body.items() if k in row and k not in ("id", "version", "done_at")})
                row["version"] += 1
                return 200, row
            if method == "DELETE":
                self.tasks.remove(row)
                return 200, {"ok": True}
        return 404, {"detail": f"the calendar stub has no {method} {path}"}

    def fulfil(self, route) -> bool:  # type: ignore[no-untyped-def]
        """Answers a Playwright route when it is one of these; ``False`` otherwise."""
        request = route.request
        url = request.url
        path = url.split("?", 1)[0]
        path = path[path.index("/api/"):] if "/api/" in path else path
        query = url.split("?", 1)[1] if "?" in url else ""
        body = None
        if request.method in ("POST", "PUT", "PATCH") and request.post_data:
            try:
                body = json.loads(request.post_data)
            except ValueError:
                body = None
        found = self.answer(request.method, path, query, body)
        if found is None:
            return False
        status, payload = found
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return True
