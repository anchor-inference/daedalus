"""A host's schedules as the Schedules screen and the schedules dialog read them.

The shared table answers an empty list, which drew a screen with nothing on it and proved nothing about
the rows the operator actually has: agent tasks with instructions two thousand characters long, cron
lines of several shapes (hourly at a minute, daily, four times a day, weekly, every six hours), a
schedule the agent asks for that waits for the operator, a run that failed with its error, a paused
schedule and a finished one-off. This keeps such a set, answers every route the screen calls, and
records the commands it was sent so a check can read them back.

``ScheduleStub(lang, chat=..., other=...)`` names two conversations the schedules belong to, so a
harness can open one of them and find its schedules there. ``answer(method, path, body)`` returns
``(status, payload)`` for a schedules route and ``None`` for any other.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from croniter import croniter

WORDS = {
    "en": {
        "mine": "Mine hourly check",
        "mine.prompt": (
            "## Hourly check of the Minecraft server\n\n"
            "1. Read the last hour of `logs/latest.log` and count the players who joined and left.\n"
            "2. Look for **crashes**, `OutOfMemoryError`, tick lag above 2000 ms and chunk errors.\n"
            "3. If the TPS fell below 15 for more than five minutes, restart the server with "
            "`systemctl restart minecraft` and say so in this chat.\n"
            "4. Check the free disk space of the world volume; warn below 10 GB.\n\n"
            "Report in three lines at most when everything is fine. When something is wrong, quote the log "
            "lines that show it, say what you did about it and what is left for me to decide. Never delete a "
            "world backup and never change `server.properties` without asking first. Keep the report in the "
            "same language as this conversation, and do not repeat yesterday's findings unless they changed."
        ),
        "digest": "Morning digest",
        "digest.prompt": "Collect the overnight mail, the calendar for today and the open pull requests, and send me a digest of ten lines at most.",
        "watch": "Site watch check",
        "watch.prompt": "Open the shop's front page and the checkout, confirm both answer within two seconds, and tell me only when one does not.",
        "weekly": "Weekly report reminder",
        "weekly.prompt": "Send the weekly report to the team before noon.",
        "price": "Price check",
        "price.prompt": "Compare the price of the three graphics cards on the watch list and note any drop of more than five percent.",
        "expedition": "Expedition recorder",
        "expedition.prompt": "Record the expedition's arrival and post the summary in the project chat.",
        "reset": "Mine reset",
        "reset.prompt": (
            "Every night at the quietest hour, reset the mining world:\n\n"
            "- announce the reset in the game chat five minutes ahead;\n"
            "- save and stop the server, move `world_mine` to `backups/` with today's date;\n"
            "- regenerate it from the seed in `mine.seed` and start the server again;\n"
            "- check that players can join, and report the size of the old world and how long it took.\n\n"
            "If any step fails, put the old world back and tell me which step failed."
        ),
        "error": "provider timed out after 120 s",
        "summary": "Two players online at the peak; TPS 19.8; no errors in the log. Disk: 61 GB free.",
    },
    "ru": {
        "mine": "Ежечасная проверка шахты",
        "mine.prompt": (
            "## Ежечасная проверка сервера Minecraft\n\n"
            "1. Прочитай последний час `logs/latest.log` и посчитай, сколько игроков зашло и вышло.\n"
            "2. Найди **падения**, `OutOfMemoryError`, лаги тика больше 2000 мс и ошибки чанков.\n"
            "3. Если TPS был ниже 15 дольше пяти минут, перезапусти сервер командой "
            "`systemctl restart minecraft` и напиши об этом в этот чат.\n"
            "4. Проверь свободное место на томе мира; предупреди, если меньше 10 ГБ.\n\n"
            "Если всё в порядке, отчёт не длиннее трёх строк. Если что-то не так, процитируй строки лога, "
            "скажи, что ты сделал и что осталось решить мне. Никогда не удаляй резервные копии мира и не меняй "
            "`server.properties` без спроса. Пиши на языке этого чата и не повторяй вчерашние находки, если они "
            "не изменились."
        ),
        "digest": "Утренняя сводка",
        "digest.prompt": "Собери ночную почту, календарь на сегодня и открытые пулл-реквесты и пришли сводку не длиннее десяти строк.",
        "watch": "Проверка сайта",
        "watch.prompt": "Открой главную страницу магазина и оформление заказа, убедись, что обе отвечают быстрее двух секунд, и напиши, только если какая-то не отвечает.",
        "weekly": "Напоминание о недельном отчёте",
        "weekly.prompt": "Отправить недельный отчёт команде до полудня.",
        "price": "Проверка цен",
        "price.prompt": "Сравни цены трёх видеокарт из списка и отметь падение больше пяти процентов.",
        "expedition": "Запись экспедиции",
        "expedition.prompt": "Записать прибытие экспедиции и выложить итог в чат проекта.",
        "reset": "Сброс шахты",
        "reset.prompt": (
            "Каждую ночь в самый тихий час сбрасывай шахтёрский мир:\n\n"
            "- за пять минут объяви сброс в игровом чате;\n"
            "- сохрани и останови сервер, перенеси `world_mine` в `backups/` с сегодняшней датой;\n"
            "- сгенерируй его заново из сида в `mine.seed` и запусти сервер;\n"
            "- проверь, что игроки могут зайти, и сообщи размер старого мира и сколько это заняло.\n\n"
            "Если какой-то шаг не удался, верни старый мир и скажи, на каком шаге."
        ),
        "error": "провайдер не ответил за 120 с",
        "summary": "В пике два игрока; TPS 19.8; ошибок в логе нет. Диск: свободно 61 ГБ.",
    },
}


def _at(**delta: float) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def _next(cron: str) -> str:
    """The next run the host would compute, so a row's "in 3h" agrees with its "every day at 07:10"."""
    return croniter(cron, datetime.now(UTC)).get_next(datetime).astimezone(UTC).isoformat()


class ScheduleStub:
    def __init__(self, lang: str = "en", *, chat: str = "chat-mine", other: str = "chat-ops", require_approval: bool = False) -> None:
        words = WORDS.get(lang, WORDS["en"])
        self.chat = chat
        self.require_approval = require_approval
        self.writes: list[tuple[str, str, dict]] = []
        self.revision = 9

        def row(id_: str, **over: object) -> dict:
            base = {"id": id_, "name": "", "cron": None, "run_at": None, "prompt": "", "enabled": 1, "next_run_at": None,
                    "last_run_at": None, "last_summary": None, "kind": "agent", "target_session": None, "run_in": "new",
                    "created_by_session": None, "active_session_id": None, "failure_count": 0, "last_error": None,
                    "authority_state": "current", "schedule_revision": 3, "project_id": None, "files": "[]",
                    "grant_expires_at": _at(days=3640), "last_session_id": None, "source_session_id": None}
            base.update(over)
            return base

        self.schedules = [
            row("s1a2b3c4d5e6f701", name=words["mine"], cron="5 * * * *", prompt=words["mine.prompt"], run_in="self",
                target_session=chat, created_by_session=chat, source_session_id=chat, next_run_at=_next("5 * * * *"),
                last_run_at=_at(minutes=-37), last_summary=words["summary"], last_session_id=chat,
                files='[{"name": "server.properties", "size": 1834, "digest": "a1"}, {"name": "ops.json", "size": 212, "digest": "b2"}]'),
            row("s2b3c4d5e6f70812", name=words["digest"], cron="10 2 * * *", prompt=words["digest.prompt"], created_by_session=other,
                source_session_id=other, next_run_at=_next("10 2 * * *"), last_run_at=_at(hours=-9), failure_count=1,
                last_error=words["error"], last_session_id="run-digest-1"),
            row("s3c4d5e6f7081223", name=words["watch"], cron="50 5,11,17,23 * * *", prompt=words["watch.prompt"], run_in="self",
                target_session=chat, created_by_session=chat, source_session_id=chat, next_run_at=_next("50 5,11,17,23 * * *"),
                last_run_at=_at(hours=-2, minutes=-48), last_session_id=chat),
            row("s4d5e6f708122334", name=words["weekly"], cron="0 4 * * 1", prompt=words["weekly.prompt"], kind="message",
                next_run_at=_next("0 4 * * 1"), last_run_at=_at(days=-3)),
            row("s5e6f70812233445", name=words["price"], cron="0 */6 * * *", prompt=words["price.prompt"], enabled=0,
                created_by_session=other, source_session_id=other, last_run_at=_at(days=-2)),
            row("s6f7081223344556", name=words["expedition"], run_at=_at(days=-1, hours=-3), prompt=words["expedition.prompt"],
                enabled=0, created_by_session=chat, source_session_id=chat, last_run_at=_at(days=-1, hours=-3),
                last_session_id="run-expedition", recurring=0),
        ]
        self.proposals = [
            {"id": "p7081223344556a7", "name": words["reset"], "prompt": words["reset.prompt"], "cron": "10 2 * * *", "run_at": None,
             "kind": "agent", "run_in": "self", "next_run_at": _next("10 2 * * *"), "file_count": 1,
             "files": [{"name": "mine.seed", "size": 64, "digest": "9f2c4e1d0a7b"}], "legacy_file_review_required": False,
             "source_session_id": chat, "source_project_id": None, "proposal_revision": 1, "request_digest": "reset-digest",
             "status": "pending", "accepted_schedule_id": None, "legacy_schedule_id": None, "created_at": _at(days=-3)},
        ]
        self.cycles = {
            "s1a2b3c4d5e6f701": [
                {"id": f"c1-{n}", "due_at": _at(hours=-n, minutes=-37), "kind": "agent", "manual": 0,
                 "effect_state": "completed", "action_state": "delivered", "effect_error": None, "session_id": chat}
                for n in range(0, 5)
            ] + [{"id": "c1-x", "due_at": _at(hours=-6, minutes=-37), "kind": "agent", "manual": 1, "effect_state": "failed",
                  "action_state": "failed", "effect_error": words["error"], "session_id": chat}],
            "s2b3c4d5e6f70812": [
                {"id": "c2-1", "due_at": _at(hours=-9), "kind": "agent", "manual": 0, "effect_state": "failed",
                 "action_state": "failed", "effect_error": words["error"], "session_id": "run-digest-1"},
                {"id": "c2-2", "due_at": _at(hours=-33), "kind": "agent", "manual": 0, "effect_state": "completed",
                 "action_state": "delivered", "effect_error": None, "session_id": "run-digest-0"},
            ],
        }

    def snapshot(self) -> dict:
        return copy.deepcopy({"schedules": self.schedules, "proposals": self.proposals})

    def _proposals(self) -> dict:
        return {"entries": self.proposals, "collection_revisions": {"global:global": self.revision},
                "require_approval": self.require_approval, "approval_days": 30 if self.require_approval else 3650}

    def answer(self, method: str, path: str, body: dict | None = None) -> tuple[int, object] | None:
        body = body or {}
        method = method.upper()
        if path == "/api/schedules" and method == "GET":
            return 200, self.schedules
        if path == "/api/recurring/proposals" and method == "GET":
            return 200, self._proposals()
        if path == "/api/recurring/overview" and method == "GET":
            return 200, {"global_collection_revision": self.revision}
        if path == "/api/recurring/preview" and method == "POST":
            return 200, {"next_run_at": body.get("run_at") or _at(hours=1), "timezone": "UTC", "output_contract": {}, "authority": ""}
        if path.startswith("/api/recurring/card/") and method == "GET":
            item_id = path.rsplit("/", 1)[1]
            proposal = next((p for p in self.proposals if p["id"] == item_id), None)
            schedule_id = proposal["accepted_schedule_id"] if proposal else item_id
            schedule = next((s for s in self.schedules if s["id"] == schedule_id), None)
            if proposal is None and schedule is None:
                return 404, {"detail": "no such schedule or occurrence"}
            card = None if schedule is None else {**{k: schedule[k] for k in ("id", "name", "cron", "run_at", "next_run_at", "kind", "run_in", "enabled", "authority_state")}, "deleted": False, "set_by_agent": True}
            return 200, {"schedule": card, "proposal": proposal, "collection_revision": self.revision if proposal else None,
                         "require_approval": self.require_approval, "approval_days": 30 if self.require_approval else 3650}
        if path.startswith("/api/recurring/proposals/") and method == "POST":
            _, _, _, _, proposal_id, action = path.split("/")
            proposal = next((p for p in self.proposals if p["id"] == proposal_id), None)
            if proposal is None or proposal["status"] != "pending":
                return 409, {"detail": {"reason": "the proposal changed", "current_revision": self.revision}}
            self.writes.append((method, path, body))
            self.revision += 1
            if action == "withdraw":
                proposal["status"] = "withdrawn"
                return 200, {"proposal_id": proposal_id, "status": "withdrawn", "receipt_id": "r", "entity_revision": self.revision}
            new_id = "s" + proposal_id[1:]
            proposal.update(status="accepted", accepted_schedule_id=new_id)
            self.schedules.append({"id": new_id, "name": proposal["name"], "cron": proposal["cron"], "run_at": proposal["run_at"],
                                   "prompt": proposal["prompt"], "enabled": 1, "next_run_at": proposal["next_run_at"], "last_run_at": None,
                                   "last_summary": None, "kind": proposal["kind"], "target_session": proposal["source_session_id"],
                                   "run_in": proposal["run_in"], "created_by_session": proposal["source_session_id"], "active_session_id": None,
                                   "failure_count": 0, "last_error": None, "authority_state": "current", "schedule_revision": 1,
                                   "project_id": None, "files": "[]", "grant_expires_at": body.get("expires_at"),
                                   "last_session_id": None, "source_session_id": proposal["source_session_id"]})
            return 200, {"proposal_id": proposal_id, "id": new_id, "proposal_revision": 2, "receipt_id": "r", "entity_revision": self.revision}
        if path.startswith("/api/recurring/"):
            parts = path.split("/")
            schedule = next((s for s in self.schedules if s["id"] == parts[3]), None)
            if schedule is None:
                return 404, {"detail": "no such schedule"}
            if len(parts) == 5 and parts[4] == "cycles" and method == "GET":
                return 200, {"schedule_id": schedule["id"], "schedule_revision": schedule["schedule_revision"],
                             "authority_state": schedule["authority_state"], "next_run_at": schedule["next_run_at"],
                             "output_contract": {}, "scope": {"kind": "global", "id": "global"},
                             "collection_revision": self.revision, "cycles": self.cycles.get(schedule["id"], [])}
            self.writes.append((method, path, body))
            if len(parts) == 4 and method == "PATCH":
                schedule.update({k: v for k, v in body.items() if k in {"name", "prompt", "cron", "run_at"}})
                if "enabled" in body:
                    schedule["enabled"] = int(bool(body["enabled"]))
                schedule["schedule_revision"] += 1
                schedule["authority_state"] = "needs_approval"
                self.revision += 1
                return 200, {"id": schedule["id"], "schedule_revision": schedule["schedule_revision"], "authority_state": "needs_approval",
                             "entity_revision": self.revision, "receipt_id": body.get("client_operation_id")}
            if len(parts) == 5 and parts[4] == "approve" and method == "POST":
                schedule["schedule_revision"] += 1
                schedule["authority_state"] = "current"
                schedule["grant_expires_at"] = body.get("expires_at")
                self.revision += 1
                return 200, {"id": schedule["id"], "schedule_revision": schedule["schedule_revision"], "authority_state": "current"}
            if len(parts) == 5 and parts[4] == "remove" and method == "POST":
                self.schedules.remove(schedule)
                self.revision += 1
                return 200, {"id": schedule["id"], "deleted": True}
            if len(parts) == 5 and parts[4] == "run" and method == "POST":
                return 200, {"cycle_id": "manual", "effect_id": "manual", "state": "pending"}
            return 404, {"detail": "no such recurring action"}
        return None
