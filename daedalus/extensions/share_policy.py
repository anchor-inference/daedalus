"""What a live link lets its visitor pick and spend: the policy the operator sets in the share dialog.

The policy is JSON kept on the link's row. ``roles`` maps a model role (``daedalus.host.share_roles``) to
the value a visitor's turn uses and the picks the visitor may switch to; a role left out is the session's
own, free as it always was. ``budget_usd`` and ``budget_weekly_pct`` bound what the visitor's turns may
spend, and ``terminals`` says which of the session's terminals the link shows. ``{}`` is a link with no
limits, which is what every link made before the policy existed carries.

Everything here is pure: the stored text in, a checked dict out. A policy is checked once, when the
operator writes it, against the roles and presets the session has then; what is read back later is
trusted to be that, and a value that no longer parses is read as the strictest thing it could have meant
rather than as no limit, because a link is a credential in a stranger's hands.
"""

from __future__ import annotations

import json
import math
from typing import Any

TERMINALS = ("all", "container", "none")
BUDGET_USD_MAX = 100_000.0
ROLES_MAX = 64
ALLOWED_MAX = 64


class PolicyError(ValueError):
    """A policy the operator sent that cannot be kept; ``field`` names where, as the dialog marks it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


def _strings(raw: Any, field: str, known: set[str]) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise PolicyError(field, "a list of strings")
    unknown = [item for item in raw if item not in known]
    if unknown:
        raise PolicyError(field, f"not offered here: {', '.join(sorted(set(unknown)))}")
    return list(dict.fromkeys(raw))


def _budget(raw: Any, field: str, top: float) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw) or not 0 < raw <= top:
        raise PolicyError(field, f"a number above 0 and at most {top:g}, or null for no limit")
    return float(raw)


def validate(raw: Any, roles: list[dict[str, Any]], place: str) -> dict[str, Any]:
    """The policy ``raw`` as it is kept, checked against the session's ``roles`` (``describe``) and its
    ``place`` (``container`` or ``host``). Raises :class:`PolicyError` naming the first field that is wrong."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PolicyError("policy", "an object")
    extra = set(raw) - {"roles", "budget_usd", "budget_weekly_pct", "terminals"}
    if extra:
        raise PolicyError(f"policy.{sorted(extra)[0]}", "not a setting of a link")
    kept: dict[str, Any] = {}
    by_id = {str(role.get("id")): role for role in roles}
    asked = raw.get("roles") or {}
    if not isinstance(asked, dict) or len(asked) > ROLES_MAX:
        raise PolicyError("policy.roles", "an object of roles")
    checked_roles: dict[str, Any] = {}
    for role_id, entry in asked.items():
        field = f"policy.roles.{role_id}"
        role = by_id.get(role_id)
        if role is None:
            raise PolicyError(field, "no such role in this session")
        if not isinstance(entry, dict):
            raise PolicyError(field, "an object")
        extra = set(entry) - {"preset", "effort", "account", "allowed"}
        if extra:
            raise PolicyError(f"{field}.{sorted(extra)[0]}", "not a setting of a role")
        presets = {str(p.get("id")): p for p in role.get("presets") or []}
        preset = entry.get("preset")
        if not isinstance(preset, str) or preset not in presets:
            raise PolicyError(f"{field}.preset", "not a preset this role can use")
        efforts = {str(e) for e in presets[preset].get("efforts") or []}
        accounts = {str(a.get("id")) for a in presets[preset].get("accounts") or []}
        effort = entry.get("effort") or ""
        if not isinstance(effort, str) or (effort and effort not in efforts):
            raise PolicyError(f"{field}.effort", "not an effort of that preset")
        account = entry.get("account") or ""
        if not isinstance(account, str) or (account and account not in accounts):
            raise PolicyError(f"{field}.account", "not an account of that preset")
        allowed_raw = entry.get("allowed") or []
        if not isinstance(allowed_raw, list) or len(allowed_raw) > ALLOWED_MAX:
            raise PolicyError(f"{field}.allowed", "a list of picks")
        allowed = []
        for index, pick in enumerate(allowed_raw):
            where = f"{field}.allowed[{index}]"
            if not isinstance(pick, dict) or set(pick) - {"preset", "accounts", "efforts"}:
                raise PolicyError(where, "an object of preset, accounts and efforts")
            name = pick.get("preset")
            if not isinstance(name, str) or name not in presets:
                raise PolicyError(f"{where}.preset", "not a preset this role can use")
            allowed.append({
                "preset": name,
                "accounts": _strings(pick.get("accounts") or [], f"{where}.accounts", {str(a.get("id")) for a in presets[name].get("accounts") or []}),
                "efforts": _strings(pick.get("efforts") or [], f"{where}.efforts", {str(e) for e in presets[name].get("efforts") or []}),
            })
        checked_roles[role_id] = {"preset": preset, "effort": effort, "account": account, "allowed": allowed}
    if checked_roles:
        kept["roles"] = checked_roles
    usd = _budget(raw.get("budget_usd"), "policy.budget_usd", BUDGET_USD_MAX)
    if usd is not None:
        kept["budget_usd"] = usd
    pct = _budget(raw.get("budget_weekly_pct"), "policy.budget_weekly_pct", 100.0)
    if pct is not None:
        kept["budget_weekly_pct"] = pct
    terminals = raw.get("terminals", "all")
    if terminals not in terminal_choices(place):
        raise PolicyError("policy.terminals", f"one of {', '.join(terminal_choices(place))} for a session on the {place}")
    if terminals != "all":
        kept["terminals"] = terminals
    return kept


def terminal_choices(place: str) -> list[str]:
    """What ``terminals`` may be for a session in ``place``: "only the container's" means nothing for a
    session whose commands run on the host, so it is not offered there."""
    return list(TERMINALS) if place == "container" else ["all", "none"]


def parse(stored: str | None) -> dict[str, Any]:
    """The policy kept on a row. Text that does not parse is read as the strictest link (no terminals,
    nothing to spend), never as no limits: the row was written by :func:`validate`, so a broken one is a
    fault, and a fault must not open more than the operator chose."""
    try:
        found = json.loads(stored or "{}")
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return {"terminals": "none", "budget_usd": 0.0, "budget_weekly_pct": 0.0, "roles": {}, "broken": True}
    return found


def terminals(policy: dict[str, Any]) -> str:
    value = policy.get("terminals", "all")
    return value if value in TERMINALS else "none"


def role(policy: dict[str, Any], role_id: str) -> dict[str, Any] | None:
    found = (policy.get("roles") or {}).get(role_id)
    return found if isinstance(found, dict) else None


def presets_allowed(policy: dict[str, Any], role_id: str) -> set[str] | None:
    """The presets a visitor may use for ``role_id``; None when the role is not limited at all."""
    entry = role(policy, role_id)
    if entry is None:
        return None if not policy.get("broken") else set()
    return {str(entry.get("preset") or "")} | {str(pick.get("preset") or "") for pick in entry.get("allowed") or []}


def allows(policy: dict[str, Any], role_id: str, preset: str, effort: str | None, account: str | None) -> bool:
    """Whether a visitor may switch ``role_id`` to this preset, effort and account. The role's own value is
    always allowed; any other must match one of ``allowed``, where an empty ``accounts`` or ``efforts``
    list is any of that preset's. None for the effort or the account is "left as it is", and is not judged."""
    entry = role(policy, role_id)
    if entry is None:
        return not policy.get("broken")
    if preset == entry.get("preset") and effort in (None, entry.get("effort") or "") and account in (None, entry.get("account") or ""):
        return True
    for pick in entry.get("allowed") or []:
        if pick.get("preset") != preset:
            continue
        efforts = pick.get("efforts") or []
        accounts = pick.get("accounts") or []
        # "" is the preset's own effort or account, which is a choice like any other: a list that names
        # some leaves it out, since the preset's own may be exactly the one the operator kept away.
        if (not efforts or effort is None or effort in efforts) and (not accounts or account is None or account in accounts):
            return True
    return False


def limits(policy: dict[str, Any], spent_usd: float, spent_weekly_pct: float) -> dict[str, Any]:
    """What the visitor's page shows of the link's limits (``GET /api/share`` → ``limits``)."""
    usd = policy.get("budget_usd")
    pct = policy.get("budget_weekly_pct")
    return {
        "roles": {
            role_id: {"preset": entry.get("preset") or "", "effort": entry.get("effort") or "", "account": entry.get("account") or "", "allowed": list(entry.get("allowed") or [])}
            for role_id, entry in (policy.get("roles") or {}).items()
            if isinstance(entry, dict)
        },
        "budget": {
            "usd": {"limit": usd, "spent": round(float(spent_usd or 0), 6)} if usd is not None else None,
            "weekly_pct": {"limit": pct, "spent": round(float(spent_weekly_pct or 0), 6)} if pct is not None else None,
        },
        "terminals": terminals(policy),
    }


__all__ = ["TERMINALS", "PolicyError", "allows", "limits", "parse", "presets_allowed", "role", "terminal_choices", "terminals", "validate"]
