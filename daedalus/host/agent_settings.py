"""What the agent may read and change of Daedalus's own settings, and how each change of its is classed.

The agent reaches the configuration through two host tools (``SettingsGet``, ``SettingsChange``) and
never through the file: the state directory is sealed from sessions. Every path a change touches is
one of three kinds:

* ``never`` — the agent can neither change it nor read it when it is a credential. The list is
  :data:`NEVER` plus every key that names a credential (:data:`SECRET_KEY`), and it is code on
  purpose: were it configuration, ``SettingsChange`` could edit the very setting that guards it.
* ``auto`` — a section the operator lets the agent apply at once (``[agent_settings] auto_apply``).
* ``approval`` — everything else, which waits for the operator's Apply.

``agent_settings`` itself is always ``approval``, whatever ``auto_apply`` says: an agent that could
add sections to its own list without asking would make the list meaningless.

Everything here is pure: the extension (``daedalus/extensions/agent_settings.py``) owns the store,
the notification and the save.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import types
import typing
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from daedalus.config import RuntimeConfig

Kind = Literal["auto", "approval", "never"]

OWN_SECTION = "agent_settings"

NEVER: dict[str, str] = {
    "self_change": "how the agent's own code changes are reviewed and rebuilt; the agent must not loosen its own review",
    "ops": "the boot guard, retention and retry machinery the installation's recovery depends on",
    "webhooks": "doors into the agent from outside; a webhook's secret and scheme are its only lock",
    "policy": "the operator's rules over the agent's tools",
    "action_rules": "the operator's levels for what the agent does in the world on its own, and the review before it; the agent must not loosen its own leash",
    "hooks": "the operator's scripts that run around every tool call and can refuse or rewrite one",
    "providers": "model endpoints and their keys",
    "mcp.servers.*.env": "credentials handed to an MCP server",
    "mcp.servers.*.headers": "credentials sent to an MCP server",
    "tools.exec.sandbox": "the walls around the agent's own commands",
    "tools.exec.sandbox_extra_writable": "the walls around the agent's own commands",
    "tools.web.proxy": "where every web request goes, and it may carry a login",
    "browser.local_sites": "the browser's network wall",
    "browser.lan_allow": "the browser's network wall",
    "browser.lan_sites": "the browser's network wall",
    "browser.rules": "the operator's rules for sensitive actions in the browser",
    "browser.record_frames": "whether the browser is recorded is the operator's to switch",
    "browser.record_takeover": "whether the browser is recorded is the operator's to switch",
    "telegram.mode": "where the operator's chat lives",
    "telegram.forum_chat_id": "where the operator's chat lives",
    "telegram.general_topic_id": "where the operator's chat lives",
}
"""Paths (``*`` is any one segment, a prefix covers everything under it) the agent can never change.
Some name sections the settings body does not even carry today (``policy``, ``hooks``, ``providers``):
they are listed so a section added to it later is guarded from its first day."""

SECRET_KEY = re.compile(r"^(?:.*_)?(?:api_key|secret|token|password|passwd|credentials?|private_key|authorization|cookie)$", re.IGNORECASE)
"""A key that names a credential, wherever it sits. Whole names only: ``max_run_tokens`` and
``chunk_tokens`` are counts, not tokens, and matching a substring refused them."""

MASK = "•••"
"""The same mask the redactor and ``GET /api/settings`` show for a stored credential."""

APPLY_AT_TEXT = {
    "next_step": "from the next model call",
    "reconnect": "when the provider or MCP connection is next rebuilt",
    "restart": "after Daedalus restarts",
}


class ChangeRefused(ValueError):
    """A change the agent may not make, said in words the model can act on."""


@dataclass(frozen=True)
class Change:
    path: str
    old: Any
    new: Any
    apply_at: str
    kind: Kind

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "old": self.old, "new": self.new, "apply_at": self.apply_at, "kind": self.kind}


def _matches(pattern: str, path: str) -> bool:
    want, have = pattern.split("."), path.split(".")
    return len(have) >= len(want) and all(w in ("*", h) for w, h in zip(want, have, strict=False))


def never_reason(path: str) -> str | None:
    """Why the agent may never change ``path``, or ``None`` when it may."""
    for pattern, reason in NEVER.items():
        if _matches(pattern, path):
            return reason
    if any(SECRET_KEY.match(part) for part in path.split(".")):
        return "a credential"
    return None


def auto_sections(config: RuntimeConfig, sections: Collection[str]) -> frozenset[str]:
    """The sections a change applies at once: the operator's list, of the sections that exist, never this
    one's own and never one that is the operator's alone as a whole (listing it would only make the
    listing of sections say "at once" for a section every change of is refused)."""
    return frozenset(
        name for name in config.agent_settings.auto_apply
        if name in sections and name != OWN_SECTION and never_reason(name) is None
    )


def kind_of(path: str, auto: Collection[str]) -> Kind:
    if never_reason(path) is not None:
        return "never"
    return "auto" if path.split(".", 1)[0] in auto else "approval"


def merge_patch(target: Any, patch: Any) -> Any:
    """``patch`` applied to ``target`` as a JSON merge patch (RFC 7396): objects merge, ``null`` removes a key.

    A removed key falls back to its default when the configuration is validated, so ``null`` is
    also how a field is reset. Lists are replaced whole, as they are everywhere in the settings.
    """
    if not isinstance(patch, Mapping):
        return patch
    out = dict(target) if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def candidate(current: RuntimeConfig, changes: Mapping[str, Any], sections: Collection[str]) -> dict[str, Any]:
    """The whole configuration as it would be with ``changes`` merged in, before validation."""
    unknown = sorted(set(changes) - set(sections))
    if unknown:
        raise ChangeRefused(f"no such settings section {', '.join(map(repr, unknown))}; the sections are {', '.join(sorted(sections))}")
    raw = current.model_dump(mode="json")
    for section, value in changes.items():
        if value is None:
            raise ChangeRefused(f"a whole section cannot be removed ({section!r}); send the fields to change")
        raw[section] = merge_patch(raw.get(section), value)
    return raw


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaves by dotted path. An empty map has no leaves, as in the validation's own flattening, so a
    map's first entry reads as the entry's fields appearing rather than as ``{}`` turning into them."""
    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    return out


def diff(before: RuntimeConfig, after: RuntimeConfig, apply_at: Mapping[str, str], auto: Collection[str]) -> list[Change]:
    """Every leaf that differs, with when it takes effect and its kind; ``apply_at`` is the validation's own answer."""
    old, new = flatten(before.model_dump(mode="json")), flatten(after.model_dump(mode="json"))
    return [
        Change(path, old.get(path), new.get(path), apply_at.get(path, "next_step"), kind_of(path, auto))
        for path in sorted(set(old) | set(new))
        if old.get(path) != new.get(path)
    ]


def overall(changes: Iterable[Change]) -> Kind:
    kinds = {change.kind for change in changes}
    return "never" if "never" in kinds else "approval" if "approval" in kinds else "auto"


def mask(view: Any, path: str = "") -> Any:
    """``view`` (a dumped section) with every credential in it masked the way the settings screen shows it.

    A credential key becomes empty with a ``<key>_set`` flag beside it, as a provider's key and a
    webhook's secret do in ``GET /api/settings``; the values of MCP headers and environment become
    the mask, as they do there; the web proxy, which may carry a login, is shown only as set or not.
    """
    if isinstance(view, dict):
        out: dict[str, Any] = {}
        for key, value in view.items():
            here = f"{path}.{key}" if path else str(key)
            if (_matches("mcp.servers.*.env", here) or _matches("mcp.servers.*.headers", here)) and isinstance(value, dict):
                out[key] = {k: (MASK if v else v) for k, v in value.items()}
            elif here == "tools.web.proxy":
                out[key] = MASK if value else ""
            elif SECRET_KEY.match(str(key)) and not isinstance(value, dict | list):
                out[key] = ""
                out[f"{key}_set"] = bool(value)
            else:
                out[key] = mask(value, here)
        return out
    if isinstance(view, list):
        return [mask(item, path) for item in view]
    return view


@cache
def attribute_docs(cls: type) -> dict[str, str]:
    """The docstrings written under a class's fields, by name.

    The configuration documents each field with a string under it, which pydantic keeps nowhere
    unless a model opts in; the source is the one place they are. A class whose source cannot be
    read (a frozen build) answers with no descriptions rather than failing the tool.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except (OSError, TypeError, SyntaxError):
        return {}
    body = tree.body[0].body if tree.body and isinstance(tree.body[0], ast.ClassDef) else []
    docs: dict[str, str] = {}
    for node, following in zip(body, body[1:], strict=False):
        if (
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and isinstance(following, ast.Expr) and isinstance(following.value, ast.Constant) and isinstance(following.value.value, str)
        ):
            docs[node.target.id] = " ".join(following.value.value.split())
    return docs


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    end = re.search(r"(?<=[.;:])\s", text)
    return (text[: end.start()] if end else text)[:200]


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return "one of " + ", ".join(repr(a) for a in args)
    if origin in (types.UnionType, typing.Union):
        names = [_type_name(a) for a in args if a is not type(None)]
        return " or ".join(names) + (" or null" if type(None) in args else "")
    if origin in (list, tuple):
        return f"list of {_type_name(args[0])}" if args else "list"
    if origin is dict:
        return f"map of name to {_type_name(args[1])}" if len(args) == 2 else "map"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    return {bool: "boolean", int: "integer", float: "number", str: "string"}.get(annotation, getattr(annotation, "__name__", str(annotation)))


_CONSTRAINTS = ("ge", "gt", "le", "lt", "min_length", "max_length", "pattern")


def _field_info(info: FieldInfo, doc: str) -> dict[str, Any]:
    out: dict[str, Any] = {"type": _type_name(info.annotation)}
    for item in info.metadata:
        for name in _CONSTRAINTS:
            value = getattr(item, name, None)
            if value is not None:
                out[name] = value
    default = info.get_default(call_default_factory=True) if info.default is not PydanticUndefined or info.default_factory else PydanticUndefined
    if default is not PydanticUndefined and not isinstance(default, BaseModel):
        out["default"] = default
    if doc:
        out["about"] = doc
    return out


def _model_of(annotation: Any) -> tuple[type[BaseModel] | None, bool]:
    """The model a field holds, and whether it is a map of named entries of it (``mcp.servers``, ``webhooks``)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    if get_origin(annotation) is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
            return args[1], True
    return None, False


def describe_model(model: type[BaseModel], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Every field of ``model``, by dotted path under ``prefix``: its type, constraints, default and description.

    A map of named entries is described once, with ``*`` standing for an entry's name.
    """
    docs = attribute_docs(model)
    out: dict[str, dict[str, Any]] = {}
    for name, info in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        inner, named = _model_of(info.annotation)
        if inner is not None:
            out.update(describe_model(inner, f"{path}.*" if named else path))
        else:
            out[path] = _field_info(info, docs.get(name, ""))
    return out


def describe_section(section: str) -> dict[str, dict[str, Any]]:
    info = RuntimeConfig.model_fields[section]
    inner, named = _model_of(info.annotation)
    if inner is None:
        return {section: _field_info(info, attribute_docs(RuntimeConfig).get(section, ""))}
    return describe_model(inner, f"{section}.*" if named else section)


def section_summary(section: str, body_docs: Mapping[str, str]) -> str:
    """One line on what a section is: the settings body's own note, else the configuration's, else its class's."""
    text = body_docs.get(section) or attribute_docs(RuntimeConfig).get(section, "")
    if not text:
        inner, _named = _model_of(RuntimeConfig.model_fields[section].annotation)
        text = (inspect.getdoc(inner) or "") if inner is not None else ""
    return first_sentence(text)


def change_lines(changes: Iterable[Mapping[str, Any]]) -> list[str]:
    """``path: old → new (applies …)``, one line a change, for the agent and for a notification."""
    return [f"{c['path']}: {_short(c['old'])} → {_short(c['new'])} ({c['apply_at'].replace('_', ' ')})" for c in changes]


def _short(value: Any) -> str:
    text = "unset" if value is None else repr(value) if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else str(value)
    return text if len(text) <= 120 else text[:117] + "…"


def outcome_text(record: dict[str, Any]) -> str:
    """The message the agent gets when its change is decided."""
    head = f"[settings change {record['id']}]"
    lines = "\n".join(f"- {line}" for line in change_lines(record["changes"]))
    status = record["status"]
    if status == "applied":
        when = sorted({APPLY_AT_TEXT.get(c["apply_at"], c["apply_at"]) for c in record["changes"]})
        return f"{head} The operator applied your settings change. It takes effect {' / '.join(when)}:\n{lines}"
    if status == "rejected":
        return f"{head} The operator rejected your settings change; nothing was changed. Do not propose it again unless they ask:\n{lines}"
    if status == "expired":
        return f"{head} Your settings change expired without an answer; nothing was changed. Propose it again only if the operator still wants it:\n{lines}"
    if status == "stale":
        return f"{head} Your settings change was not applied: the settings changed after you proposed it. Read them again with SettingsGet and propose it anew if it is still wanted:\n{lines}"
    return f"{head} Your settings change could not be applied ({record.get('error') or 'unknown error'}); nothing was changed:\n{lines}"


def result_text(record: dict[str, Any]) -> str:
    """The tool's answer. Its first line names the change: the app reads the id from it to draw the card."""
    head = f"Settings change: {record['id']}"
    lines = "\n".join(f"- {line}" for line in change_lines(record["changes"]))
    if record["status"] == "applied":
        when = sorted({APPLY_AT_TEXT.get(c["apply_at"], c["apply_at"]) for c in record["changes"]})
        return f"{head}\nApplied at once. It takes effect {' / '.join(when)}; tell the operator so:\n{lines}"
    if record["status"] == "pending":
        return (
            f"{head}\nWaiting for the operator: an Apply / Reject card is in this chat and in their notifications. "
            "Tell them what you proposed and that it waits for them. Do not propose it again meanwhile; the outcome "
            f"reaches you as a message.\n{lines}"
        )
    return f"{head}\n{outcome_text(record)}"


__all__ = [
    "APPLY_AT_TEXT",
    "MASK",
    "NEVER",
    "OWN_SECTION",
    "SECRET_KEY",
    "Change",
    "ChangeRefused",
    "Kind",
    "attribute_docs",
    "auto_sections",
    "candidate",
    "change_lines",
    "describe_model",
    "describe_section",
    "diff",
    "first_sentence",
    "flatten",
    "kind_of",
    "mask",
    "merge_patch",
    "never_reason",
    "outcome_text",
    "overall",
    "result_text",
    "section_summary",
]
