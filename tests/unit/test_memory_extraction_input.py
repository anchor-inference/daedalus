"""What the memory extraction reads of a run: its end as well as its start."""

from __future__ import annotations

from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

from daedalus.extensions import learning


def _op(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})


def test_a_long_run_keeps_its_last_operator_message_and_its_first() -> None:
    # The input was the first 60,000 characters, so on a long run the operator's closing decision —
    # the likeliest durable fact — was never read at all.
    filler = [Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=f"step {i} " + "y" * 4000)]) for i in range(50)]
    history = [_op("first ask: keep reports in docs/reports"), *filler, _op("final decision: deploy only from the release branch")]
    text = learning.extraction_input(history)
    assert len(text) <= learning.EXTRACT_INPUT_CHARS + 200
    assert "final decision: deploy only from the release branch" in text
    assert "first ask: keep reports in docs/reports" in text


def test_a_tool_result_is_read_past_its_first_three_hundred_characters() -> None:
    body = "z" * 700 + " the token lives in secrets/ci.env line 4 " + "z" * 700
    history = [
        _op("go"),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c", name="Exec", arguments_json='{"command": "grep -n TOKEN"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c", content=body)]),
    ]
    assert "secrets/ci.env line 4" in learning.extraction_input(history)


def test_the_extraction_prompt_says_the_omission_marker_is_the_hosts() -> None:
    assert "chars left out" in learning.EXTRACT_PROMPT and "not part of the tool output" in learning.EXTRACT_PROMPT
