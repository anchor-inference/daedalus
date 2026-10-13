"""The agent reads and changes Daedalus's own settings, and the operator approves what is not harmless.

``SettingsGet`` answers with a section's values, credentials masked, and what each field means.
``SettingsChange`` takes a merge patch over the settings sections, validates the whole result the way
the settings screen's save does (:func:`daedalus.host.config_validation.validate_candidate`), and then:

* a change only of sections in ``[agent_settings] auto_apply`` is saved at once;
* any other waits for the operator: a card in the session (the app draws it from
  ``GET /api/settings/changes/<id>``) and a notification with Apply and Reject, answered through the
  ``settings:`` resolver or the card's own buttons — the first answer wins;
* a change of anything :mod:`daedalus.host.agent_settings` lists as never is refused outright.

Apply validates again against the revision the change was made on. When the settings moved since,
the change is not applied over them: it is marked stale and the agent is asked to propose anew.
Whatever the outcome — applied, rejected, stale, failed, expired — the agent hears it as a message in
its session (origin ``settings``), which is how every other decision made after a tool returned
reaches it. A message the session could not take then is delivered on a later pass.

The changes are kept in ``kv`` (``settings_change:<id>``), so a pending card survives a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from daedalus.config import RuntimeConfig
from daedalus.extensions.notifications import Action, ActionConflict, ActionOutcome, ActionRefused, ActionRequest, Draft
from daedalus.host import agent_settings as rules
from daedalus.host.config_validation import ConfigConflict, config_revision, validate_candidate
from daedalus.host.help_search import config_links
from daedalus.host.notify_text import render
from daedalus.host.setting_refs import SPEECH_MODELS
from daedalus.speech import catalog as speech_catalog
from daedalus.speech.service import CLOUD

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

KEY = "settings_change:"
PREFIX = "settings"
"""The notification resolver's prefix: a request is ``settings:<session>:<change id>``."""
REASON_MAX = 1000
CHANGES_MAX_CHARS = 20_000
"""A patch larger than this is not a settings change a person reads on a card."""
TICK_SECONDS = 60.0
KEEP_DAYS = 30
"""How long a decided change stays readable, for the card that shows how it ended."""
DECIDED = ("applied", "rejected", "expired", "stale", "failed", "withdrawn")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def body_sections() -> dict[str, str]:
    """The sections the settings body carries, each with its note: the sections the agent may name.

    Read off the settings screen's own body, so a section added there (navigation, say) is the
    agent's to read and change from that moment, classed like every other.
    """
    from daedalus.extensions.api import SettingsBody  # Lazy: the API module installs last and imports half the host.

    docs = rules.attribute_docs(SettingsBody)
    stored = RuntimeConfig.model_fields
    return {name: docs.get(name, "") for name in SettingsBody.model_fields if name in stored}


def _section_link(section: str, links: dict[str, str]) -> str | None:
    """Where a configuration section is edited: the settings page of its first indexed field."""
    found = next((link for path, link in links.items() if path == section or path.startswith(section + ".")), None)
    return found.split("#", 1)[0] if found else None


def masked_settings(config: Any) -> dict[str, Any]:
    """The configuration as ``GET /api/settings`` masks it, and then every credential it did not know of."""
    from daedalus.extensions.api import mask_provider_keys  # Lazy: the API module imports half the host.

    view = mask_provider_keys(config.model_dump(mode="json"))
    view.pop("provider_kinds", None)
    return rules.mask(view)


class SettingsChanges:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._lock = asyncio.Lock()

    # -- reading ---------------------------------------------------------------------------

    def sections(self) -> dict[str, str]:
        return body_sections()

    def links(self) -> dict[str, str]:
        """The address of the row that edits each field, from the settings index: the agent hands it to
        the operator when the change is theirs to make, or when they want to see it."""
        root = getattr(getattr(self.app, "settings", None), "bot_repo_dir", None)
        return config_links(Path(root)) if root else {}

    async def get(self, section: str | None) -> dict[str, Any]:
        config = self.app.config
        sections = self.sections()
        auto = rules.auto_sections(config, sections)
        revision = config_revision(config)
        links = self.links()
        if not section:
            return {
                "revision": revision,
                "sections": [
                    {
                        "name": name, "about": rules.section_summary(name, sections), "applies": "at once" if name in auto else "after the operator's Apply",
                        **({"link": where} if (where := _section_link(name, links)) else {}),
                    }
                    for name in sorted(sections)
                ],
            }
        if section not in sections:
            raise rules.ChangeRefused(f"no such settings section {section!r}; the sections are {', '.join(sorted(sections))}")
        fields = {path: {**info, "link": links[path]} if path in links else info for path, info in rules.describe_section(section).items()}
        operator_only = sorted({path for path in fields if rules.never_reason(path) is not None} | {pattern for pattern in rules.NEVER if pattern.split(".", 1)[0] == section})
        return {
            "revision": revision,
            "section": section,
            "applies": "at once" if section in auto else "after the operator's Apply",
            "values": masked_settings(config).get(section),
            "fields": fields,
            "operator_only": operator_only,
        }

    # -- proposing -------------------------------------------------------------------------

    def _speech_problems(self, diff: list[rules.Change]) -> list[str]:
        """Why a chosen speech recogniser could not work, said before it is saved.

        The configuration takes any string for ``asr.transcriber``: the settings screen only ever
        offers the endpoint and the installed models, so nothing checked. The agent is not shown that
        list, and asked to "turn on speech recognition with GigaAM" it saved the id of a model that was
        never downloaded — every voice note then failed while the screen said the setting was made.
        """
        speech = getattr(self.app, "speech", None)
        problems: list[str] = []
        for change in diff:
            if change.path not in ("asr.transcriber", "asr.fallback") or change.new in (None, "", CLOUD):
                continue
            try:
                model = speech_catalog.get(str(change.new))
            except KeyError:
                choices = ", ".join(model.id for model in speech_catalog.MODELS)
                problems.append(f"{change.path}: {change.new!r} is no speech recogniser; it is 'cloud' (the endpoint in [asr]) or a local model id: {choices}")
                continue
            if speech is not None and speech.installed(model.id) is None:
                problems.append(
                    f"{change.path}: the local model {model.id!r} is not downloaded on this machine, so nothing could hear with it. "
                    f"The operator downloads it under Settings → Voice & speech ({SPEECH_MODELS.link()}); "
                    "give them that link, and make this change once it is installed"
                )
        return problems

    async def change(self, *, session_id: str, tool_call_id: str, changes: Any, reason: str) -> dict[str, Any]:
        """Validate a change and apply it, or post it for the operator. Raises :class:`rules.ChangeRefused`."""
        if isinstance(changes, str):
            # Some models hand an object argument over as its JSON text.
            try:
                changes = json.loads(changes)
            except ValueError:
                pass
        if not isinstance(changes, dict) or not changes:
            raise rules.ChangeRefused("changes is an object of sections, each with the fields to change, e.g. {\"compaction\": {\"auto_ratio\": 0.6}}")
        if len(json.dumps(changes, ensure_ascii=False)) > CHANGES_MAX_CHARS:
            raise rules.ChangeRefused(f"a settings change is at most {CHANGES_MAX_CHARS} characters of JSON")
        reason = " ".join(str(reason or "").split())
        if not reason:
            raise rules.ChangeRefused("say why in reason: the operator reads it beside the change")
        reason = reason[:REASON_MAX]
        async with self._lock:
            config = self.app.config
            sections = self.sections()
            base = config_revision(config)
            report, candidate = validate_candidate(config, rules.candidate(config, changes, sections), base_revision=base)
            if not report.valid or candidate is None:
                problems = "; ".join(f"{p.path}: {p.message}" for p in report.problems) or "the settings changed while this was checked"
                raise rules.ChangeRefused(f"the change is not valid: {problems}")
            auto = rules.auto_sections(config, sections)
            diff = rules.diff(config, candidate, {c.path: c.apply_at for c in report.changes}, auto)
            if not diff:
                raise rules.ChangeRefused("nothing would change: the settings already say that")
            unusable = self._speech_problems(diff)
            if unusable:
                raise rules.ChangeRefused("; ".join(unusable) + ". Nothing was changed.")
            refused = [f"{c.path} ({rules.never_reason(c.path)})" for c in diff if c.kind == "never"]
            if refused:
                raise rules.ChangeRefused(
                    "these settings are the operator's alone and cannot be changed by the agent: " + "; ".join(refused)
                    + ". Nothing was changed; tell the operator to change them in Settings if they want to."
                )
            kind = rules.overall(diff)
            for existing in await self._records(status="pending"):
                if existing["session_id"] == session_id and existing["patch"] == changes:
                    return existing
            record: dict[str, Any] = {
                "id": "sc" + uuid.uuid4().hex[:10],
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "reason": reason,
                "patch": changes,
                "base_revision": base,
                "changes": [c.as_dict() for c in diff],
                "kind": kind,
                "status": "pending",
                "created_at": _now(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=config.agent_settings.proposal_hours)).isoformat(),
                "decided_at": None,
                "via": "",
                "error": "",
                "told": False,
            }
            if kind == "auto":
                try:
                    await self._save(candidate, base)
                except ConfigConflict as exc:
                    raise rules.ChangeRefused("the settings changed while this was saved; read them again and repeat the change") from exc
                record.update(status="applied", decided_at=_now(), via="auto", told=True)
                await self._put(record)
                return record
            await self._put(record)
        await self._post(record)
        return record

    async def _save(self, candidate: Any, base: str) -> None:
        await self.app.save_config(candidate, expected_revision=base)
        manager = self.app.manager
        if manager is not None:
            # What the settings screen's save does after it, for the same reason: an endpoint the
            # change retired would otherwise keep its connection pool until the next restart.
            await manager.providers.close_retired()

    async def _post(self, record: dict[str, Any]) -> None:
        notifications = self.app.notifications
        if notifications is None:
            return
        lang = notifications.language()
        manager = self.app.manager
        state = await manager.get_state(record["session_id"]) if manager is not None else None
        title = state.session.title if state is not None else record["session_id"]
        project = state.project.id if state is not None and state.project is not None else None
        body = "\n".join([*rules.change_lines(record["changes"]), "", record["reason"]])
        ref = request_ref(record)
        try:
            await notifications.post(Draft(
                "permission", render("settings.change", lang, title=title), body, kind="settings_change", tone="warning", level="normal",
                session_id=record["session_id"], project_id=project, link=f"/app/agents/{record['session_id']}",
                # Reject may be pressed on a lock screen; Apply opens the app, as a host-level permission
                # does: a lock-screen token is not someone who should change how the agent is run.
                actions=(
                    Action("apply", render("settings.apply", lang), "primary"),
                    Action("reject", render("settings.reject", lang), "default", quick=True),
                    Action("open", render("open", lang), "ghost"),
                ),
                request_ref=ref, dedupe_key=ref, source="settings",
            ))
        except Exception:  # noqa: BLE001 — the card in the session still asks; the notification is the second way in
            logger.warning("the settings change %s was not posted as a notification", record["id"], exc_info=True)

    # -- deciding --------------------------------------------------------------------------

    async def decide(self, change_id: str, action: str, *, via: str) -> dict[str, Any]:
        """The operator's Apply or Reject. Raises ``LookupError``, :class:`ActionRefused`, or
        :class:`ActionConflict` when the change was already decided (it carries how)."""
        if action not in ("apply", "reject"):
            raise ActionRefused(f"a settings change is answered with apply or reject, not {action!r}")
        async with self._lock:
            record = await self.view(change_id)
            if record is None:
                raise LookupError(change_id)
            if record["status"] != "pending":
                raise ActionConflict(record["status"])
            if action == "reject":
                record.update(status="rejected")
            else:
                await self._apply(record)
            record.update(decided_at=_now(), via=via)
            await self._put(record)
        await self._settle(record, via=via)
        return record

    async def _apply(self, record: dict[str, Any]) -> None:
        config = self.app.config
        if config_revision(config) != record["base_revision"]:
            record.update(status="stale", error="the settings changed after the change was proposed")
            return
        try:
            report, candidate = validate_candidate(config, rules.candidate(config, record["patch"], self.sections()), base_revision=record["base_revision"])
        except rules.ChangeRefused as exc:
            record.update(status="failed", error=str(exc))
            return
        if not report.valid or candidate is None:
            record.update(status="failed", error="; ".join(f"{p.path}: {p.message}" for p in report.problems) or "the change is no longer valid")
            return
        try:
            await self._save(candidate, record["base_revision"])
        except ConfigConflict:
            record.update(status="stale", error="the settings changed after the change was proposed")
        except Exception as exc:  # noqa: BLE001 — save_config rolled back; the operator and the agent are told why
            logger.exception("the settings change %s could not be saved", record["id"])
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        else:
            record.update(status="applied")

    async def _settle(self, record: dict[str, Any], *, via: str) -> None:
        """Close the notification and tell the agent; both are repeated by the next pass when they fail."""
        if self.app.notifications is not None:
            try:
                await self.app.notifications.resolve(request_ref(record), record["status"], via=via)
            except Exception:  # noqa: BLE001
                logger.warning("the notification of settings change %s was not resolved", record["id"], exc_info=True)
        await self._tell(record)

    async def _tell(self, record: dict[str, Any]) -> None:
        manager = self.app.manager
        if manager is None or record.get("told"):
            return
        try:
            await manager.submit(record["session_id"], rules.outcome_text(record), as_answer=False, origin="settings")
        except KeyError:
            pass  # the session is gone: nobody is left to tell
        except Exception:  # noqa: BLE001 — a session that cannot take a message now (no model, starting up) is told on a later pass
            logger.info("settings change %s: the outcome waits for session %s", record["id"], record["session_id"], exc_info=True)
            return
        record["told"] = True
        await self._put(record)

    async def resolve_action(self, request: ActionRequest) -> ActionOutcome:
        """The ``settings:`` resolver: Apply or Reject pressed on the notification (or its push)."""
        if request.action not in ("apply", "reject"):
            raise ActionRefused(f"a settings change is answered with apply or reject, not {request.action!r}")
        try:
            record = await self.decide(request.target, request.action, via=request.via)
        except LookupError as exc:
            raise ActionConflict("withdrawn") from exc
        return ActionOutcome(record["status"])

    async def withdraw_session(self, session_id: str) -> None:
        """A deleted session's pending changes are withdrawn: nobody is left to hear the answer."""
        async with self._lock:
            for record in await self._records(status="pending"):
                if record["session_id"] == session_id:
                    record.update(status="withdrawn", decided_at=_now(), via="deleted", told=True)
                    await self._put(record)

    # -- keeping ---------------------------------------------------------------------------

    async def view(self, change_id: str) -> dict[str, Any] | None:
        value = await self.app.db.kv_get(KEY + change_id)
        return value if isinstance(value, dict) else None

    async def _put(self, record: dict[str, Any]) -> None:
        await self.app.db.kv_set(KEY + record["id"], record)

    async def _records(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = await self.app.db.fetchall("SELECT value FROM kv WHERE key LIKE ? ESCAPE '\\'", (KEY.replace("_", "\\_") + "%",))
        records = [json.loads(row["value"]) for row in rows]
        return [r for r in records if isinstance(r, dict) and (status is None or r.get("status") == status)]

    async def tick(self) -> None:
        """Expire what waited too long, tell the agents what they have not been told, forget the old."""
        now = datetime.now(UTC)
        expired: list[dict[str, Any]] = []
        async with self._lock:
            for record in await self._records(status="pending"):
                if datetime.fromisoformat(record["expires_at"]) <= now:
                    record.update(status="expired", decided_at=_now(), via="timeout")
                    await self._put(record)
                    expired.append(record)
        for record in expired:
            await self._settle(record, via="timeout")
        horizon = now - timedelta(days=KEEP_DAYS)
        for record in await self._records():
            if record.get("status") in DECIDED and not record.get("told"):
                await self._tell(record)
            elif record.get("status") in DECIDED and record.get("decided_at") and datetime.fromisoformat(record["decided_at"]) < horizon:
                await self.app.db.execute("DELETE FROM kv WHERE key = ?", (KEY + record["id"],))

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad pass must not end the expiry for good
                logger.exception("the settings changes pass failed")
            await asyncio.sleep(max(1.0, TICK_SECONDS - (time.monotonic() - started)))


def request_ref(record: dict[str, Any]) -> str:
    return f"{PREFIX}:{record['session_id']}:{record['id']}"


def public(record: dict[str, Any]) -> dict[str, Any]:
    """What the app's card is given: everything but the bookkeeping."""
    return {key: record.get(key) for key in ("id", "session_id", "tool_call_id", "reason", "changes", "kind", "status", "created_at", "expires_at", "decided_at", "via", "error")}


async def install(app: Application) -> list[asyncio.Task[None]]:
    assert app.manager is not None
    manager = app.manager
    service = SettingsChanges(app)
    app.extensions["agent_settings"] = service

    async def settings_get(*, session_id: str, section: str | None) -> dict[str, Any]:
        return await service.get(section)

    async def settings_change(*, session_id: str, tool_call_id: str, changes: Any, reason: str) -> dict[str, Any]:
        return await service.change(session_id=session_id, tool_call_id=tool_call_id, changes=changes, reason=reason)

    manager.service_hooks["settings_get"] = settings_get
    manager.service_hooks["settings_change"] = settings_change
    manager.delete_hooks.append(service.withdraw_session)
    if app.notifications is not None:
        app.notifications.register_resolver(PREFIX, service.resolve_action)
    return [asyncio.create_task(service.run(), name="settings-changes")]


__all__ = ["KEY", "PREFIX", "SettingsChanges", "body_sections", "install", "masked_settings", "public", "request_ref"]
