"""A live link's visitor's turn: which link drives it, and what each model role resolves to inside it.

A visitor's turn is a run started from a visitor's words (their message, a queue the visitor alone filled,
their retry or their answer) or a helper run its leader started while such a turn was going. The run keeps
the link's id on its state from its start to its end (``SessionState.share_id``); the operator's own runs
in the same session carry none, so nothing here ever reaches them.

Inside a visitor's turn a role named in the link's policy runs on the policy's value, or on the visitor's
own pick when the policy allows that pick. The pick is kept beside the link in the session's metadata
(``PICKS_KEY``, keyed by link), never in the session's model: written there, the operator's next turn
would have run on whatever a visitor last chose. A helper's model is the agent's pick, not the visitor's:
the leader makes it from the link's set when it starts the helper (:func:`agent_pick`), and it is kept on
the helper's own metadata under the same key.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from daedalus.host import quick_agent

if TYPE_CHECKING:
    from daedalus.stores.database import Database

logger = logging.getLogger(__name__)

PICKS_KEY = "daedalus.share_picks"
"""Session metadata: ``{share id: {"preset", "effort", "account"}}``, what each link's visitor chose."""

_visiting: contextvars.ContextVar[str] = contextvars.ContextVar("daedalus_share_visiting", default="")


@contextmanager
def visiting(share_id: str) -> Iterator[None]:
    """Mark the request a visitor's while it is being accepted, so a run it starts knows its link.

    Read only when a run starts (:func:`starting_share`), never during it: tasks created inside the
    request copy the mark, and one of them may later start the operator's run."""
    token = _visiting.set(share_id)
    try:
        yield
    finally:
        _visiting.reset(token)


def starting_share(metadata: dict[str, Any] | None) -> str:
    """The link a run starting now belongs to: the visitor's mark on its message, else the request's.

    The request's mark is used up here: the run's task, created after this, copies the context, and the
    next run that task starts from the queue (the operator's follow-up, say) would otherwise be the visitor's."""
    from daedalus.host.transcript_view import VISITOR_KEY  # Lazy: the view module imports the runner's neighbours

    requested = _visiting.get()
    if requested:
        _visiting.set("")
    mark = (metadata or {}).get(VISITOR_KEY)
    if isinstance(mark, dict) and mark.get("share"):
        return str(mark["share"])
    return requested


def role_of(metadata: dict[str, Any]) -> str:
    """The role a session's run is, for a link's policy: the conversation, a quick agent or a subagent."""
    if not metadata.get("subagent_of"):
        return "main"
    return "quick" if metadata.get(quick_agent.KEY) else "subagents"


async def policy_of(db: Database, share_id: str) -> dict[str, Any]:
    """The link's policy, ``{}`` when it has none, its row is gone or the column is not there yet."""
    try:
        row = await db.fetchone("SELECT policy FROM session_shares WHERE id = ?", (share_id,))
    except Exception:  # noqa: BLE001 — a database without the column has links with no limits
        return {}
    if row is None or not row["policy"]:
        return {}
    try:
        found = json.loads(row["policy"])
    except ValueError:
        logger.warning("live link %s has a policy that is not JSON; it is read as no policy", share_id)
        return {}
    return found if isinstance(found, dict) else {}


def rule_of(policy: dict[str, Any], role: str) -> dict[str, Any] | None:
    roles = policy.get("roles")
    rule = roles.get(role) if isinstance(roles, dict) else None
    return rule if isinstance(rule, dict) else None


def allows(rule: dict[str, Any], preset: str, effort: str, account: str) -> bool:
    """Whether ``rule`` lets a visitor run on this choice: the rule's own value always, else an ``allowed``
    entry for the preset whose accounts and efforts (empty meaning any) take the rest."""
    if (preset, effort, account) == (str(rule.get("preset") or ""), str(rule.get("effort") or ""), str(rule.get("account") or "")):
        return True
    for entry in rule.get("allowed") or ():
        if not isinstance(entry, dict) or entry.get("preset") != preset:
            continue
        accounts = list(entry.get("accounts") or ())
        efforts = list(entry.get("efforts") or ())
        if (not accounts or not account or account in accounts) and (not efforts or not effort or effort in efforts):
            return True
    return False


def choice(rule: dict[str, Any], pick: dict[str, Any] | None) -> dict[str, str]:
    """What a role runs on in a visitor's turn: the visitor's pick when the rule allows it, else the rule's value.

    A pick the operator has since narrowed away falls back to the value: a tightened policy holds at once."""
    value = {"preset": str(rule.get("preset") or ""), "effort": str(rule.get("effort") or ""), "account": str(rule.get("account") or "")}
    if isinstance(pick, dict):
        picked = {key: str(pick.get(key) or "") for key in ("preset", "effort", "account")}
        if picked["preset"] and allows(rule, picked["preset"], picked["effort"], picked["account"]):
            return picked
    return value


def members(rule: dict[str, Any]) -> list[str]:
    """The presets of a rule's set, its value first: what the agent may start a helper on in a visitor's turn."""
    found = [str(rule.get("preset") or "")]
    found += [str(entry.get("preset") or "") for entry in rule.get("allowed") or () if isinstance(entry, dict)]
    return [preset for preset in dict.fromkeys(found) if preset]


def agent_pick(rule: dict[str, Any], preset: str | None, effort: str | None) -> dict[str, str]:
    """What a helper the agent starts in a visitor's turn runs on, for a role whose set the agent chooses from.

    ``preset`` None is the rule's value. ``effort`` None is the value's effort on the value's preset, and on
    another preset the first effort its entry names (else the preset's own). The account is never the
    agent's to choose: the value's on the value's preset, else the first its entry names. A preset or an
    effort outside the set is a ``ValueError`` saying what is inside, as the installation's own list
    refuses a model it does not hold rather than running the helper on something else."""
    chosen = preset or str(rule.get("preset") or "")
    inside = members(rule)
    if chosen not in inside:
        raise ValueError(f"model {chosen!r} is not one this live link lets helpers run on; allowed: {', '.join(inside)}")
    own = chosen == str(rule.get("preset") or "")
    entry = next((item for item in rule.get("allowed") or () if isinstance(item, dict) and item.get("preset") == chosen), None)
    listed = [str(item) for item in (entry or {}).get("efforts") or ()]
    if effort is None:
        effort = str(rule.get("effort") or "") if own else (listed[0] if listed else "")
    if own:
        account = str(rule.get("account") or "")
    else:
        accounts = [str(item) for item in (entry or {}).get("accounts") or ()]
        account = accounts[0] if accounts else ""
    if not allows(rule, chosen, effort, account):
        offered = sorted({*listed, *([str(rule.get("effort") or "")] if own else [])} - {""})
        raise ValueError(f"effort {effort!r} is not one this live link lets helpers use on {chosen!r}; allowed: {', '.join(offered) or 'the model own effort'}")
    return {"preset": chosen, "effort": effort, "account": account}


def pick_of(metadata: dict[str, Any], share_id: str) -> dict[str, Any] | None:
    picks = metadata.get(PICKS_KEY)
    found = picks.get(share_id) if isinstance(picks, dict) else None
    return found if isinstance(found, dict) else None


def applied(overrides: dict[str, Any], chosen: dict[str, str], presets: dict[str, Any]) -> dict[str, Any]:
    """The session's live overrides with a link's choice put over them, for one run's engine.

    A preset that is no longer in the table leaves the model alone rather than failing the visitor's
    turn; the effort is still applied. ``none`` is no thinking, as everywhere a task names an effort."""
    result = dict(overrides)
    if chosen["preset"] and chosen["preset"] in presets:
        result.update(preset=chosen["preset"], provider=None, model_name=None)
    effort = chosen["effort"]
    if effort == "none":
        result.update(thinking_enabled=False, reasoning_effort=None)
    elif effort:
        result.update(thinking_enabled=True, reasoning_effort=effort)
    return result


__all__ = ["PICKS_KEY", "agent_pick", "allows", "applied", "choice", "members", "pick_of", "policy_of", "role_of", "rule_of", "starting_share", "visiting"]
