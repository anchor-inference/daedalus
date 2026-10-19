"""What a command-line staff member is told about its team, whichever CLI it runs in.

A CLI staff member reaches its orchestrator only through two tools, ``Report`` and
``AskOrchestrator``, served per launch by ``ptyd team-mcp``. A model that forgets them — or never
learns of them — works on silently, and the orchestrator hears nothing until the screen goes quiet.
So the protocol is said three times, each where a different CLI will read it:

- a short **mandatory block**, placed in the CLI's own system-level channel (Claude's appended system
  prompt, Codex's developer instructions, an agent prompt, an instruction file) and repeated as the
  first line of the first prompt;
- the **``daedalus-team`` skill**, for CLIs that load skills, with the examples and the etiquette the
  block has no room for; generated per launch into the launch directory, never the project folder;
- the tools' own descriptions (``ptyd team-mcp``), for a CLI that loads neither.

The texts are built here once and handed to every adapter through ``LaunchSpec``; an adapter only
decides where its CLI reads them. They are English on purpose: they are read by models, not shown
to the operator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from daedalus.harness.contract import mcp_server

SERVER = "daedalus_team"
"""The MCP server every command-line member's team tools are served under."""

SKILL_NAME = "daedalus-team"
SKILL_PATH = f".claude/skills/{SKILL_NAME}/SKILL.md"
"""Where the skill sits under a directory a CLI is told to look in; the layout Claude Code reads."""

EXCERPT_CHARS = 600
"""How much of a turn's last message an implicit report carries: enough to judge the turn, short
enough that the orchestrator reads it rather than skims it."""

NO_REPORT = "no report — ReadStaff for details"


@dataclass(frozen=True, slots=True)
class TeamFacts:
    """Who the member is and what it works on, for the texts below."""

    staff_name: str
    project_name: str
    role: str = ""
    task_id: str = ""
    task_title: str = ""
    branch: str = ""
    """The member's own branch when it works in a worktree: "done" then means committed there."""


def mandatory_block(facts: TeamFacts) -> str:
    """The few lines every CLI member reads before anything else. Kept under ten lines: it rides in
    the system prompt of every turn."""
    role = f" ({facts.role})" if facts.role else ""
    done = (
        f"commit your work on the branch {facts.branch}, then call Report with kind done"
        if facts.branch
        else "call Report with kind done, saying what you produced and where"
    )
    return "\n".join(
        [
            f"You are {facts.staff_name}{role}, a staff member of the project {facts.project_name}, working under its orchestrator.",
            "Talk to your team only through two tools: Report (kind checkpoint, needs_input, stuck or done, with a short note) "
            "and AskOrchestrator (a question you cannot settle yourself, with the options you see).",
            "Never ask the person at this terminal: nobody reads it. A question goes to AskOrchestrator; if its answer is "
            "pending, end your turn — the answer arrives as your next message.",
            f"When the task's done-when is met, {done}. A turn that ends without a Report leaves your team guessing.",
            "Files handed to you are in .agents/inbox/<task>/ of your folder. To hand files back, keep them in your folder "
            "and name their paths in Report's artifacts; a file only on another machine reaches nobody.",
            f"The {SKILL_NAME} skill, where you have skills, has examples of good reports and when to ask.",
        ]
    )


def first_line(facts: TeamFacts) -> str:
    """The block's one-line reminder, put at the top of the first prompt: a CLI that shows its system
    prompt to the model late, or not at all, still starts the task knowing the protocol."""
    return (
        f"[team] You are {facts.staff_name}, staff of {facts.project_name}. Report progress and results with the Report "
        "tool and ask with AskOrchestrator; never ask the terminal."
    )


def skill_markdown(facts: TeamFacts) -> str:
    """The ``daedalus-team`` skill: what the block cannot fit. Written for the launch, so it can name
    the member's branch."""
    where = (
        f"You work in your own git worktree on the branch `{facts.branch}`. Commit there as you go, with clear "
        "messages; never switch branches, never touch the operator's checkout, never push. Report(done) is refused "
        "while the worktree has uncommitted changes: commit first."
        if facts.branch
        else "You work in a folder other members may be working in too: change only what your task needs, and "
        "leave files you did not create alone."
    )
    return f"""---
name: {SKILL_NAME}
description: How {facts.staff_name}, a staff member of the project {facts.project_name}, reports to and asks its orchestrator. Use it whenever you are about to report progress, ask a question, hand work over or finish a turn.
---

# Working as a staff member

You were given one task by your project's orchestrator. Nobody watches your terminal: the orchestrator
hears from you only through two tools, and the operator only through the orchestrator.

## Report

`Report(kind, note, artifacts?, remember?)` — the kinds:

- `checkpoint`: progress worth knowing. "Parser rewritten and its 14 tests pass; the CLI flag is next."
- `needs_input`: you cannot go on without a decision. Say what the decision is and what you would pick.
- `stuck`: something outside your task blocks you. "The staging database refuses connections since 10:40."
- `done`: the task's done-when is met. "menu.md written from the three price lists; checked every item
  against the source; nothing else touched." Put the paths you produced in `artifacts`.

A good note is short and factual: what was done, where it is, how it was checked. `remember` keeps one line
in your notes for every later session ("tests run with `make check`, not pytest").

End every turn with a Report. A turn that ends without one is passed on as "no report", with the last thing
you wrote, and the orchestrator has to read your session to find out what happened.

## AskOrchestrator

`AskOrchestrator(question, options?, context?)` asks what you cannot settle yourself: a choice the brief does
not make, a boundary you would have to cross. Give the options you see and the context needed to decide
without reading your whole session. Ask once. If the result says the answer is pending, end your turn: the
answer arrives as your next message. Do not ask the person at the terminal, and do not stop to wait for
permission the brief already gives you: decide what the brief allows, ask about the rest.

## Where you work

{where}

## Handing over

When you finish or stop, the next session (yours or another member's) starts from what you leave: committed
work, a done or checkpoint Report that says where things are, and notes worth remembering. Leave nothing only
in your head.
"""


def is_team_tool(tool: str) -> bool:
    """Whether a CLI's permission request is for one of the team's own tools.

    Those tools only talk to the member's own orchestrator, so they are never a member's to be
    granted: a request for one is answered by the host at once. Claude Code in plan mode asks before
    every MCP tool whatever its allow list says, and a read-only member's ``Report`` once waited on
    the orchestrator five times in one session; one such request was denied, and the orchestrator,
    believing the tool itself was withheld, told the member to answer in plain text instead. Grok
    names the tools ``daedalus_team__<tool>``, the others ``mcp__daedalus_team__<tool>``."""
    return mcp_server(tool) == SERVER or tool.startswith(f"{SERVER}__")


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The end of a message on one line: a turn's last words carry its conclusion."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else "…" + flat[-(limit - 1):]


_QUESTION_TAIL = re.compile(r"\?\s*[)\]\"'*_`]*\s*$")
_ASKING = re.compile(
    r"\b(should i|shall i|do you want|would you like|can you (confirm|clarify|tell)|please (confirm|clarify|let me know)|"
    r"let me know (if|whether|which)|which (one|option) (do|would) you|what would you prefer)\b",
    re.IGNORECASE,
)


def looks_like_question(text: str) -> bool:
    """Whether a turn's last message ends by asking the person at the terminal something. Only the
    closing lines count: a question quoted in the middle of a report is not one asked."""
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return False
    tail = " ".join(lines[-2:])
    return bool(_QUESTION_TAIL.search(lines[-1]) or _ASKING.search(tail))


__all__ = [
    "EXCERPT_CHARS",
    "NO_REPORT",
    "SERVER",
    "SKILL_NAME",
    "SKILL_PATH",
    "TeamFacts",
    "excerpt",
    "first_line",
    "is_team_tool",
    "looks_like_question",
    "mandatory_block",
    "skill_markdown",
]
