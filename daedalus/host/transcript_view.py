"""What the Mini App shows for one transcript row.

A transcript row never changes after it is written, so neither does the shape the app
draws from it — but the shape costs about a millisecond a message to derive, nearly all of
it secret redaction, and the app asks for six hundred of them every time a run emits an
event. So the view is computed once, when the row is appended, and stored beside it.

:class:`TranscriptViewBuilder` is what does the computing, and :meth:`~TranscriptViewBuilder.key`
says which code and which redactor produced a stored view. A view whose key no longer
matches is recomputed from the message and rewritten, which is what makes a newly
configured secret reach rows written before it was known.
"""

from __future__ import annotations

import json
import re
from typing import Any

from protocore.contracts.blob import IBlobStore
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from protocore.runtime.wire_format import is_compacted_placeholder, parse_compacted_placeholder

from daedalus.host import prompts
from daedalus.host.prompts import split_headline
from daedalus.host.run_outcome import OUTCOME_METADATA_KEY
from daedalus.security import operator_secrets, redact

VIEW_VERSION = 11
"""Bumped whenever the shape below changes; stored views from an older version are recomputed. It
covers this file only — what the redactor masks is covered by the key, by value and by shape, so a
new secret format does not depend on anyone remembering this number."""

TOOL_RESULT_PREVIEW_CHARS = 400
"""Characters of a tool result a listed turn carries. The whole text is one request away
(``/api/sessions/{id}/tool-results/{call_id}``), and previews were the larger half of a
session payload while they were ten times this long."""

_SPLIT_POINTER_RE = re.compile(r"\n\[truncated \d+ chars(?:; full result at [^\]]*)?\]\Z")
"""The line the core ends a result with when it cut the text for the model and kept the whole value
beside it (``canonical_content``)."""

_SUMMARY_WRAP_RE = re.compile(r"</?compacted-turn[^>]*>")
_NUDGE_MARKERS = ("[internal control", "tool repeatedly failed with the same error", "has been disabled for the rest of this run", "The run has reached its budget")


def _looks_like_core_nudge(text: str) -> bool:
    head = text.lstrip()[:400]
    return any(marker in head for marker in _NUDGE_MARKERS)


def message_view(message: Message) -> dict[str, Any]:
    text: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in message.content_blocks:
        if isinstance(block, TextBlock):
            text.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking.append(block.text)
        elif isinstance(block, ToolUseBlock):
            try:
                args = json.loads(block.arguments_json or "{}")
            except json.JSONDecodeError:
                args = {"raw": block.arguments_json}
            tool_calls.append({"id": block.tool_call_id, "name": block.name, "arguments": redact.shared().redact_tool_data(args)})
        elif isinstance(block, ToolResultBlock):
            tool_results.append(_result_preview(block))
    compaction = message.metadata.get("daedalus.compaction") if isinstance(message.metadata, dict) else None
    is_summary = bool(message.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)) if isinstance(message.metadata, dict) else False
    body = prompts.without_host_notes("".join(text))
    if is_summary:
        body = _SUMMARY_WRAP_RE.sub("", body).strip()
        # A summary without the host's record came from the core mid-run; the host's own (auto/manual) sit between runs.
        compaction = compaction or {"reason": "core"}
    origin = message.metadata.get("daedalus.origin") if isinstance(message.metadata, dict) else None
    delivery = message.metadata.get("daedalus.delivery") if isinstance(message.metadata, dict) else None
    internal = message.role is MessageRole.user and not is_summary and (
        # The copy that opens a turn with a message queued while the turn before ran is where the model
        # read it, so it is the one shown. It used to be hidden as a repeat of the queued row — which is
        # hidden too — and an operator's message sent in the last seconds of a turn vanished from the chat.
        origin == "core" or message.metadata.get("daedalus.queued", False) or (origin != "operator" and _looks_like_core_nudge(body))
    )
    reply = message.metadata.get("daedalus.reply_to") if isinstance(message.metadata, dict) else None
    if reply and body.startswith("[In reply to: «"):
        body = body.split("\n", 1)[1] if "\n" in body else ""  # the chat draws what it answers from ``reply_to``
    handed = message.metadata.get("daedalus.secrets") if isinstance(message.metadata, dict) else None
    if handed:
        body = operator_secrets.without_attachment_lines(body)  # the chat draws a chip per name instead
    headline = ""
    if message.role is MessageRole.assistant:
        body, headline = split_headline(body)
        body = redact.redact(body)
    archived = message.metadata.get("daedalus.archived") if isinstance(message.metadata, dict) else None
    # Who really answered. Absent on every turn written before the host started recording it, and on
    # every turn that is not the model's — so the app shows the note where there is one and nothing
    # where there is not, rather than claiming the configured model for an answer it cannot vouch for.
    produced = message.metadata.get("daedalus.model") if isinstance(message.metadata, dict) else None
    produced = produced if isinstance(produced, dict) else {}
    return {
        "role": message.role.value,
        "run_id": message.metadata.get("daedalus.run_id"),
        "summary": is_summary,
        "internal": internal,
        "origin": origin or ("operator" if message.role is MessageRole.user and not internal else ""),
        "client_message_id": message.metadata.get("daedalus.client_message_id") if message.role is MessageRole.user else None,
        "client_message_ids": message.metadata.get("daedalus.client_message_ids") if message.role is MessageRole.user else None,
        "seq": message.metadata.get("daedalus.seq") if isinstance(message.metadata, dict) else None,
        "compaction": compaction,
        "archived": archived,
        "headline": headline,
        "text": body,
        "thinking": "".join(thinking) or (message.reasoning_content or ""),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "created_at": message.created_at.isoformat(),
        "model": str(produced.get("model") or ""),
        "provider": str(produced.get("provider") or ""),
        "fallback": produced.get("fallback") if isinstance(produced.get("fallback"), dict) else None,
        "media": message.metadata.get("daedalus.media", []) if isinstance(message.metadata, dict) else [],
        # The YAGNI switch this turn told the model about (``on``/``off``): the note itself is
        # in the turn context, which no reader is shown, so the app marks the message instead.
        "yagni": message.metadata.get("daedalus.yagni") if isinstance(message.metadata, dict) else None,
        # The closing line of a run that produced no answer (``run_outcome``): why it stopped and where.
        "outcome": message.metadata.get(OUTCOME_METADATA_KEY) if isinstance(message.metadata, dict) else None,
        # How an operator's message reached the model: ``steer`` placed into a turn under way,
        # ``drained`` opening the turn after the one it was written during, ``follow_up`` after it.
        "delivery": delivery if isinstance(delivery, str) else None,
        # What the message answers (a report, an event line), as the operator chose it in the chat.
        "reply_to": message.metadata.get("daedalus.reply_to") if isinstance(message.metadata, dict) else None,
        # The operator's secrets attached to the message, by name and scope; never a value.
        "secrets": handed if isinstance(handed, list) else None,
        # A member's steps for the operator the host put in the chat, drawn as a card of their own.
        "operator_steps": message.metadata.get("daedalus.operator_steps") if isinstance(message.metadata, dict) else None,
    }


def _readable_part(placeholder: str) -> str:
    """What a compaction placeholder says for a reader: the lines under its machine frame, or, in the
    older placeholders that have none, the head and tail of the output the frame carries."""
    frame, _, readable = placeholder.strip().partition("\n")
    if readable:
        return readable
    parsed = parse_compacted_placeholder(frame)
    return parsed[0].preview if parsed else ""


def _result_preview(block: ToolResultBlock) -> dict[str, Any]:
    """The listing's preview of one tool result; the full text (a skill body, a long command output) is one request away.

    Whether there is more to fetch is decided here, on the text itself: the length the app is told
    is the text's, while what it holds is a redacted preview, and a secret that redacts to something
    shorter would otherwise read as a result cut short.

    A result compaction masked holds a placeholder whose first line is a machine frame of hashes and
    base64. That frame used to be the preview, and its few hundred characters were all the expand
    button ever fetched: the original sits in the blob store, which only the endpoint reads. So the
    preview is the part written for a reader, and the length is left unknown rather than given as
    the placeholder's, which would claim the result was that short.
    """
    content = block.content
    if is_compacted_placeholder(content):
        return {"id": block.tool_call_id, "content": redact.shared().redact_tool_data(_readable_part(content)[:TOOL_RESULT_PREVIEW_CHARS]), "is_error": block.is_error, "length": None, "clipped": True}
    if block.canonical_content and _SPLIT_POINTER_RE.search(content):
        # The model was shown a first page; the listing measures what the tool returned.
        content = block.canonical_content
    return {
        "id": block.tool_call_id,
        "content": redact.shared().redact_tool_data(content[:TOOL_RESULT_PREVIEW_CHARS]),
        "is_error": block.is_error,
        "length": len(content),
        "clipped": len(content) > TOOL_RESULT_PREVIEW_CHARS,
    }


async def full_tool_result(block: ToolResultBlock, blobs: IBlobStore, tenant_id: str) -> tuple[str, bool]:
    """The whole text of one tool result, and whether it really is whole.

    The transcript holds what the model was shown, which is not always what the tool returned:
    compaction replaces an old result with a placeholder and keeps the original in the blob store,
    and the core may cut a long result to a first page and keep the rest in ``canonical_content``.
    Either way the original is what the operator asked to see. Bytes that are not UTF-8 (a binary
    file read raw) are decoded with replacement characters rather than refused, so the reader sees
    what is there. When the stored original is gone, the placeholder's readable part is returned
    and the second value says the text is not the whole result.
    """
    content = block.content
    if is_compacted_placeholder(content):
        parsed = parse_compacted_placeholder(content)
        refs = [block.canonical_ref, block.metadata.get("blob_ref"), parsed[0].blob_ref if parsed else None]
        for ref in refs:
            if not isinstance(ref, str) or not ref:
                continue
            try:
                return (await blobs.get(tenant_id, ref)).decode("utf-8", errors="replace"), True
            except Exception:  # noqa: BLE001 — a pruned or unreadable blob falls through to the next name for it
                continue
        return _readable_part(content), False
    if block.canonical_content and _SPLIT_POINTER_RE.search(content):
        return block.canonical_content, True
    return content, True


class TranscriptViewBuilder:
    """Builds and keys the stored view. Held by the store, which knows nothing else about it."""

    def key(self) -> str:
        """Version of the code and of the redactor that a stored view was produced by.

        Three parts, because there are three ways a stored view can go stale: this file's shape, the
        secret values the redactor was given, and the secret shapes the build knows. The last one
        used to ride on ``VIEW_VERSION`` being bumped by hand, and a shape added without that left
        every view already stored showing the secret it was added for.
        """
        return f"{VIEW_VERSION}.{redact.shapes_digest()}.{redact.shared().fingerprint()}"

    def build(self, message: Message) -> dict[str, Any]:
        return message_view(message)


__all__ = ["TOOL_RESULT_PREVIEW_CHARS", "VIEW_VERSION", "TranscriptViewBuilder", "full_tool_result", "message_view"]
