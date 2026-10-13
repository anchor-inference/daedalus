"""Remember and Recall as this host offers them: with a category and topics on every record, and
with the sensitive subjects the operator has not allowed refused at the door.

They replace the core's two tools of the same names (Forget stays the core's). The scope rules —
which scope a call files under, which keys it may address, what an unscoped recall fans out to —
are the core's own helpers, imported rather than copied, so the two cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, ClassVar

from protocore.contracts.memory import IMemoryContentScanner, MemoryStoreUnavailableError
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
    _allowed_recall_scopes,
    _ambient_scope_keys,
    _blocked_write_error,
    _call_id,
    _definition_from_model,
    _memory_disabled,
    _memory_disabled_error,
    _non_fatal_error,
    _resolve_scope,
    _resolved_max_records_per_scope,
    _resolved_similarity_threshold,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    scope: str | None = Field(default=None, description="Optional scope override: global, user, project, session, agent or custom. Omit for the default.")
    scope_key: str | None = Field(default=None, description="Optional key for the chosen scope. Omit to use the ambient key.")

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
    scope: str | None = Field(default=None, description="Optional single-scope filter (global, user, project, session, agent, custom).")
    scope_key: str | None = Field(default=None, description="Optional key for the chosen scope.")
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
        tenant_allowed: Callable[[], Iterable[str]],
        classifier: Callable[[], Asker | None],
        scanner: IMemoryContentScanner | None = None,
    ) -> None:
        super().__init__(store, scanner=scanner)
        self._memory = store
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
        scope, scope_key = _resolve_scope(context, payload.scope, payload.scope_key)
        try:
            result = await self._memory.write(
                context.tenant_id, scope, scope_key, payload.text,
                kind=category,
                metadata={"topics": topics, "classified_by": source},
                similarity_threshold=_resolved_similarity_threshold(context),
                max_records_per_scope=_resolved_max_records_per_scope(context),
            )
        except MemoryStoreUnavailableError as exc:
            return _non_fatal_error(call_id, f"memory unavailable: {exc}")
        record = result.record
        saved_topics = record_topics(record.metadata)
        return ToolResult(
            tool_call_id=call_id,
            content=f"{result.decision.value} memory {record.id} (scope={scope.value}; category={record.kind}; topics={', '.join(saved_topics) or '-'})",
            metadata={"memory_id": record.id, "decision": result.decision.value, "scope": scope.value, "scope_key": scope_key,
                      "version": record.version, "category": record.kind, "topics": saved_topics},
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
        try:
            if payload.scope is not None:
                scope, scope_key = _resolve_scope(context, payload.scope, payload.scope_key)
                hits = await self._memory.search(context.tenant_id, payload.query, scopes=[scope], scope_keys={scope: scope_key}, kinds=kinds, topics=topics, limit=payload.limit)
            else:
                recall_scopes = _allowed_recall_scopes(context)
                # An empty clipped list means no allowed scope resolves: the store would read [] as
                # "every default scope", widening past the allow-list, so nothing is asked at all.
                if recall_scopes is not None and not recall_scopes:
                    return ToolResult(tool_call_id=call_id, content="No matching memories.", metadata={"count": 0})
                hits = await self._memory.search(context.tenant_id, payload.query, scopes=recall_scopes, scope_keys=_ambient_scope_keys(context), kinds=kinds, topics=topics, limit=payload.limit)
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
    tenant_allowed: Callable[[], Iterable[str]],
    classifier: Callable[[], Asker | None],
    scanner: IMemoryContentScanner | None = None,
) -> list[ForgetTool | HostRecallTool | HostRememberTool]:
    """Forget, Recall, Remember — the core's order, which its prompt-cache sort expects."""
    return [
        ForgetTool(store),
        HostRecallTool(store, scanner=scanner),
        HostRememberTool(store, tenant_allowed=tenant_allowed, classifier=classifier, scanner=scanner),
    ]


__all__ = ["HostRecallTool", "HostRememberTool", "RecallInput", "RememberInput", "build_memory_tools", "sensitive_refusal"]
