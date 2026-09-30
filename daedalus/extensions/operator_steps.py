"""Steps a member writes for the operator to follow themselves, carried to the operator by the host.

A member's instruction for a person — which account is which, what to press, how to check it worked —
used to reach the operator only through the orchestrator's retelling of the report. The retelling
lost the table of accounts and the steps, then a step with the current password; an address the
member never gave was offered as the way in. The instruction is data now: the member reports it as
``operator_steps``, the host keeps it as a file of the project and puts it in front of the operator
word for word, and the orchestrator is told it arrived rather than asked to repeat it.

The member says whether it walked the steps on the version that is running. A step it could not walk
is marked so wherever the steps are shown; the host does not judge it and could not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VERIFIED = ("on-running-version", "unverified")
STEPS_MAX = 30
STEP_MAX_CHARS = 600
FIELD_MAX_CHARS = 1000
ROLES_MAX = 12


class StepsRefused(ValueError):
    """Steps that cannot be carried as they are; the message says what to change."""


@dataclass(frozen=True, slots=True)
class OperatorSteps:
    goal: str
    steps: list[str]
    roles: list[dict[str, str]] = field(default_factory=list)
    expected: str = ""
    check: str = ""
    limits: str = ""
    verified: str = "unverified"
    verified_how: str = ""

    @property
    def walked(self) -> bool:
        return self.verified == "on-running-version"

    def view(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "steps": list(self.steps), "roles": [dict(r) for r in self.roles], "expected": self.expected,
            "check": self.check, "limits": self.limits, "verified": self.verified, "verified_how": self.verified_how,
        }

    def markdown(self, *, member: str, task: str = "") -> str:
        """The steps as the operator reads them, in the file and in the chat alike."""
        lines = [f"# {self.goal}", "", f"From {member}" + (f", task {task}" if task else "") + "."]
        if not self.walked:
            lines += ["", "**Not checked on the running version.**" + (f" {self.verified_how}" if self.verified_how else "")]
        elif self.verified_how:
            lines += ["", f"Checked on the running version: {self.verified_how}"]
        if self.roles:
            lines += ["", "| account | what it is for |", "|---|---|", *(f"| {r['account']} | {r['purpose']} |" for r in self.roles)]
        lines += ["", *(f"{i}. {step}" for i, step in enumerate(self.steps, start=1))]
        for label, value in (("Expected", self.expected), ("How to check", self.check), ("Limits", self.limits)):
            if value:
                lines += ["", f"**{label}:** {value}"]
        return "\n".join(lines) + "\n"


def _text(raw: Any, name: str, limit: int = FIELD_MAX_CHARS) -> str:
    value = " ".join(str(raw or "").split())
    if len(value) > limit:
        raise StepsRefused(f"operator_steps.{name} is at most {limit} characters")
    return value


def normalise(raw: Any) -> OperatorSteps:
    """The steps a report carries, checked; :class:`StepsRefused` names what is wrong."""
    if not isinstance(raw, dict):
        raise StepsRefused("operator_steps is an object: {goal, steps: […], roles?, expected?, check?, limits?, verified, verified_how?}")
    goal = _text(raw.get("goal"), "goal", 200)
    if not goal:
        raise StepsRefused("operator_steps needs its goal: what the operator achieves by following them")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not [s for s in steps if str(s or "").strip()]:
        raise StepsRefused("operator_steps.steps is a list of the steps, one sentence each, in order")
    if len(steps) > STEPS_MAX:
        raise StepsRefused(f"at most {STEPS_MAX} steps; split the instruction")
    cleaned = [_text(s, "steps", STEP_MAX_CHARS) for s in steps if str(s or "").strip()]
    roles: list[dict[str, str]] = []
    for role in raw.get("roles") or []:
        if isinstance(role, dict) and str(role.get("account") or "").strip():
            roles.append({"account": _text(role.get("account"), "roles.account", 120), "purpose": _text(role.get("purpose"), "roles.purpose", 300)})
    if len(roles) > ROLES_MAX:
        raise StepsRefused(f"at most {ROLES_MAX} accounts in roles")
    verified = str(raw.get("verified") or "").strip()
    if verified not in VERIFIED:
        raise StepsRefused("operator_steps.verified is 'on-running-version' (you walked every step on the version that runs) or 'unverified'")
    return OperatorSteps(
        goal=goal, steps=cleaned, roles=roles, expected=_text(raw.get("expected"), "expected"), check=_text(raw.get("check"), "check"),
        limits=_text(raw.get("limits"), "limits"), verified=verified, verified_how=_text(raw.get("verified_how"), "verified_how"),
    )


__all__ = ["VERIFIED", "OperatorSteps", "StepsRefused", "normalise"]
