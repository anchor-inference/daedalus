"""Remember and Recall as this host offers them: with a category and topics on every record, and
with the sensitive subjects the operator has not allowed refused at the door.

They replace the core's two tools of the same names (Forget stays the core's). Where a note goes
is the host's rule (:mod:`daedalus.memory.scope`): the chat's project, else the operator's global
memory, never a per-chat silo; and every note is filed in an entry with a source row naming the
chat and run that taught it (:mod:`daedalus.memory.entries`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, ClassVar, Literal

from protocore.contracts.memory import IMemoryContentScanner, MemoryScope, MemoryStoreUnavailableError
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolDefinition, ToolResult
from protocore.tools.memory import (
    RECALL_DEFAULT_LIMIT,
    RECALL_MAX_LIMIT,
    RECALL_QUERY_MAX_LENGTH,
    REMEMBER_TEXT_MAX_LENGTH,
    ForgetTool,
    RecallTool,
    RememberTool,
    _blocked_write_error,
    _call_id,
    _definition_from_model,
    _memory_disabled,
    _memory_disabled_error,
    _non_fatal_error,
    _resolved_max_records_per_scope,
    _resolved_similarity_threshold,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from daedalus.memory.entries import MemoryEntries, UnknownProject
from daedalus.memory.scope import WRITABLE_SCOPES, PlacementRefused, placement_from, resolve_write
from daedalus.memory.taxonomy import (
    CATEGORIES,
    SENSITIVE_LABELS,
    classify,
    normalise_category,
    normalise_topics,
    record_topics,
    sensitive_subjects,
)
from daedalus.stores.persistent import PersistentMemory

Asker = Callable[[str], Awaitable[str]]


def sensitive_refusal(call_id: str, subjects: Iterable[str]) -> ToolResult:
    """The refusal the model reads: what was refused, why, and what to do instead.

    It says plainly that rephrasing is not the way round it, because a model told only "blocked"
    tends to try again in other words, which is exactly the note the operator does not want kept."""
    names = " and ".join(SENSITIVE_LABELS.get(s, s) for s in sorted(subjects))
    return ToolResult(
        tool_call_id=call_id,
        content=(
            f"Not saved: this note concerns {names}, which the operator has not allowed in long-term memory "
            "(Settings → Memory → Sensitive topics). Do not save it in other words or in a file instead. "
            "Use it in this conversation only; if keeping it matters, tell the operator, who can allow the topic."
        ),
        is_error=True,
        metadata={"refused": sorted(subjects)},
    )


class RememberInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(
        ..., min_length=1, max_length=REMEMBER_TEXT_MAX_LENGTH,
        description="The fact, decision, preference or lesson to keep, as one self-contained sentence or short paragraph that names its subject.",
    )
    category: str | None = Field(
        default=None,
        description=(
            "One of person, project, preference, fact, procedure, other. person = about a specific person; "
            "project = a piece of work, repository or deadline; preference = how someone wants things done; "
            "procedure = how to do something, a lesson or a trap; fact = any other durable fact. Omit to have it inferred."
        ),
    )
    topics: list[str] | None = Field(
        default=None, max_length=5,
        description="One to three short lower-case tags naming what the note is about (e.g. 'deploy', 'billing-api'); reuse tags you have used before. Omit to have them inferred.",
    )
    scope: Literal["global", "project"] | None = Field(
        default=None,
        description=(
            "Omit for the default: this chat's project when it belongs to one, otherwise the operator's global memory. "
            "Pass global for something about the operator that holds everywhere (a preference, who they are) when you are inside a project."
        ),
    )
    scope_key: str | None = Field(default=None, description="With scope=project: the project's id, to file the note in another project's memory. Omit for this chat's project.")

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        category = normalise_category(value)
        if category is None:
            raise ValueError(f"unknown category {value!r}; use one of {', '.join(CATEGORIES)}")
        return category


class RecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(
        default="", max_length=RECALL_QUERY_MAX_LENGTH,
        description="Search text, ranked by keywords: use exact tokens (ids, names, paths) when you have them. Empty returns the most recent memories that match the filters.",
    )
    category: str | None = Field(default=None, description="Only memories of this category: person, project, preference, fact, procedure or other.")
    topic: str | None = Field(default=None, max_length=64, description="Only memories carrying this topic tag.")
    scope: Literal["global", "project"] | None = Field(default=None, description="Only global memory, or only a project's. Omit to search global memory and this chat's project together.")
    scope_key: str | None = Field(default=None, description="With scope=project: the project's id. Omit for this chat's project.")
    limit: int = Field(default=RECALL_DEFAULT_LIMIT, ge=1, le=RECALL_MAX_LIMIT, description=f"Max results (1-{RECALL_MAX_LIMIT}).")

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        category = normalise_category(value)
        if category is None:
            raise ValueError(f"unknown category {value!r}; use one of {', '.join(CATEGORIES)}")
        return category


class HostRememberTool(RememberTool):
    description_: ClassVar[str] = (
        "Save a durable fact, decision, preference or lesson to long-term memory so you can recall it in a later turn or "
        "session. Give it a category and one to three topics. Idempotent: saving the same fact again merges or skips. "
        "Health, personal finances, intimate life, identity documents and precise locations are refused unless the operator allowed them."
    )

    def __init__(
        self,
        store: PersistentMemory,
        *,
        entries: MemoryEntries,
        tenant_allowed: Callable[[], Iterable[str]],
        classifier: Callable[[], Asker | None],
        scanner: IMemoryContentScanner | None = None,
    ) -> None:
        super().__init__(store, scanner=scanner)
        self._memory = store
        self._entries = entries
        self._allowed = tenant_allowed
        self._classifier = classifier

    @property
    def definition(self) -> ToolDefinition:
        return _definition_from_model(self.name_, self.description_, RememberInput)

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        payload = RememberInput.model_validate(arguments)
        call_id = _call_id(context)
        if _memory_disabled(context):
            return _memory_disabled_error(call_id)
        topics = normalise_topics(payload.topics or [])
        if self._scanner is not None:
            # The topics are rendered into every later recall, so they are as untrusted as the text.
            threat = self._scanner.scan(payload.text) or next((hit for t in topics if (hit := self._scanner.scan(t))), None)
            if threat:
                return _blocked_write_error(call_id, threat)
        category = payload.category
        source = "agent"
        if category and topics:
            sensitive = sensitive_subjects(payload.text)
        else:
            found = await classify(payload.text, vocabulary=self._memory.vocabulary(context.tenant_id), ask=self._classifier())
            category = category or found.category
            topics = topics or found.topics
            sensitive = found.sensitive
            source = found.source
        refused = sensitive - set(self._allowed())
        if refused:
            return sensitive_refusal(call_id, refused)
        try:
            scope, scope_key = resolve_write(placement_from(context.metadata), payload.scope, payload.scope_key)
        except PlacementRefused as exc:
            return _non_fatal_error(call_id, str(exc))
        try:
            result = await self._entries.remember(
                scope, scope_key, payload.text,
                kind=category,
                metadata={"topics": topics, "classified_by": source},
                origin="remember",
                session_id=context.session_id,
                run_id=context.run_id,
                similarity_threshold=_resolved_similarity_threshold(context),
                max_records_per_scope=_resolved_max_records_per_scope(context),
            )
        except UnknownProject:
            return _non_fatal_error(call_id, f"Not saved: there is no project with the id {scope_key!r}.")
        except MemoryStoreUnavailableError as exc:
            return _non_fatal_error(call_id, f"memory unavailable: {exc}")
        record = result.record
        saved_topics = record_topics(record.metadata)
        return ToolResult(
            tool_call_id=call_id,
            content=f"{result.decision.value} memory {record.id} (scope={scope.value}; category={record.kind}; topics={', '.join(saved_topics) or '-'})",
            metadata={"memory_id": record.id, "decision": result.decision.value, "scope": scope.value, "scope_key": scope_key,
                      "version": record.version, "category": record.kind, "topics": saved_topics, "entry": record.metadata.get("entry")},
        )


class HostRecallTool(RecallTool):
    description_: ClassVar[str] = (
        "Search your long-term memory and return the most relevant saved facts, ranked by keywords. Narrow by category "
        "or topic when you know what kind of thing you are after; pass exact tokens (ids, names, paths) for precise recall."
    )

    def __init__(self, store: PersistentMemory, *, scanner: IMemoryContentScanner | None = None) -> None:
        super().__init__(store, scanner=scanner)
        self._memory = store

    @property
    def definition(self) -> ToolDefinition:
        return _definition_from_model(self.name_, self.description_, RecallInput)

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        payload = RecallInput.model_validate(arguments)
        call_id = _call_id(context)
        if _memory_disabled(context):
            return _memory_disabled_error(call_id)
        kinds = [payload.category] if payload.category else None
        topics = normalise_topics([payload.topic]) if payload.topic else None
        placement = placement_from(context.metadata)
        if payload.scope == MemoryScope.global_.value:
            scopes, keys = [MemoryScope.global_], {}
        elif payload.scope == MemoryScope.project.value:
            key = (payload.scope_key or "").strip() or placement.project_id
            if not key:
                return _non_fatal_error(call_id, "This chat belongs to no project; name the project's id in scope_key, or omit the scope.")
            scopes, keys = [MemoryScope.project], {MemoryScope.project: key}
        else:
            # Global memory and this chat's project, nothing else: every other key is left out of
            # the map, and the store skips a non-global scope it has no key for.
            scopes, keys = list(WRITABLE_SCOPES), placement.scope_keys()
        try:
            hits = await self._memory.search(context.tenant_id, payload.query, scopes=scopes, scope_keys=keys, kinds=kinds, topics=topics, limit=payload.limit)
        except MemoryStoreUnavailableError as exc:
            return _non_fatal_error(call_id, f"memory unavailable: {exc}")
        if not hits:
            return ToolResult(tool_call_id=call_id, content="No matching memories.", metadata={"count": 0})
        lines = []
        for index, hit in enumerate(hits, start=1):
            record = hit.record
            tags = ", ".join(self._safe_recall_kind(t) for t in record_topics(record.metadata))
            lines.append(f"[{index}] id={record.id} ({record.scope.value}/{self._safe_recall_kind(record.kind)}{'; ' + tags if tags else ''}) {self._safe_recall_text(record.text)}")
        return ToolResult(tool_call_id=call_id, content="\n".join(lines), metadata={"count": len(hits), "memory_ids": [h.record.id for h in hits]})


def build_memory_tools(
    store: PersistentMemory,
    *,
    entries: MemoryEntries,
    tenant_allowed: Callable[[], Iterable[str]],
    classifier: Callable[[], Asker | None],
    scanner: IMemoryContentScanner | None = None,
) -> list[ForgetTool | HostRecallTool | HostRememberTool]:
    """Forget, Recall, Remember — the core's order, which its prompt-cache sort expects."""
    return [
        ForgetTool(store),
        HostRecallTool(store, scanner=scanner),
        HostRememberTool(store, entries=entries, tenant_allowed=tenant_allowed, classifier=classifier, scanner=scanner),
    ]


__all__ = ["HostRecallTool", "HostRememberTool", "RecallInput", "RememberInput", "build_memory_tools", "sensitive_refusal"]
