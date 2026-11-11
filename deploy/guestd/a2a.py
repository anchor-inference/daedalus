"""A2A: the door's second entrance, for agents that speak the Agent2Agent protocol (1.0, JSON-RPC binding).

Agents built on frameworks that speak A2A arrive with a client that reads an agent card, sends ``SendMessage``
and follows a task, and never reads a page of instructions. This is a translation of that protocol onto the
mailbox and nothing more: a message sent here is a guest's message there, with the same limits, the same level
rules and the same words, and a conversation is one conversation whichever entrance a guest uses.

What maps onto what:

- A conversation is A2A's context. Its token is the bearer the card's security scheme asks for, got once from
  the redeem, as on the page: A2A says how to present a credential, not how to obtain one.
- On a direct link a message with no ``taskId`` sets a task, so the agent's turn on it ends the task done, needing
  input or refused, as on the plain door. A message naming a task that needs input answers it.
- Where the door takes no tasks (a stand-in, or a conversation the owner has not admitted yet) a message is a
  task of one message: submitted while it waits, working while it is answered, completed with the answer.
  Its id is ``m_<message>`` and the replies the door wrote for it carry ``answers``.
- Text parts are the message's text, data parts are added to it as JSON, a ``raw`` part up to
  :data:`INLINE_FILE_BYTES` becomes a file of the conversation's, and a ``url`` part may only name a file already
  uploaded here in pieces: this process has no network and fetches nothing. A file the agent sends is a ``url``
  part pointing at the door's own download, which needs the same bearer.

Push notifications are refused (``-32003``): they would need this process to call out, and it has no network on
purpose. A client follows a task with ``SendStreamingMessage``, ``SubscribeToTask`` or ``GetTask`` instead.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .files import CHUNK_BYTES, FileRefused
from .mailbox import CLOSED_WORDS, MAX_TEXT, OWNER_OFFLINE, STOP, TEXT_ONLY, Answer, Mailbox
from .store import (
    ACCEPTED,
    AGENT,
    ANSWERED,
    BY_OWNER,
    CANCELLED,
    CLAIMED,
    CLOSED,
    DONE,
    DOOR,
    FAILED,
    FILE,
    GUEST,
    HELD,
    NEEDS_INPUT,
    OPEN_TASK_STATES,
    PENDING,
    QUEUED,
    REFUSED_TASK,
    STAND_IN,
    STATUS,
    TASK,
    TEXT,
    WORKING,
)
from .text import one_line

PROTOCOL_VERSION = "1.0"
RPC_PATH = "/a/rpc"
CARD_PATH = re.compile(r"^/a/([A-Za-z0-9_-]{32,64})/(?:\.well-known/)?agent-card\.json$")
"""A link's card. Under the invite, because a card describes one link and the door holds many: A2A's well-known
place is a host's root, and a client handed ``<base>/a/<invite>`` as the agent's address looks there."""
LIMITS_EXTENSION = "urn:daedalus:agent-link:limits:1"

INLINE_FILE_BYTES = 32 * 1024
"""A file sent inside a message. The whole request has to pass the front's and the tunnel's 64 KiB, base64 adds a
third, and the text may be long: a larger file goes up in pieces first and is named here by its address."""
MAX_PARTS = 20
HOLD_SECONDS = 25.0
"""How long a blocking ``SendMessage`` waits for the task to settle before answering with where it is. Under the
front's 45 s for a whole request, which a plain answer cannot outlive."""
STREAM_SECONDS = 540.0
"""How long one stream follows a task. The front gives a stream ten minutes; a client that is still waiting after
this subscribes again, which the spec has it do after any closed connection."""
LOOK_SECONDS = 2.0
"""How often a held answer or a stream looks again without being woken. A wake can be missed while a stream is
writing, and a claim by the stand-in wakes nobody."""
KEEPALIVE_SECONDS = 15.0
"""A comment line on a quiet stream: nginx's example times out a read after 50 s, and so do other proxies."""
MESSAGE_ID_CHARS = 128

USER, AGENT_ROLE = "ROLE_USER", "ROLE_AGENT"
SUBMITTED, A2A_WORKING, COMPLETED, A2A_FAILED, CANCELED, INPUT_REQUIRED, REJECTED, AUTH_REQUIRED = (
    "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED", "TASK_STATE_AUTH_REQUIRED",
)
SETTLED = frozenset({COMPLETED, A2A_FAILED, CANCELED, REJECTED, INPUT_REQUIRED, AUTH_REQUIRED})
"""Where a stream ends and a blocking send answers: a terminal state, or one that waits for the client."""
TERMINAL = frozenset({COMPLETED, A2A_FAILED, CANCELED, REJECTED})
TASK_STATE = {ACCEPTED: SUBMITTED, WORKING: A2A_WORKING, NEEDS_INPUT: INPUT_REQUIRED, DONE: COMPLETED, CANCELLED: CANCELED}
MESSAGE_STATE = {HELD: SUBMITTED, QUEUED: SUBMITTED, CLAIMED: A2A_WORKING, ANSWERED: COMPLETED, FAILED: A2A_FAILED}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, PUSH_NOT_SUPPORTED, UNSUPPORTED, CONTENT_TYPE, VERSION_NOT_SUPPORTED = -32001, -32002, -32003, -32004, -32005, -32009
REFUSED = -32000
"""The door's own refusals, in JSON-RPC's range for a server: a limit, a closed conversation, a busy one. The A2A
codes have no word for these, and ``data`` says which it is and what to do."""
ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"

OLD_METHODS = frozenset({
    "message/send", "message/stream", "tasks/get", "tasks/list", "tasks/cancel", "tasks/resubscribe", "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get", "tasks/pushNotificationConfig/list", "tasks/pushNotificationConfig/delete", "agent/getAuthenticatedExtendedCard",
})
"""The method names of A2A 0.3. Named so a client of that version hears which version this is, not "no such method"."""
PUSH_METHODS = frozenset({"CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig"})
STREAMING = frozenset({"SendStreamingMessage", "SubscribeToTask"})
SENDING = frozenset({"SendMessage", "SendStreamingMessage"})
NO_PUSH = "This door cannot call anyone back: it has no network. Follow a task with SendStreamingMessage, SubscribeToTask or GetTask."


class RpcError(Exception):
    """A JSON-RPC error, with the door's own reason and words in ``data`` for a client that reads them."""

    def __init__(self, code: int, message: str, *, reason: str = "", retry_after: int | None = None, **metadata: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.retry_after = retry_after
        self.metadata = metadata

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason:
            metadata = {**self.metadata, **({"retryAfter": str(self.retry_after)} if self.retry_after is not None else {})}
            out["data"] = [{"@type": ERROR_INFO, "reason": self.reason, "domain": "agent-link", "metadata": metadata}]
        return out


@dataclass(slots=True)
class Call:
    """One JSON-RPC request, read and checked."""

    method: str
    params: dict[str, Any]
    id: Any = None
    notification: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def envelope(call_id: Any, *, result: Any = None, error: RpcError | None = None) -> dict[str, Any]:
    if error is not None:
        return {"jsonrpc": "2.0", "id": call_id, "error": error.body()}
    return {"jsonrpc": "2.0", "id": call_id, "result": result}


def parse(raw: bytes) -> Call:
    """A request as JSON-RPC 2.0 has it. Raises :class:`RpcError` with the id, when there was one, in ``extra``."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise RpcError(PARSE_ERROR, "This is not JSON.") from None
    if isinstance(body, list):
        # A batch would be a way round the per-call rate limit, and no A2A client sends one.
        raise RpcError(INVALID_REQUEST, "Send one request at a time; batches are not taken here.")
    if not isinstance(body, dict):
        raise RpcError(INVALID_REQUEST, "A request is a JSON object.")
    call_id = body.get("id")
    error_id = call_id if isinstance(call_id, str | int) and not isinstance(call_id, bool) else None
    if body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str) or (call_id is not None and error_id is None):
        error = RpcError(INVALID_REQUEST, 'A request has "jsonrpc": "2.0", a string "method" and an "id" that is a string or a number.')
        error.metadata["id"] = json.dumps(error_id)
        raise error
    params = body.get("params", {})
    if not isinstance(params, dict):
        error = RpcError(INVALID_PARAMS, "params is an object.")
        error.metadata["id"] = json.dumps(error_id)
        raise error
    return Call(body["method"], params, error_id, notification="id" not in body)


def version_refused(asked: str) -> RpcError | None:
    """The version a client asked for, when it is not this one. A client that names none is taken at its word that it
    speaks what its method names say: a 1.0 method from a client that forgot the header is still a 1.0 call."""
    if not asked:
        return None
    if ".".join(asked.strip().split(".")[:2]) == PROTOCOL_VERSION:
        return None
    return RpcError(VERSION_NOT_SUPPORTED, f"This door speaks A2A {PROTOCOL_VERSION} only.", reason="VERSION", supported=PROTOCOL_VERSION)


def stamp(at: float) -> str:
    return datetime.fromtimestamp(float(at), UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def loaded(data: str) -> dict[str, Any]:
    try:
        value = json.loads(data) if data else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def answer_error(answer: Answer) -> RpcError:
    """The mailbox's refusal as A2A hears it: the same words, under the code a client can act on."""
    body = answer.body
    detail = str(body.get("detail") or "Refused.")
    status = answer.status
    if status == 403 and detail == TEXT_ONLY:
        return RpcError(CONTENT_TYPE, detail, reason="TEXT_ONLY")
    if status == 409 and body.get("task"):
        return RpcError(UNSUPPORTED, detail, reason="TASK_SETTLED", state=str(body.get("state") or ""))
    if status in (400, 404, 413) or (status == 409 and body.get("status") != "pending"):
        return RpcError(INVALID_PARAMS, detail, reason=str(body.get("code") or body.get("status") or "INVALID").upper())
    reason = {"limit": "LIMIT", "closed": "CLOSED", "busy": "BUSY", "pending": "PENDING", "already_redeemed": "CLOSED"}.get(str(body.get("status")), "REFUSED")
    extra = {"limit": str(body["limit"])} if body.get("limit") else {}
    return RpcError(REFUSED, detail, reason=reason, retry_after=answer.retry_after, stopPolling=json.dumps(bool(body.get("stop_polling"))), **extra)


class A2A:
    """The translation, over one mailbox. Every method takes a conversation the caller has already found by its token."""

    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.store = mailbox.store

    # -- the card -------------------------------------------------------------------------------------

    def base(self) -> str:
        return self.mailbox.public_base() or ""

    def card(self, link: Any, *, invite: str = "", conversation: Any = None) -> dict[str, Any]:
        """A link's agent card: what a client needs to talk to it, and nothing of its owner's beyond the link's topic.

        The name is taken from the topic too: a link's label is the operator's own note of who it was made for."""
        base = self.base()
        direct = link["mode"] != STAND_IN
        topic = one_line(str(link["topic"] or ""), 2000) or "An agent you can ask about what its owner set it up for."
        name = topic.split(". ")[0][:80].rstrip(".") or "Agent link"
        redeem = f"POST {base}/a/{invite}/redeem" if invite else "POST <the link>/redeem"
        modes = ["text/plain", "application/json"] + (["*/*"] if direct else [])
        skills = [{
            "id": "conversation", "name": "Conversation", "description": topic, "tags": ["conversation"],
            "examples": [], "inputModes": ["text/plain", "application/json"], "outputModes": ["text/plain"],
        }]
        if direct:
            skills.append({
                "id": "task", "name": "Task",
                "description": "Set a task with files if you like; it ends completed with a result and files, needing your input, rejected or failed. Cancel with CancelTask.",
                "tags": ["task", "files"], "examples": [], "inputModes": modes, "outputModes": ["text/plain", "*/*"],
            })
        limits: dict[str, Any] = {
            "mode": "direct" if direct else "stand_in", "approval": bool(link["approval"]), "maxTextChars": MAX_TEXT, "retentionDays": float(link["retention_days"]),
            "turnsPerDay": int(link["turns_per_day"]) or None, "turnsTotal": int(link["turns_total"]) or None,
        }
        if direct:
            limits.update(fileMb=float(link["file_mb"]) or None, dayMb=float(link["day_mb"]) or None, inlineFileBytes=INLINE_FILE_BYTES, pieceBytes=CHUNK_BYTES)
        if conversation is not None:
            limits.update(conversation=conversation["state"], expiresAt=stamp(conversation["expires_at"]) if conversation["expires_at"] else None)
        out: dict[str, Any] = {
            "name": name, "description": topic,
            "supportedInterfaces": [{"url": f"{base}{RPC_PATH}", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}],
            "version": "1.0.0",
            "capabilities": {
                "streaming": True, "pushNotifications": False, "extendedAgentCard": True,
                "extensions": [{"uri": LIMITS_EXTENSION, "description": "This link's limits; a null is no limit.", "required": False, "params": limits}],
            },
            "securitySchemes": {"conversationToken": {"httpAuthSecurityScheme": {
                "scheme": "Bearer",
                "description": f"The conversation's token. Get it once with {redeem}: the reply's token field. It is shown only that once, so keep it; a second redeem is refused.",
            }}},
            "securityRequirements": [{"schemes": {"conversationToken": {"list": []}}}],
            "defaultInputModes": modes, "defaultOutputModes": ["text/plain"] + (["*/*"] if direct else []), "skills": skills,
        }
        if invite:
            out["documentationUrl"] = f"{base}/a/{invite}"
        return out

    # -- tasks as A2A sees them ---------------------------------------------------------------------------

    def file_part(self, file_id: str) -> dict[str, Any] | None:
        file = self.store.file(file_id)
        if file is None:
            return None
        return {
            "url": f"{self.base()}/a/files/{file['id']}/content", "filename": file["name"], "mediaType": media_type(file["name"]),
            "metadata": {"id": file["id"], "size": int(file["size"]), "sha256": file["sha256"], "state": file["state"], "pieceBytes": CHUNK_BYTES},
        }

    def parts_of(self, message: Any) -> list[dict[str, Any]]:
        """A message's words, its files as url parts, and its card (a summary, free times, a meeting) as a data part. The
        words always say what the card says, so a client that knows no cards loses nothing by skipping it."""
        data = loaded(message["data"])
        parts: list[dict[str, Any]] = [{"text": message["text"]}] if message["text"] else []
        parts += [part for part in (self.file_part(file_id) for file_id in data.get("files") or []) if part is not None]
        card = data.get("card")
        if isinstance(card, dict) and card.get("type"):
            parts.append({"data": card, "mediaType": "application/json", "metadata": {"kind": "card", "type": str(card["type"])}})
        return parts or [{"text": str(data.get("state") or "")}]

    def message_of(self, message: Any, conversation_id: str, task_id: str) -> dict[str, Any]:
        data = loaded(message["data"])
        guest = message["role"] == GUEST
        out: dict[str, Any] = {
            "messageId": str(data.get("a2a_id") or "") if guest and data.get("a2a_id") else f"{'g' if guest else 'r'}{message['id']}",
            "contextId": conversation_id, "taskId": task_id, "role": USER if guest else AGENT_ROLE, "parts": self.parts_of(message),
        }
        if message["role"] == DOOR:
            out["metadata"] = {"from": "door"}
        elif data.get("from") == BY_OWNER:
            # The owner's own words, not their agent's: said as the plain door says it, and signed or not as it came.
            out["metadata"] = {"from": BY_OWNER}
        return out

    def door_words(self, text: str, conversation_id: str, task_id: str, word: str) -> dict[str, Any]:
        """A status message the door writes for A2A alone: who is waiting for whom."""
        return {"messageId": f"door-{word}-{task_id}", "contextId": conversation_id, "taskId": task_id, "role": AGENT_ROLE, "parts": [{"text": text}], "metadata": {"from": "door"}}

    def task(self, conversation: Any, task_id: str, *, history: int | None = None, artifacts: bool = True) -> dict[str, Any] | None:
        """The task ``task_id`` of this conversation in A2A's shape, or ``None`` when it has no such task."""
        if task_id.startswith("t_"):
            row = self.store.task(task_id)
            if row is None or row["conversation_id"] != conversation["id"]:
                return None
            return self._door_task(conversation, row, history, artifacts)
        if re.fullmatch(r"m_[0-9]{1,18}", task_id):
            message = self.store.message(int(task_id[2:]))
            if message is None or message["conversation_id"] != conversation["id"] or message["role"] != GUEST or message["task_id"]:
                return None
            return self._message_task(conversation, message, history, artifacts)
        return None

    def _shaped(self, conversation: Any, task_id: str, state: str, status: dict[str, Any] | None, at: float, said: list[dict[str, Any]], made: list[dict[str, Any]],
                history: int | None, artifacts: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"id": task_id, "contextId": conversation["id"], "status": {"state": state, "timestamp": stamp(at)}}
        if status is not None:
            out["status"]["message"] = status
        if artifacts and made:
            out["artifacts"] = made
        kept = said if history is None else said[-history:] if history > 0 else []
        if kept:
            out["history"] = kept
        return out

    def _waiting_words(self, conversation: Any) -> tuple[str, str] | None:
        """What a task that waits is waiting for, when it is not the agent: the owner's admission, or their side coming back."""
        if conversation["state"] == PENDING:
            return "owner", "Kept for the owner, who decides whether to admit this conversation. It is answered once they agree."
        link = self.store.link(conversation["link_id"])
        if link is not None and self.mailbox.host_away(link):
            return "offline", OWNER_OFFLINE
        return None

    def _closed(self, conversation: Any) -> tuple[str, str]:
        reason = str(conversation["reason"] or "ended")
        state = REJECTED if reason in ("refused", "approval_timeout") else CANCELED if reason == "ended" else A2A_FAILED
        return state, f"{CLOSED_WORDS.get(reason, 'This conversation is closed.')} {STOP}"

    def _door_task(self, conversation: Any, row: Any, history: int | None, artifacts: bool) -> dict[str, Any]:
        messages = self.store.task_messages(row["id"])
        said = [self.message_of(m, conversation["id"], row["id"]) for m in messages]
        statuses = [m for m in messages if m["role"] != GUEST and m["kind"] == STATUS]
        last = statuses[-1] if statuses else None
        if row["state"] == REFUSED_TASK:
            # The door's own refusal is a turn that failed or was cut off; the agent's is a decision.
            state = A2A_FAILED if last is not None and last["role"] == DOOR else REJECTED
        else:
            state = TASK_STATE.get(row["state"], SUBMITTED)
        status = self.message_of(last, conversation["id"], row["id"]) if last is not None else None
        if conversation["state"] == CLOSED and row["state"] in OPEN_TASK_STATES:
            state, words = self._closed(conversation)
            status = self.door_words(words, conversation["id"], row["id"], "closed")
        elif state in (SUBMITTED, A2A_WORKING) and (waiting := self._waiting_words(conversation)) is not None:
            status = self.door_words(waiting[1], conversation["id"], row["id"], waiting[0])
        made = [
            {"artifactId": f"r{m['id']}", "name": "result" if loaded(m["data"]).get("state") == DONE else "files", "parts": self.parts_of(m)}
            for m in messages if m["role"] == AGENT and (loaded(m["data"]).get("files") or loaded(m["data"]).get("state") == DONE)
        ]
        return self._shaped(conversation, row["id"], state, status, float(row["updated_at"]), said, made, history, artifacts)

    def _message_task(self, conversation: Any, message: Any, history: int | None, artifacts: bool) -> dict[str, Any]:
        task_id = f"m_{message['id']}"
        replies = self.store.answers_to(conversation["id"], message["id"])
        said = [self.message_of(message, conversation["id"], task_id)] + [self.message_of(r, conversation["id"], task_id) for r in replies]
        state = MESSAGE_STATE.get(message["state"], SUBMITTED)
        last = replies[-1] if replies else None
        if state == COMPLETED and (last is None or last["role"] == DOOR):
            state = A2A_FAILED
        status = self.message_of(last, conversation["id"], task_id) if last is not None else None
        at = float(last["at"]) if last is not None else float(message["at"])
        if conversation["state"] == CLOSED and state in (SUBMITTED, A2A_WORKING):
            state, words = self._closed(conversation)
            status = self.door_words(words, conversation["id"], task_id, "closed")
        elif state in (SUBMITTED, A2A_WORKING) and (waiting := self._waiting_words(conversation)) is not None:
            status = self.door_words(waiting[1], conversation["id"], task_id, waiting[0])
        made = [{"artifactId": f"r{r['id']}", "name": "answer", "parts": self.parts_of(r)} for r in replies if r["role"] == AGENT]
        return self._shaped(conversation, task_id, state, status, at, said, made, history, artifacts)

    def tasks(self, conversation: Any, *, state: str = "", history: int = 0, artifacts: bool = False) -> list[dict[str, Any]]:
        """Every task of the conversation, the most recently moved first."""
        found = [self._door_task(conversation, row, history, artifacts) for row in self.store.tasks_of(conversation["id"])]
        found += [self._message_task(conversation, m, history, artifacts) for m in self.store.untasked_guest_messages(conversation["id"])]
        if state:
            found = [task for task in found if task["status"]["state"] == state]
        return sorted(found, key=lambda task: task["status"]["timestamp"], reverse=True)

    # -- calls ------------------------------------------------------------------------------------------

    def _task_or_error(self, conversation: Any, params: dict[str, Any], key: str = "id", **shape: Any) -> dict[str, Any]:
        task_id = params.get(key)
        if not isinstance(task_id, str) or not task_id:
            raise RpcError(INVALID_PARAMS, f"{key} is the task's id.")
        found = self.task(conversation, task_id, **shape)
        if found is None:
            raise RpcError(TASK_NOT_FOUND, "There is no such task in this conversation.", reason="NO_TASK")
        return found

    @staticmethod
    def history_of(params: dict[str, Any], key: str = "historyLength") -> int | None:
        value = params.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RpcError(INVALID_PARAMS, f"{key} is a whole number, 0 or more.")
        return value

    def get_task(self, conversation: Any, params: dict[str, Any]) -> dict[str, Any]:
        return self._task_or_error(conversation, params, history=self.history_of(params))

    def list_tasks(self, conversation: Any, params: dict[str, Any]) -> dict[str, Any]:
        context = params.get("contextId")
        if context and context != conversation["id"]:
            raise RpcError(INVALID_PARAMS, "contextId names another conversation: this token speaks for one.", reason="CONTEXT")
        size = params.get("pageSize", 50)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 100:
            raise RpcError(INVALID_PARAMS, "pageSize is from 1 to 100.")
        token = str(params.get("pageToken") or "0")
        start = int(token) if token.isdigit() and len(token) < 9 else 0
        state = str(params.get("status") or "")
        if state == "TASK_STATE_UNSPECIFIED":
            state = ""
        every = self.tasks(conversation, state=state, history=self.history_of(params) or 0, artifacts=bool(params.get("includeArtifacts")))
        page = every[start:start + size]
        following = start + size if start + size < len(every) else 0
        return {"tasks": page, "nextPageToken": str(following) if following else "", "pageSize": size, "totalSize": len(every)}

    def cancel_task(self, conversation: Any, params: dict[str, Any]) -> dict[str, Any]:
        found = self._task_or_error(conversation, params)
        if found["id"].startswith("m_") or found["status"]["state"] in TERMINAL:
            raise RpcError(TASK_NOT_CANCELABLE, f"That task is {found['status']['state']}: there is nothing to cancel." if found["status"]["state"] in TERMINAL else
                           "A message is answered as it comes and cannot be called back once sent.", reason="NOT_CANCELABLE")
        if conversation["state"] == CLOSED:
            raise RpcError(REFUSED, self._closed(conversation)[1], reason="CLOSED")
        row = self.store.task(found["id"])
        assert row is not None
        self.mailbox.cancel_task(conversation, row, by=GUEST)
        refreshed = self.task(conversation, found["id"])
        assert refreshed is not None
        return refreshed

    def extended_card(self, conversation: Any) -> dict[str, Any]:
        link = self.store.link(conversation["link_id"])
        assert link is not None
        return self.card(link, conversation=conversation)

    # -- sending ------------------------------------------------------------------------------------------

    def send(self, token: str, conversation: Any, params: dict[str, Any]) -> tuple[str, bool, int | None]:
        """Send what a ``SendMessage`` carries. Returns the task it went to, whether to answer at once rather than wait
        for the task to settle, and how much history the answer wants."""
        message = params.get("message")
        if not isinstance(message, dict):
            raise RpcError(INVALID_PARAMS, "params.message is the message to send.")
        configuration = params.get("configuration") or {}
        if not isinstance(configuration, dict):
            raise RpcError(INVALID_PARAMS, "configuration is an object.")
        if configuration.get("taskPushNotificationConfig"):
            raise RpcError(PUSH_NOT_SUPPORTED, NO_PUSH, reason="NO_PUSH")
        history = self.history_of(configuration)
        if message.get("role") != USER:
            raise RpcError(INVALID_PARAMS, f'A message to this agent has "role": "{USER}".')
        message_id = message.get("messageId")
        if not isinstance(message_id, str) or not 0 < len(message_id) <= MESSAGE_ID_CHARS:
            raise RpcError(INVALID_PARAMS, f"messageId is a string of 1 to {MESSAGE_ID_CHARS} characters, unique to the message.")
        context = message.get("contextId")
        if context and context != conversation["id"]:
            raise RpcError(INVALID_PARAMS, f"contextId names another conversation. This token speaks for {conversation['id']}; leave contextId out or send that.", reason="CONTEXT")
        # A client that did not hear the answer sends the same message again: it gets the task the first one went to.
        again = self.store.guest_message_by_mark(conversation["id"], "a2a_id", message_id)
        if again is not None:
            return (again["task_id"] or f"m_{again['id']}"), bool(configuration.get("returnImmediately")), history
        parts = message.get("parts")
        if not isinstance(parts, list) or not 0 < len(parts) <= MAX_PARTS or not all(isinstance(part, dict) for part in parts):
            raise RpcError(INVALID_PARAMS, f"parts is a list of 1 to {MAX_PARTS} parts.")
        link = self.store.link(conversation["link_id"])
        assert link is not None
        if conversation["state"] == CLOSED:
            raise RpcError(REFUSED, self._closed(conversation)[1], reason="CLOSED", stopPolling="true")
        task_ref = message.get("taskId") or ""
        if task_ref:
            found = self.task(conversation, str(task_ref))
            if found is None:
                raise RpcError(TASK_NOT_FOUND, "There is no such task in this conversation.", reason="NO_TASK")
            if found["status"]["state"] in TERMINAL:
                raise RpcError(UNSUPPORTED, f"That task is {found['status']['state']} and takes no more messages. Send without taskId to start another.", reason="TASK_SETTLED")
            if task_ref.startswith("m_"):
                raise RpcError(UNSUPPORTED, "That task is one message waiting for its answer. Send without taskId to say more.", reason="ONE_MESSAGE")
        text, files = self._parts_in(token, conversation, link, parts)
        if task_ref:
            body: dict[str, Any] = {"kind": TEXT if text else FILE, "text": text, "task": task_ref}
        elif link["mode"] == STAND_IN or conversation["state"] == PENDING:
            body = {"kind": TEXT, "text": text}
        else:
            body = {"kind": TASK, "text": text or "Files: " + ", ".join(str(self.store.file(f)["name"]) for f in files)}
        if files:
            body["files"] = files
        answer = self.mailbox.send(token, body, extra={"a2a_id": message_id})
        if answer is None:
            raise RpcError(REFUSED, "The token is not valid here.", reason="TOKEN")
        if answer.status >= 300:
            raise answer_error(answer)
        if task_ref:
            return str(task_ref), bool(configuration.get("returnImmediately")), history
        if answer.body.get("task"):
            return str(answer.body["task"]), bool(configuration.get("returnImmediately")), history
        if answer.body.get("id"):
            return f"m_{answer.body['id']}", bool(configuration.get("returnImmediately")), history
        # Taken as a refusal: a second message before the owner admitted the first is not kept anywhere.
        raise answer_error(Answer(409, answer.body, answer.retry_after))

    def _parts_in(self, token: str, conversation: Any, link: Any, parts: list[dict[str, Any]]) -> tuple[str, list[str]]:
        """The text and the files a message's parts carry. Files are checked against the link before any is kept."""
        words: list[str] = []
        files: list[str] = []
        wanted_files = [part for part in parts if "raw" in part or "url" in part]
        if wanted_files and link["mode"] == STAND_IN:
            raise RpcError(CONTENT_TYPE, TEXT_ONLY, reason="TEXT_ONLY")
        if wanted_files and conversation["state"] == PENDING:
            raise RpcError(INVALID_PARAMS, "Files are taken once the owner admits this conversation. Send text first.", reason="PENDING", retryAfter="30")
        for part in parts:
            kinds = [key for key in ("text", "raw", "url", "data") if key in part]
            if len(kinds) != 1:
                raise RpcError(INVALID_PARAMS, "Each part has exactly one of text, raw, url or data.")
            if "text" in part:
                if not isinstance(part["text"], str):
                    raise RpcError(INVALID_PARAMS, "A text part's text is a string.")
                words.append(part["text"])
            elif "data" in part:
                words.append("```json\n" + json.dumps(part["data"], ensure_ascii=False, indent=1) + "\n```")
            elif "raw" in part:
                files.append(self._inline_file(token, part))
            else:
                files.append(self._named_file(conversation, part))
        text = "\n\n".join(word for word in words if word.strip())
        if len(text) > MAX_TEXT:
            raise RpcError(INVALID_PARAMS, f"A message's text may be at most {MAX_TEXT} characters, data parts included.", reason="TOO_LONG")
        if not text.strip() and not files:
            raise RpcError(INVALID_PARAMS, "A message needs a text part, a data part or a file.")
        return text, files

    def _inline_file(self, token: str, part: dict[str, Any]) -> str:
        raw = part.get("raw")
        if not isinstance(raw, str):
            raise RpcError(INVALID_PARAMS, "raw is the file's bytes in base64.")
        if len(raw) > INLINE_FILE_BYTES * 4 // 3 + 8:
            raise RpcError(INVALID_PARAMS, self._too_big(), reason="INLINE_LIMIT")
        try:
            data = base64.b64decode(raw, validate=True) if "-" not in raw and "_" not in raw else base64.urlsafe_b64decode(raw)
        except (binascii.Error, ValueError):
            raise RpcError(INVALID_PARAMS, "raw is not base64.") from None
        if not data:
            raise RpcError(INVALID_PARAMS, "An empty file has nothing to send.")
        if len(data) > INLINE_FILE_BYTES:
            raise RpcError(INVALID_PARAMS, self._too_big(), reason="INLINE_LIMIT")
        name = str(part.get("filename") or "") or "file" + (mimetypes.guess_extension(str(part.get("mediaType") or "")) or "")
        made = self.mailbox.announce_file(token, {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if made is None or made.status >= 300:
            raise answer_error(made or Answer(401, {"detail": "The token is not valid here."}))
        put = self.mailbox.put_piece(token, made.body["id"], 0, data)
        if put is None or put.status >= 300:
            raise answer_error(put or Answer(401, {"detail": "The token is not valid here."}))
        return str(made.body["id"])

    def _too_big(self) -> str:
        return (f"A file inside a message is at most {INLINE_FILE_BYTES} bytes. Upload a larger one in pieces with POST {self.base()}/a/files and PUT (the link's page says how), "
                f"then send it as a url part: {self.base()}/a/files/<id>.")

    def _named_file(self, conversation: Any, part: dict[str, Any]) -> str:
        url = part.get("url")
        matched = re.search(r"/a/files/(f_[0-9a-f]{16})(?:/content)?$", url) if isinstance(url, str) else None
        if matched is None or not str(url).startswith((self.base() + "/a/files/", "/a/files/")):
            raise RpcError(INVALID_PARAMS, "This door has no network and fetches nothing from elsewhere. " + self._too_big(), reason="FETCH")
        try:
            file = self.mailbox.own_file(conversation, matched.group(1))
        except FileRefused as exc:
            raise RpcError(INVALID_PARAMS, exc.detail, reason=exc.code.upper()) from None
        if file["origin"] != GUEST:
            raise RpcError(INVALID_PARAMS, "That file is the other side's.", reason="NOT_YOURS")
        return str(file["id"])

    # -- following a task -----------------------------------------------------------------------------------

    async def settle(self, conversation_id: str, task_id: str, seconds: float, history: int | None = None) -> dict[str, Any]:
        """The task once it settles, or where it is after ``seconds``."""
        deadline = time.monotonic() + seconds
        while True:
            conversation = self.store.conversation(conversation_id)
            assert conversation is not None
            found = self.task(conversation, task_id, history=history)
            assert found is not None
            left = deadline - time.monotonic()
            if found["status"]["state"] in SETTLED or left <= 0:
                return found
            await self.mailbox.changes(conversation_id, min(left, LOOK_SECONDS))

    async def follow(self, conversation_id: str, task_id: str, *, seconds: float | None = None, history: int | None = None) -> AsyncIterator[dict[str, Any] | None]:
        """A task's stream: the task first, then each new artifact and each change of status, until it settles or
        ``seconds`` (:data:`STREAM_SECONDS`) pass. ``None`` is a moment to say something on a quiet line."""
        seconds = STREAM_SECONDS if seconds is None else seconds
        conversation = self.store.conversation(conversation_id)
        assert conversation is not None
        found = self.task(conversation, task_id, history=history)
        assert found is not None
        yield {"task": found}
        if found["status"]["state"] in SETTLED:
            return
        sent_status = json.dumps(found["status"].get("message"), sort_keys=True) + found["status"]["state"]
        sent_artifacts = {artifact["artifactId"] for artifact in found.get("artifacts", [])}
        started = time.monotonic()
        beat = started + KEEPALIVE_SECONDS
        while True:
            now = time.monotonic()
            if now - started >= seconds:
                return
            if now >= beat:
                beat = now + KEEPALIVE_SECONDS
                yield None
            await self.mailbox.changes(conversation_id, min(LOOK_SECONDS, max(0.0, beat - now), max(0.0, seconds - (now - started))))
            conversation = self.store.conversation(conversation_id)
            assert conversation is not None
            found = self.task(conversation, task_id, history=0)
            assert found is not None
            for artifact in found.get("artifacts", []):
                if artifact["artifactId"] not in sent_artifacts:
                    sent_artifacts.add(artifact["artifactId"])
                    yield {"artifactUpdate": {"taskId": task_id, "contextId": conversation_id, "artifact": artifact, "append": False, "lastChunk": True}}
            said = json.dumps(found["status"].get("message"), sort_keys=True) + found["status"]["state"]
            if said != sent_status:
                sent_status = said
                yield {"statusUpdate": {"taskId": task_id, "contextId": conversation_id, "status": found["status"]}}
            if found["status"]["state"] in SETTLED:
                return


__all__ = [
    "CARD_PATH", "HOLD_SECONDS", "INLINE_FILE_BYTES", "KEEPALIVE_SECONDS", "OLD_METHODS", "PROTOCOL_VERSION", "PUSH_METHODS", "RPC_PATH", "SENDING", "SETTLED", "STREAMING",
    "STREAM_SECONDS", "TERMINAL", "A2A", "Call", "RpcError", "answer_error", "envelope", "parse", "version_refused",
]
