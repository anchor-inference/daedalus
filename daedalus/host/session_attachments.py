"""What was attached in a chat, as one list: the files the operator sent and the files the agent handed over.

Nothing here is stored separately. The operator's files are the ``attachments`` of their messages
(:mod:`daedalus.host.attached_files`), the agent's are its ``SendFile`` calls and the media it placed in
an answer, and each already has a route that serves its bytes; this reads them out of the transcript's
views in one place and names that route for each, in the address shape the app already fetches a file
in (``{base}/download?path=…``). ``message_seq`` is the number the app keys a turn by.
"""

from __future__ import annotations

import mimetypes
import posixpath
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

_SENT_SIZE = re.compile(r"\((\d+) bytes\)")
"""How a SendFile result states what it delivered: ``shown report.pdf (2048 bytes) …``."""


def _bytes(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _operator(session_id: str, seq: int | None, created_at: str, index: int, item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "")
    if not name:
        return None
    if item.get("kept"):
        url = f"/api/files/{quote(str(item['kept']), safe='')}/download?path={quote(name, safe='')}"
    else:
        url = f"/api/sessions/{session_id}/attachments/download?path={quote(str(item.get('file') or name), safe='')}"
    return {
        "id": f"message:{seq}:{index}", "source": "operator", "name": name, "size": _bytes(item.get("size")),
        "mime": str(item.get("mime") or "") or mimetypes.guess_type(name)[0] or "application/octet-stream",
        "message_seq": seq, "created_at": created_at, "url": url,
    }


def _sent_name(arguments: dict[str, Any]) -> str:
    path = str(arguments.get("path") or "").strip()
    # A folder sent with zip=true goes as the archive written beside it, under the folder's name.
    path = path.rstrip("/\\") + ".zip" if arguments.get("zip") is True else path
    return posixpath.basename(path.replace("\\", "/").rstrip("/"))


def listing(session_id: str, views: Iterable[dict[str, Any]], answers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The attachments of a chat, oldest message first.

    ``views`` are the stored views of the messages that may carry a file; ``answers`` are those of the
    messages that answer the ``SendFile`` calls among them. A call whose answer is an error, or that
    has none yet, handed nothing over and is left out; its size comes from the answer's own words.
    """
    results: dict[str, dict[str, Any]] = {}
    for view in answers:
        for result in view.get("tool_results") or []:
            if isinstance(result, dict) and result.get("id"):
                results[str(result["id"])] = result
    found: list[dict[str, Any]] = []
    for view in views:
        seq = view.get("seq") if isinstance(view.get("seq"), int) else None
        at = str(view.get("created_at") or "")
        if view.get("role") == "user":
            for index, item in enumerate(view.get("attachments") or []):
                if isinstance(item, dict) and (entry := _operator(session_id, seq, at, index, item)):
                    found.append(entry)
        for result in view.get("tool_results") or []:
            # A reply to a question that carried files: the operator's, though it rides on a tool result.
            for index, item in enumerate(result.get("attachments") or [] if isinstance(result, dict) else []):
                if isinstance(item, dict) and (entry := _operator(session_id, seq, at, index, item)):
                    found.append({**entry, "id": f"answer:{result.get('id')}:{index}"})
        for call in view.get("tool_calls") or []:
            if not isinstance(call, dict) or call.get("name") != "SendFile" or not isinstance(call.get("arguments"), dict):
                continue
            result = results.get(str(call.get("id")))
            name = _sent_name(call["arguments"])
            if result is None or result.get("is_error") or not name:
                continue
            size = _SENT_SIZE.search(str(result.get("content") or ""))
            found.append({
                "id": f"sent:{call['id']}", "source": "agent", "name": name, "size": int(size.group(1)) if size else None,
                "mime": mimetypes.guess_type(name)[0] or "application/octet-stream", "message_seq": seq, "created_at": at,
                "url": f"/api/sessions/{session_id}/sent/{quote(str(call['id']), safe='')}/download?path={quote(name, safe='')}",
            })
        for presentation in view.get("media") or []:
            for item in presentation.get("items") or [] if isinstance(presentation, dict) else []:
                name = str(item.get("filename") or "")
                if not name:
                    continue
                item_id = str(item.get("id") or "")
                found.append({
                    "id": f"media:{item_id}", "source": "agent", "name": name, "size": _bytes(item.get("byte_size")),
                    "mime": str(item.get("mime_type") or "") or "application/octet-stream", "message_seq": seq, "created_at": at,
                    "url": f"/api/sessions/{session_id}/media/{quote(str(presentation['id']), safe='')}/download?path={quote(item_id + '/' + name, safe='/')}",
                })
    return found


__all__ = ["listing"]
