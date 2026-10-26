"""Where a run's memory lives: which scope Remember files under when the model names none, and what
an unscoped Recall reads.

Memory has two homes. A chat that belongs to one of the operator's projects keeps what it learns in
that project's memory; every other chat keeps it in the operator's global memory. There is no
per-chat memory: the core's own default, a session scope keyed by the session id, made every note
private to the chat that wrote it, and Recall in any other chat never saw it — which is how a
long-term memory turned out to be fifty separate short-term ones.

Not every run is the operator talking. A staff member working a project's task, a subagent, a
scheduled run, an autopilot continuation, a report waking an orchestrator: none of these is the
operator's conversation, and none of them may write into the operator's global memory. Inside a
project they write into the project's memory, which is the project's shared notebook and where the
operator reads what its team learned; outside one they have no default at all, and a Remember there
is refused with a note telling the model to put the finding in its answer, where the conversation
that asked for the work can decide whether it is worth keeping. Reading is never narrowed: every
run recalls global memory and its own project's.

The placement travels to the tools in the run's metadata, under the core's own keys (so its helpers
and any core tool read the same thing) plus one key of the host's naming who is writing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from protocore.contracts.memory import MemoryScope
from protocore.tools.memory import MEMORY_SCOPE_CONTEXT_KEY, MEMORY_SCOPE_KEY_CONTEXT_KEY, MEMORY_SCOPE_KEYS_CONTEXT_KEY

WRITER_CONTEXT_KEY = "daedalus.memory_writer"
"""``operator`` when the run is the operator's conversation, ``agent`` otherwise."""

PROJECT_CONTEXT_KEY = "daedalus.memory_project"
"""The project whose memory the run uses, empty for none: what Remember files an entry under."""

WRITABLE_SCOPES: tuple[MemoryScope, ...] = (MemoryScope.global_, MemoryScope.project)
"""The only scopes a record is filed under. The core knows more (session, user, agent, custom); a
single operator has no users, and the others are the per-chat silos this module exists to end."""


@dataclass(frozen=True)
class MemoryPlacement:
    project_id: str = ""
    """The operator's project the chat belongs to; empty for a lone chat or a system project."""
    operator: bool = True
    """Whether the run is the operator's own conversation (see the module's docstring)."""

    def default(self) -> tuple[MemoryScope, str] | None:
        """Where an unscoped Remember goes; ``None`` when nowhere is allowed by default."""
        if self.project_id:
            return MemoryScope.project, self.project_id
        if self.operator:
            return MemoryScope.global_, ""
        return None

    def scope_keys(self) -> dict[MemoryScope, str]:
        """What an unscoped Recall reads beside global memory: the run's own project, when it has one."""
        return {MemoryScope.project: self.project_id} if self.project_id else {}

    def run_metadata(self) -> dict[str, Any]:
        """The keys a run's tools read this placement from."""
        found: dict[str, Any] = {
            WRITER_CONTEXT_KEY: "operator" if self.operator else "agent",
            PROJECT_CONTEXT_KEY: self.project_id,
            MEMORY_SCOPE_KEYS_CONTEXT_KEY: {scope.value: key for scope, key in self.scope_keys().items()},
        }
        default = self.default()
        if default is not None:
            # With no default the keys are left out: the host's Remember then refuses, which is the
            # point. (The core's own helper would fall back to the session scope, but no tool of the
            # core's that writes is registered here.)
            found[MEMORY_SCOPE_CONTEXT_KEY] = default[0].value
            found[MEMORY_SCOPE_KEY_CONTEXT_KEY] = default[1]
        return found


RUN_METADATA_KEYS: tuple[str, ...] = (WRITER_CONTEXT_KEY, PROJECT_CONTEXT_KEY, MEMORY_SCOPE_KEYS_CONTEXT_KEY, MEMORY_SCOPE_CONTEXT_KEY, MEMORY_SCOPE_KEY_CONTEXT_KEY)
"""Every key :meth:`MemoryPlacement.run_metadata` may set, so a run that changes hands clears the old ones."""


def stamp(run_metadata: dict[str, Any], placement: MemoryPlacement) -> None:
    """Put ``placement`` on a run's metadata in place, dropping whatever an earlier one left there:
    a run continued by a schedule after the operator's turn must not keep the operator's default."""
    for key in RUN_METADATA_KEYS:
        run_metadata.pop(key, None)
    run_metadata.update(placement.run_metadata())


def placement_from(metadata: Mapping[str, Any]) -> MemoryPlacement:
    """The placement a run's tool context carries. A context without one — a test, a tool called
    outside a run — is the operator's own and belongs to no project, the safe reading for a write:
    global memory, which the operator sees and can tidy, never a silo nobody reads."""
    project = metadata.get(PROJECT_CONTEXT_KEY)
    return MemoryPlacement(project_id=str(project or ""), operator=metadata.get(WRITER_CONTEXT_KEY, "operator") != "agent")


class PlacementRefused(ValueError):
    """A write the placement does not allow; the message is what the model reads."""


def resolve_write(placement: MemoryPlacement, scope: str | None, scope_key: str | None) -> tuple[MemoryScope, str]:
    """The scope and key a Remember files under, or :class:`PlacementRefused` saying why not."""
    if scope is None:
        default = placement.default()
        if default is None:
            raise PlacementRefused(
                "Not saved: this run is not the operator's conversation and belongs to no project, so it has no memory "
                "to write to. Put the finding in your answer instead; the conversation that asked for this work decides what is kept."
            )
        return default
    chosen = MemoryScope(scope)
    if chosen is MemoryScope.global_:
        if not placement.operator:
            raise PlacementRefused(
                "Not saved: only the operator's own conversations write to global memory. "
                + ("Omit the scope to save it in this project's memory, or put" if placement.project_id else "Put")
                + " the finding in your answer instead."
            )
        return chosen, ""
    if chosen is MemoryScope.project:
        key = (scope_key or "").strip() or placement.project_id
        if not key:
            raise PlacementRefused("Not saved: this chat belongs to no project; name the project's id in scope_key, or omit the scope.")
        return chosen, key
    raise PlacementRefused(f"Not saved: memory is filed under global or project scope, not {chosen.value}.")


__all__ = [
    "PROJECT_CONTEXT_KEY",
    "RUN_METADATA_KEYS",
    "WRITABLE_SCOPES",
    "WRITER_CONTEXT_KEY",
    "MemoryPlacement",
    "PlacementRefused",
    "placement_from",
    "resolve_write",
    "stamp",
]
