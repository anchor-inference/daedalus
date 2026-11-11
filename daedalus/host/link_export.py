"""One conversation over an agent link, taken out whole: a zip to keep, or a note in the chat that owns it.

Both directions end up in the same record. A conversation of ours at somebody else's door is its ``agent_talks``
row with its messages; a guest's conversation at our door is what the door's control socket says of it, beside what
this side keeps. Everything here works on plain dicts and paths, so what an export holds can be checked without a
door, a chat or a database.

What a record keeps of a conversation is named field by field (:data:`TALK_FIELDS`, :data:`GUEST_FIELDS`): the row
of a conversation of ours holds its bearer token and the hash of its invite, and a field added to a table later must
not reach a file the operator mails to somebody by accident. Where a file lies on this machine is never written
either: it is read to put the bytes into the zip, and the zip names the file only by its own safe name.
"""

from __future__ import annotations

import json
import os
import stat
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daedalus.host import link_protocol

FILES_BOUND_BYTES = 200 * link_protocol.MB
"""The most the kept files of one export may take together. Past it the zip carries the words and the list of files
only: a download of several gigabytes to a phone is a failure that looks like a hang."""

TALK = "agent_talk"
GUEST = "guest_link"

TALK_FIELDS = (
    "host", "topic", "mode", "state", "reason", "review", "approval", "sends", "sends_limit", "wake_turns", "wake_usd", "retention_days", "max_text", "file_mb", "day_mb",
    "created_at", "last_at", "closed_at",
)
"""What a conversation of ours may show of its row. Left out on purpose: ``token`` (the bearer the door gave us),
``invite_hash``, ``base_url`` (the address the invite came with), and the reader's bookkeeping."""
GUEST_KEPT_FIELDS = ("label", "topic", "mode", "tools", "usd_limit", "created_at", "revoked_at")
"""What this side keeps of a guest link. The note to the agent stays out: it is the operator's instruction to their
own agent, not part of what was said."""
GUEST_FIELDS = (
    "state", "approval", "usd_limit", "turns_per_day", "turns_total", "conversation_hours", "idle_hours", "retention_days", "file_mb", "day_mb", "spent_usd", "turns_used",
    "invite_expires_at",
)
"""What the door's view of a link may give. The briefing of a stand-in, the guest's description of its own client and
the door's events stay out."""
GUEST_CONVERSATION_FIELDS = ("state", "reason", "created_at", "expires_at", "last_guest_at")

GUEST_ROLES = {"guest": "them", "agent": "us", "door": "door", "owner": "owner"}
"""The door's roles in the words of this side: the guest is "them", whoever answered for the chat is "us"."""

WORDS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Conversation with {label}", "kind.agent_talk": "our agent at another agent's door", "kind.guest_link": "a guest's agent at our door",
        "kind": "Kind", "host": "Door", "topic": "Topic", "state": "State", "opened": "Opened", "closed": "Closed", "exported": "Exported",
        "messages": "Messages", "tasks": "Tasks", "files": "Files", "none": "None.", "task": "task", "result": "result", "in_zip": "in the zip as", "not_kept": "not kept on this machine", "kept_out": "kept here, left out of the zip",
        "door_unread": "The door's messages could not be read ({why}), so this holds only what this side keeps: the files and the tasks.",
        "left_out": "The kept files take {size} together, more than the {bound} an export carries, so none of them is in the zip. Each is listed with its sha256.",
        "cut": "The rest of this conversation did not fit in a note. The zip export holds all of it.",
        "us": "us", "them": "them", "door": "door", "owner": "owner", "in": "in", "out": "out", "mine": "ours", "theirs": "theirs",
    },
    "ru": {
        "title": "Разговор с {label}", "kind.agent_talk": "наш агент у чужой двери", "kind.guest_link": "агент гостя у нашей двери",
        "kind": "Вид", "host": "Дверь", "topic": "Тема", "state": "Состояние", "opened": "Начат", "closed": "Закрыт", "exported": "Выгружен",
        "messages": "Сообщения", "tasks": "Задачи", "files": "Файлы", "none": "Нет.", "task": "задача", "result": "результат", "in_zip": "в архиве как", "not_kept": "на этой машине не хранится", "kept_out": "хранится здесь, в архив не вошёл",
        "door_unread": "Сообщения двери прочитать не удалось ({why}), поэтому здесь только то, что хранит эта сторона: файлы и задачи.",
        "left_out": "Сохранённые файлы вместе занимают {size}, больше {bound}, которые вмещает выгрузка, поэтому в архиве их нет. Каждый перечислен с его sha256.",
        "cut": "Остаток разговора не поместился в заметку. В выгрузке архивом он есть целиком.",
        "us": "мы", "them": "они", "door": "дверь", "owner": "владелец", "in": "входящий", "out": "исходящий", "mine": "наша", "theirs": "их",
    },
}


def words(language: str) -> dict[str, str]:
    return WORDS.get(language, WORDS["en"])


def stamp(value: Any) -> str:
    """A moment as ISO 8601 in UTC, whichever way it was stored: this side writes ISO strings, the door seconds."""
    if value is None or value == "":
        return ""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC).isoformat(timespec="seconds")
    text = str(value)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).isoformat(timespec="seconds")


def shown(value: Any) -> str:
    """A moment as a person reads it in the transcript."""
    text = stamp(value)
    return text.replace("T", " ").replace("+00:00", " UTC") if text else "—"


def size_text(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    amount = size / 1024
    for unit in ("KB", "MB"):
        if amount < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GB"


def _get(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def pick(row: Any, names: Iterable[str]) -> dict[str, Any]:
    """Only the named fields of a row, each one present even when the row has no such column."""
    return {name: _get(row, name) for name in names}


def _file(row: Any, key: str) -> dict[str, Any]:
    return {
        "key": key, "name": link_protocol.safe_name(_get(row, "name")), "size": int(_get(row, "size") or 0), "sha256": str(_get(row, "sha256") or ""),
        "direction": str(_get(row, "direction") or ""), "state": str(_get(row, "state") or ""), "task": str(_get(row, "task") or ""), "at": stamp(_get(row, "created_at")),
        "path": str(_get(row, "path") or ""),
    }


def _task(row: Any) -> dict[str, Any]:
    return {
        "task": str(_get(row, "task") or ""), "direction": str(_get(row, "direction") or ""), "text": str(_get(row, "text") or ""), "state": str(_get(row, "state") or ""),
        "result": str(_get(row, "result") or ""), "created_at": stamp(_get(row, "created_at")), "updated_at": stamp(_get(row, "updated_at")),
    }


def _stamped(fields: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        if name in fields:
            fields[name] = stamp(fields[name]) or None
    return fields


def talk_record(talk: Any, messages: Iterable[Any], tasks: Iterable[Any], files: Iterable[Any]) -> dict[str, Any]:
    """A conversation of ours at another door, from its row, its messages in order, and its ``link_files`` and
    ``link_tasks`` rows. A message names its files through ``link_files.message_id``."""
    rows = list(files)
    kept = [_file(row, f"f{_get(row, 'id')}") for row in rows]
    carried: dict[Any, list[str]] = {}
    for row, item in zip(rows, kept, strict=True):
        if _get(row, "message_id") is not None:
            carried.setdefault(_get(row, "message_id"), []).append(item["key"])
    conversation = _stamped(pick(talk, TALK_FIELDS), "created_at", "last_at", "closed_at")
    if conversation["approval"] is not None:
        conversation["approval"] = bool(conversation["approval"])
    return {
        "kind": TALK, "id": str(_get(talk, "id")), "session_id": str(_get(talk, "session_id")), "label": str(_get(talk, "host") or ""), "conversation": conversation,
        "messages": [
            {
                "at": stamp(_get(row, "created_at")), "who": str(_get(row, "role") or ""), "kind": str(_get(row, "kind") or link_protocol.TEXT), "task": str(_get(row, "task") or ""),
                "task_state": str(_get(row, "task_state") or ""), "state": str(_get(row, "state") or ""), "note": str(_get(row, "note") or ""), "text": str(_get(row, "text") or ""),
                "files": carried.get(_get(row, "id"), []),
            }
            for row in messages
        ],
        "tasks": [_task(row) for row in tasks], "files": kept, "door_unread": "",
    }


def guest_record(known: Any, door_link: dict[str, Any] | None, tasks: Iterable[Any], files: Iterable[Any], *, door_error: str = "") -> dict[str, Any]:
    """A guest's conversation at our door: this side's ``guest_links`` row, the door's full view of the link (``None``
    when the door could not be asked, with ``door_error`` saying why), and this side's ``link_files`` and
    ``link_tasks`` rows.

    A door message names its files by the door's ids. One this side fetched or sent is its own row, found by
    ``remote_id``; one the door still names and this side never kept is listed from the door's word, with no bytes."""
    rows = list(files)
    kept = [_file(row, f"f{_get(row, 'id')}") for row in rows]
    by_remote = {str(_get(row, "remote_id")): item["key"] for row, item in zip(rows, kept, strict=True) if _get(row, "remote_id")}
    conversation = _stamped(pick(known, GUEST_KEPT_FIELDS), "created_at", "revoked_at")
    messages: list[dict[str, Any]] = []
    if door_link is not None:
        conversation.update(_stamped(pick(door_link, GUEST_FIELDS), "invite_expires_at"))
        conversation["opened_at"] = stamp(door_link.get("created_at")) or None
        talk = door_link.get("conversation") or {}
        conversation["conversation"] = _stamped(pick(talk, GUEST_CONVERSATION_FIELDS), "created_at", "expires_at", "last_guest_at") if talk else None
        for item in door_link.get("files") or []:
            remote = str(item.get("id") or "")
            if remote and remote not in by_remote:
                listed = _file({**item, "direction": "in" if item.get("from") == "guest" else "out", "path": ""}, f"d{remote}")
                kept.append(listed)
                by_remote[remote] = listed["key"]
        for item in door_link.get("messages") or []:
            messages.append({
                "at": stamp(item.get("at")), "who": GUEST_ROLES.get(str(item.get("role") or ""), str(item.get("role") or "")), "kind": str(item.get("kind") or link_protocol.TEXT),
                "task": str(item.get("task") or ""), "task_state": str(item.get("task_state") or ""), "state": str(item.get("state") or ""), "note": "", "text": str(item.get("text") or ""),
                "files": [by_remote[str(remote)] for remote in item.get("files") or [] if str(remote) in by_remote],
            })
    label = str(_get(known, "label") or "") or str(_get(known, "topic") or "") or str(_get(known, "id") or "")
    return {
        "kind": GUEST, "id": str(_get(known, "id")), "session_id": str(_get(known, "session_id")), "label": label, "conversation": conversation, "messages": messages,
        "tasks": [_task(row) for row in tasks], "files": kept, "door_unread": door_error or ("" if door_link is not None else "unknown"),
    }


# -- what goes into the export ----------------------------------------------------------------------------


@dataclass
class Export:
    """A record made ready: the words, the data, and the files to put into the zip by the names they go under."""

    transcript: str
    document: dict[str, Any]
    entries: list[tuple[str, Path]] = field(default_factory=list)


def _kept_path(item: dict[str, Any]) -> Path | None:
    """The file's bytes on this machine, when they are still the bytes the conversation carried: a plain file (a link
    the agent left in its place could name any file here) of the size recorded."""
    if not item["path"]:
        return None
    try:
        found = os.lstat(item["path"])
    except OSError:
        return None
    return Path(item["path"]) if stat.S_ISREG(found.st_mode) and found.st_size == item["size"] else None


def zip_names(names: Iterable[str]) -> list[str]:
    """Each name as it goes under ``files/`` in the zip: never a path, never hidden, and never the same as another's
    in any case, since the archive is unpacked on systems that do not tell ``A`` from ``a``."""
    taken: set[str] = set()
    out: list[str] = []
    for name in names:
        base = link_protocol.safe_name(name)
        stem, dot, suffix = base.rpartition(".")
        stem, suffix = (stem, f".{suffix}") if dot and stem else (base, "")
        candidate, number = base, 2
        while candidate.lower() in taken:
            candidate = f"{stem} ({number}){suffix}"
            number += 1
        taken.add(candidate.lower())
        out.append(f"files/{candidate}")
    return out


def prepare(record: dict[str, Any], *, language: str = "en", now: datetime | None = None, bound: int = FILES_BOUND_BYTES) -> Export:
    """The transcript, the data and the zip's entries of one record. Files whose bytes are gone or changed are listed
    and said to be so, never stood in for; when the kept ones together pass ``bound``, none goes in the zip."""
    moment = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    present = [(item, path) for item in record["files"] if (path := _kept_path(item)) is not None]
    total = sum(item["size"] for item, _ in present)
    left_out = bool(present) and total > bound
    placed: dict[str, str] = {}
    entries: list[tuple[str, Path]] = []
    if not left_out:
        for (item, path), name in zip(present, zip_names(item["name"] for item, _ in present), strict=True):
            placed[item["key"]] = name
            entries.append((name, path))
    names = {item["key"]: item["name"] for item in record["files"]}
    kept = {item["key"] for item, _ in present}
    document = {
        "kind": record["kind"], "label": record["label"], "exported_at": moment, "conversation": record["conversation"],
        "door_unread": record["door_unread"] or None, "files_left_out": {"bytes": total, "bound": bound} if left_out else None,
        "messages": [{**{name: message[name] for name in ("at", "who", "kind", "task", "task_state", "state", "note", "text")}, "files": [names[key] for key in message["files"]]} for message in record["messages"]],
        "tasks": record["tasks"],
        "files": [
            {**{name: item[name] for name in ("name", "size", "sha256", "direction", "state", "task", "at")}, "kept": item["key"] in kept, "in_zip": placed.get(item["key"])}
            for item in record["files"]
        ],
    }
    return Export(transcript(record, document, language=language), document, entries)


def _quoted(text: str) -> str:
    """A message's words as a quote: whatever markdown the other side wrote stays inside it and cannot pass for a
    heading of the transcript."""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines()) or ">"


def transcript(record: dict[str, Any], document: dict[str, Any], *, language: str = "en") -> str:
    """The conversation as markdown a person reads: what it was, then every message in order, its tasks and its files.
    ``document`` is what :func:`prepare` made of the record, which says where each file went."""
    say = words(language)
    conversation = record["conversation"]
    lines = [f"# {say['title'].format(label=record['label'] or '—')}", ""]
    lines.append(f"- {say['kind']}: {say['kind.' + record['kind']]}")
    if record["kind"] == TALK:
        lines.append(f"- {say['host']}: {conversation.get('host') or '—'}")
    if conversation.get("topic"):
        lines.append(f"- {say['topic']}: {' '.join(str(conversation['topic']).split())}")
    talk = conversation.get("conversation") or {}
    state = talk.get("state") or conversation.get("state") or "—"
    reason = talk.get("reason") or conversation.get("reason") or ""
    lines.append(f"- {say['state']}: {state}{f' ({reason})' if reason else ''}")
    lines.append(f"- {say['opened']}: {shown(talk.get('created_at') or conversation.get('created_at'))}")
    closed = conversation.get("closed_at") or conversation.get("revoked_at")
    if closed:
        lines.append(f"- {say['closed']}: {shown(closed)}")
    lines.append(f"- {say['exported']}: {shown(document['exported_at'])}")
    lines.append("")
    if record["door_unread"]:
        lines += [f"> {say['door_unread'].format(why=' '.join(record['door_unread'].split()))}", ""]
    if document["files_left_out"]:
        lines += [f"> {say['left_out'].format(size=size_text(document['files_left_out']['bytes']), bound=size_text(document['files_left_out']['bound']))}", ""]
    names = {item["key"]: item for item in record["files"]}
    lines += [f"## {say['messages']}", ""]
    if not record["messages"]:
        lines += [say["none"], ""]
    for message in record["messages"]:
        head = [f"**{shown(message['at'])} · {say.get(message['who'], message['who'])}**", message["kind"]]
        if message["task"]:
            head.append(f"{say['task']} `{message['task']}`" + (f" ({message['task_state']})" if message["task_state"] else ""))
        if message["state"]:
            head.append(message["state"])
        lines.append(" · ".join(head))
        carried = [names[key] for key in message["files"] if key in names]
        if message["text"]:
            lines.append(_quoted(message["text"]))
            if carried:
                # Without the blank line markdown reads the next line as more of the quote.
                lines.append("")
        if carried:
            lines.append(f"{say['files']}: " + ", ".join(f"{item['name']} ({size_text(item['size'])})" for item in carried))
        lines.append("")
    lines += [f"## {say['tasks']}", ""]
    if not record["tasks"]:
        lines += [say["none"], ""]
    for task in record["tasks"]:
        lines.append(f"- `{task['task']}` · {say.get(task['direction'], task['direction'])} · {task['state']} — {' '.join(task['text'].split())}")
        if task["result"]:
            lines.append(f"  {say['result']}: {' '.join(task['result'].split())}")
    if record["tasks"]:
        lines.append("")
    lines += [f"## {say['files']}", ""]
    if not record["files"]:
        lines += [say["none"], ""]
    for item, entry in zip(record["files"], document["files"], strict=True):
        where = f"{say['in_zip']} `{entry['in_zip']}`" if entry["in_zip"] else say["kept_out"] if entry["kept"] else say["not_kept"]
        lines.append(f"- {item['name']} · {size_text(item['size'])} · {say.get(item['direction'], item['direction'])} · {item['state']} · sha256 `{item['sha256'] or '—'}` · {where}")
    return "\n".join(lines).rstrip() + "\n"


def write_zip(export: Export, target: Path) -> None:
    """The export as one zip at ``target``: ``transcript.md``, ``conversation.json`` and ``files/``. The files go in
    as they are; a picture or an archive gains nothing from being squeezed again."""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("transcript.md", export.transcript)
        archive.writestr("conversation.json", json.dumps(export.document, ensure_ascii=False, indent=2))
        for name, path in export.entries:
            archive.write(path, name, compress_type=zipfile.ZIP_STORED)


def note_title(record: dict[str, Any], *, language: str = "en", now: datetime | None = None, limit: int = 200) -> str:
    day = (now or datetime.now(UTC)).date().isoformat()
    tail = f" — {day}"
    title = words(language)["title"].format(label=" ".join(record["label"].split()) or "—")
    return (title if len(title) + len(tail) <= limit else title[: limit - len(tail) - 1] + "…") + tail


def note_body(text: str, *, language: str = "en", limit: int) -> str:
    """The transcript as a note's body, cut at a line when it is longer than a note may be, and said to be cut."""
    if len(text) <= limit:
        return text
    tail = f"\n\n> {words(language)['cut']}\n"
    head = text[: limit - len(tail)]
    return head[: head.rfind("\n") + 1 or len(head)].rstrip() + tail


def download_name(record: dict[str, Any], *, now: datetime | None = None) -> str:
    """The zip's file name: the label in letters, digits and dashes, and the day."""
    label = "-".join("".join(character if character.isalnum() else " " for character in record["label"]).split())[:60] or record["id"]
    return f"conversation-{label}-{(now or datetime.now(UTC)).date().isoformat()}.zip"


__all__ = [
    "FILES_BOUND_BYTES", "GUEST", "TALK", "Export", "download_name", "guest_record", "note_body", "note_title", "prepare", "stamp", "talk_record", "transcript", "write_zip", "zip_names",
]
