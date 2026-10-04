"""Bounded, allowlisted projections of execution attempt faults for operator support."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTION_VERSION = 1
MAX_FAULTS = 20
_REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_STATES = frozenset({"queued", "starting", "running", "waiting", "recovering", "completed", "failed", "cancelled"})
_KINDS = frozenset({"adapter_error", "renderer_error", "launch_error", "runtime_error", "cancelled"})
_CANCELLERS = frozenset({"operator", "worker", "system"})


def _safe(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def attempt_diagnostics(attempt: Mapping[str, Any], faults: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Project one attempt without copying raw exceptions, stderr, paths or provider output.

    Callers must first authorize the exact task and attempt. Unknown fields and values are omitted,
    since even a supposedly harmless diagnostic string may contain a credential.
    """
    attempt_id = _safe(attempt.get("id"), _REFERENCE)
    task_id = _safe(attempt.get("task_id"), _REFERENCE)
    if attempt_id is None or task_id is None:
        raise ValueError("a valid attempt and task identity are required")
    state = attempt.get("state")
    if not isinstance(state, str) or state not in _STATES:
        state = "unknown"
    shown = []
    matched = 0
    for fault in faults:
        if fault.get("attempt_id") != attempt_id:
            continue
        matched += 1
        if matched > MAX_FAULTS:
            continue
        kind = fault.get("kind")
        if not isinstance(kind, str) or kind not in _KINDS:
            kind = "runtime_error"
        item: dict[str, Any] = {"kind": kind}
        # A copied provider reference can itself be a secret; only the store's integer key becomes
        # an operator handle, and the raw provider text never enters the projection.
        fault_id = fault.get("id")
        reference = f"fault-{fault_id}" if type(fault_id) is int and 0 < fault_id < 10**18 else None
        created = _safe(fault.get("created_at"), _TIME)
        if reference is not None:
            item["diagnostic_ref"] = reference
        if created is not None:
            item["created_at"] = created
        if kind == "cancelled" and isinstance(fault.get("cancelled_by"), str) and fault["cancelled_by"] in _CANCELLERS:
            item["cancelled_by"] = fault["cancelled_by"]
        shown.append(item)
    return {"attempt_id": attempt_id, "task_id": task_id, "state": state,
            "faults": shown, "redacted": True, "redaction_version": REDACTION_VERSION,
            "truncated": matched > MAX_FAULTS}


def support_bundle(attempt: Mapping[str, Any], faults: Sequence[Mapping[str, Any]]) -> bytes:
    """Return a small JSON bundle containing only the safe attempt projection."""
    return (json.dumps(attempt_diagnostics(attempt, faults), separators=(",", ":"), sort_keys=True) + "\n").encode()
