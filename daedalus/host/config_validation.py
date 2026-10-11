"""Pure candidate validation and revisioning for mutable runtime configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from daedalus.config import RuntimeConfig


class ConfigProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: Literal["provider", "mcp", "runtime"]
    path: str
    message: str


class EffectiveConfigChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    apply_at: Literal["next_step", "reconnect", "restart"]


class ConfigValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: str
    candidate_revision: str | None = None
    valid: bool
    stale: bool = False
    problems: list[ConfigProblem] = Field(default_factory=list)
    changes: list[EffectiveConfigChange] = Field(default_factory=list)
    required_restart: bool = False
    probes: list[dict[str, Any]] = Field(default_factory=list)


class ConfigConflict(ValueError):
    """The editor based its candidate on a configuration that is no longer current."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("settings changed in another window; review the current values and try again")
        self.current_revision = current_revision


def config_revision(config: RuntimeConfig) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out.update(_flatten(item, path))
    return out


def _apply_at(path: str) -> Literal["next_step", "reconnect", "restart"]:
    if path.startswith(("providers.", "mcp.")):
        return "reconnect"
    if path.startswith(("telegram.", "ops.")):
        return "restart"
    return "next_step"


def _group(path: str) -> Literal["provider", "mcp", "runtime"]:
    if path.startswith("providers."):
        return "provider"
    if path.startswith("mcp."):
        return "mcp"
    return "runtime"


UNKNOWN_REFERENCE_PATHS = frozenset({"compaction.preset", "model.preset", "model.chain"})
"""Problems on these paths name something that does not exist, not a malformed value; the save answers 422."""


def _dangling_references(current: RuntimeConfig, candidate: RuntimeConfig) -> list[ConfigProblem]:
    """A newly chosen preset id — the summary model, the default model, a fallback — that names no configured preset.

    Only a change is refused. A preset chosen earlier and deleted since stays in the configuration on
    purpose — the runtime summarises with the session's own model until it is replaced, and the app
    shows it as missing — so every unrelated save that carries the section along must still pass.
    """
    problems: list[ConfigProblem] = []
    chosen = candidate.compaction.preset
    if chosen and chosen != current.compaction.preset and chosen not in candidate.presets:
        problems.append(ConfigProblem(group="runtime", path="compaction.preset", message=f"no such model preset {chosen!r} for the summary model; pick one from Settings → Models"))
    # The default model and its fallbacks are chosen in the app through a patch that refuses an unknown
    # id itself; a candidate built any other way (the agent's SettingsChange) met no such check, and a
    # default naming nothing would leave every new run without a model.
    chosen = candidate.model.preset
    if chosen and chosen != current.model.preset and chosen not in candidate.presets:
        problems.append(ConfigProblem(group="runtime", path="model.preset", message=f"no such model preset {chosen!r}; the presets are {', '.join(sorted(candidate.presets)) or 'none'}"))
    added = [pid for pid in candidate.model.chain if pid not in current.model.chain and pid not in candidate.presets]
    if added:
        problems.append(ConfigProblem(group="runtime", path="model.chain", message=f"no such model preset {', '.join(map(repr, added))} for the fallback chain"))
    return problems


def validate_candidate(current: RuntimeConfig, raw: dict[str, Any], *, base_revision: str) -> tuple[ConfigValidation, RuntimeConfig | None]:
    current_revision = config_revision(current)
    if base_revision != current_revision:
        return ConfigValidation(base_revision=current_revision, valid=False, stale=True), None
    try:
        candidate = RuntimeConfig.model_validate(raw)
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_url=False):
            path = ".".join(str(part) for part in error["loc"])
            problems.append(ConfigProblem(group=_group(path), path=path, message=str(error["msg"])[:500]))
        return ConfigValidation(base_revision=current_revision, valid=False, problems=problems), None
    dangling = _dangling_references(current, candidate)
    if dangling:
        return ConfigValidation(base_revision=current_revision, valid=False, problems=dangling), None
    before = _flatten(current.model_dump(mode="json"))
    after = _flatten(candidate.model_dump(mode="json"))
    changes = [EffectiveConfigChange(path=path, apply_at=_apply_at(path)) for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]
    return ConfigValidation(
        base_revision=current_revision,
        candidate_revision=config_revision(candidate),
        valid=True,
        changes=changes,
        required_restart=any(change.apply_at == "restart" for change in changes),
    ), candidate


def _merged(base: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        out[key] = _merged(out.get(key), value) if isinstance(value, Mapping) else value
    return out


def patched(current: RuntimeConfig, patch: Mapping[str, Any]) -> tuple[ConfigValidation, RuntimeConfig | None]:
    """``patch`` merged over the current configuration and validated against the current revision.

    For a command that changes a field or two (``/approval``, ``/verbosity``). Those used to set the
    field on the live configuration and save that same object: the revision check had nothing to
    compare, and the rollback after a failed reload restored the object it had just changed, so a
    reload that failed left the change in force with nothing saying so. A candidate is a new object,
    and the save's rollback has the old one to go back to.
    """
    return validate_candidate(current, _merged(current.model_dump(mode="json"), patch), base_revision=config_revision(current))


__all__ = ["UNKNOWN_REFERENCE_PATHS", "ConfigConflict", "ConfigProblem", "ConfigValidation", "EffectiveConfigChange", "config_revision", "patched", "validate_candidate"]
