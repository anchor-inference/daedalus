"""What a command-line member is told to call: the team tools by the name its own CLI gives them.

A Claude Code member told to "call Report" called ``Report``, got "No such tool available: Report",
and found ``mcp__daedalus_team__Report`` only on its second try; its first ``ToolSearch`` for
``select:Report,AskOrchestrator`` had found nothing at all.
"""

from __future__ import annotations

import pytest

from daedalus.harness import team
from daedalus.harness.team import TeamFacts, first_line, mandatory_block, skill_markdown, tool_name

NAMES = {
    "claude": ("mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator"),
    "codex": ("mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator"),
    "grok": ("daedalus_team__Report", "daedalus_team__AskOrchestrator"),
    "opencode": ("daedalus_team_Report", "daedalus_team_AskOrchestrator"),
    "pi": ("Report", "AskOrchestrator"),
}


def facts(harness: str) -> TeamFacts:
    return TeamFacts(staff_name="Ada", project_name="Bakery", harness=harness)


@pytest.mark.parametrize("harness", sorted(NAMES))
def test_every_text_names_the_tools_as_the_members_cli_calls_them(harness: str) -> None:
    report, ask = NAMES[harness]
    assert (tool_name(harness, "Report"), tool_name(harness, "AskOrchestrator")) == (report, ask)
    block, line, skill = mandatory_block(facts(harness)), first_line(facts(harness)), skill_markdown(facts(harness))
    for text in (block, line, skill):
        assert report in text and ask in text, (harness, text)
    if report != "Report":
        # Briefs, requirements and the orchestrator's messages say "Report(kind=…)": the block says
        # once what that word means in this CLI, so none of them has to be rewritten per harness.
        assert f"says Report or AskOrchestrator, it means {report} and {ask}" in block
        assert f"`{report}(kind, note" in skill and f"`{ask}(question" in skill


def test_claude_is_told_its_team_tools_may_need_loading_and_may_still_be_connecting() -> None:
    block = mandatory_block(facts("claude"))
    assert 'ToolSearch with the query "select:mcp__daedalus_team__Report,mcp__daedalus_team__AskOrchestrator"' in block
    assert "still connecting" in block
    assert "search_tool" in mandatory_block(facts("grok"))


def test_a_cli_whose_naming_is_not_known_is_pointed_at_the_server() -> None:
    assert tool_name("cursor", "Report") == "Report"
    assert f"the {team.SERVER} MCP server" in mandatory_block(facts("cursor"))
    assert team.SERVER not in mandatory_block(facts("pi"))


def test_the_block_stays_short() -> None:
    # It rides in the system prompt of every turn.
    assert all(len(mandatory_block(facts(h)).splitlines()) <= 10 for h in [*NAMES, "cursor"])
