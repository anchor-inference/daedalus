"""One call's output is bounded, and Read says what it is showing you."""

from __future__ import annotations

from pathlib import Path

from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig
from daedalus.host.services import SessionServices, locator
from daedalus.tools._common import error, ok, output_limit
from daedalus.tools.files import read_file


def _ctx(session_id: str) -> ToolContext:
    return ToolContext(tenant_id="t", run_id="r", session_id=session_id, metadata={"tool_call_id": "c"})


def test_ok_clips_every_result_to_the_session_budget(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="cap", workspace_dir=tmp_path, max_tool_output_chars=2_000))
    ctx = _ctx("cap")
    try:
        result = ok(ctx, "x" * 50_000, note="kept")
        assert len(result.content) < 2_100
        assert "characters omitted" in result.content
        # Metadata is the tool's own record of the call and is never touched.
        assert result.metadata == {"note": "kept"}
        # A result already under the budget passes through byte for byte.
        assert ok(ctx, "short").content == "short"
    finally:
        locator.unregister("cap")


def test_a_tool_called_outside_a_session_falls_back_to_the_configured_default() -> None:
    ctx = _ctx("no-such-session")
    assert output_limit(ctx) == RuntimeConfig().tools.exec.max_output_chars
    assert len(ok(ctx, "x" * 200_000).content) < 61_000


def test_an_error_is_clipped_on_the_same_terms_as_a_success(tmp_path: Path) -> None:
    # A failure is not automatically small — a build that dies after ten
    # thousand lines of output is a tool result the model carries like any
    # other, so it is under the same budget.
    locator.register(SessionServices(session_id="err", workspace_dir=tmp_path, max_tool_output_chars=2_000))
    ctx = _ctx("err")
    try:
        result = error(ctx, "head" + "y" * 50_000 + "tail", note="kept")
        assert result.is_error and len(result.content) < 2_100
        # Head and tail both survive: the line that names a failure is as often
        # the last one as the first.
        assert result.content.startswith("head") and result.content.endswith("tail")
        assert "characters omitted" in result.content
        assert result.metadata == {"note": "kept"}
        assert error(ctx, "short").content == "short"
    finally:
        locator.unregister("err")


async def test_a_failing_command_still_names_its_spill_file(tmp_path: Path) -> None:
    # The framed error paths clip their body with the frame reserve, so the
    # second clip in error() never lands on the line saying where the rest went.
    from daedalus.tools.shell import exec_command

    locator.register(SessionServices(session_id="fail", workspace_dir=tmp_path, max_tool_output_chars=2_000))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="fail", metadata={"tool_call_id": "call-fail"})
    try:
        result = await exec_command().invoke(ctx, {"command": "for i in $(seq 1 3000); do echo line-$i; done; exit 3"})
        spill = tmp_path / ".exec" / "call-fail.log"
        assert result.is_error and result.metadata["exit_code"] == 3
        assert str(spill) in result.content and len(result.content) <= 2_000
    finally:
        locator.unregister("fail")


async def test_a_long_criterion_cannot_clip_the_line_that_says_where_the_output_went(tmp_path: Path) -> None:
    # The receipt line carries the criterion, which the model writes and nothing
    # bounds. Long enough, it pushes the framed result over the budget, ok()
    # clips a second time, and that clip drops the middle — where the note
    # saying where the rest of the output went is written.
    from daedalus.tools.verify import verify

    locator.register(SessionServices(session_id="receipt", workspace_dir=tmp_path, max_tool_output_chars=2_000))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="receipt", metadata={"tool_call_id": "call-receipt"})
    try:
        result = await verify().invoke(
            ctx,
            {
                "criterion": "the suite passes " * 300,
                "command": "for i in $(seq 1 2000); do echo line-$i; done",
                "dependencies": "a container shell " * 100,
            },
        )
        first = result.content.splitlines()[0]
        assert first.startswith("✅ verified") and len(first) < 700
        assert "write the output to a file for the rest" in result.content
        assert len(result.content) <= 2_000
    finally:
        locator.unregister("receipt")


async def test_read_refuses_a_binary_file(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="bin", workspace_dir=tmp_path))
    try:
        (tmp_path / "blob.bin").write_bytes(b"MZ\x90\x00" + b"\x00\x01\x02" * 4_000)
        result = await read_file().invoke(_ctx("bin"), {"path": "blob.bin"})
        assert result.is_error
        assert "is binary" in result.content and "12004 bytes" in result.content
        assert "ImageView" in result.content and "xxd" in result.content
    finally:
        locator.unregister("bin")


async def test_read_refuses_a_file_whose_nul_sits_inside_valid_text(tmp_path: Path) -> None:
    # A NUL decodes perfectly well as UTF-8, so the decoder alone would let this
    # through and hand the model a page of control characters.
    locator.register(SessionServices(session_id="nul", workspace_dir=tmp_path))
    try:
        (tmp_path / "mixed").write_bytes(b"header\n" + b"\x00" + b"tail\n")
        result = await read_file().invoke(_ctx("nul"), {"path": "mixed"})
        assert result.is_error and "is binary" in result.content
    finally:
        locator.unregister("nul")


async def test_read_says_the_lines_are_a_slice_of_a_large_file(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="big", workspace_dir=tmp_path, max_tool_output_chars=4_000))
    try:
        (tmp_path / "big.txt").write_text("".join(f"line {i}\n" for i in range(5_000)), encoding="utf-8")
        result = await read_file().invoke(_ctx("big"), {"path": "big.txt"})
        first = result.content.splitlines()[0]
        assert first.startswith("[") and "5000 lines" in first and "showing lines 1-" in first
        assert "continue with offset=" in result.content
        assert len(result.content) <= 4_000
    finally:
        locator.unregister("big")


async def test_read_says_so_when_the_whole_file_is_one_long_line(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="min", workspace_dir=tmp_path, max_tool_output_chars=4_000))
    try:
        (tmp_path / "bundle.js").write_text("var a=1;" * 5_000, encoding="utf-8")
        result = await read_file().invoke(_ctx("min"), {"path": "bundle.js"})
        assert "on a single line" in result.content.splitlines()[0]
        assert len(result.content) <= 4_000
    finally:
        locator.unregister("min")


async def test_read_cannot_be_talked_past_the_budget_by_a_large_limit(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="ask", workspace_dir=tmp_path, max_tool_output_chars=4_000))
    try:
        (tmp_path / "wide.txt").write_text("".join("w" * 500 + "\n" for _ in range(2_000)), encoding="utf-8")
        result = await read_file().invoke(_ctx("ask"), {"path": "wide.txt", "limit": 100_000})
        assert len(result.content) <= 4_000
        assert "continue with offset=" in result.content
    finally:
        locator.unregister("ask")


async def test_a_small_file_is_returned_whole_with_no_header(tmp_path: Path) -> None:
    locator.register(SessionServices(session_id="small", workspace_dir=tmp_path))
    try:
        (tmp_path / "s.txt").write_text("one\ntwo\n", encoding="utf-8")
        result = await read_file().invoke(_ctx("small"), {"path": "s.txt"})
        assert result.content == "     1\tone\n     2\ttwo"
    finally:
        locator.unregister("small")


def test_the_result_budget_defaults_are_the_documented_ones() -> None:
    results = RuntimeConfig().tools.results
    assert (results.fresh_count, results.stale_max_chars, results.trim_batch_chars, results.trim_min_share) == (6, 2_000, 40_000, 0.25)


def test_the_run_is_built_with_stale_trimming_on() -> None:
    from daedalus.host.engine_factory import runtime_constants

    config = RuntimeConfig()
    rc = runtime_constants(config, context_window=128_000, max_output_tokens=32_000, thinking=True)
    assert rc.tool_result_stale_trim_enabled is True
    assert rc.tool_result_fresh_count == config.tools.results.fresh_count
    assert rc.tool_result_stale_max_chars == config.tools.results.stale_max_chars
    assert rc.tool_result_stale_trim_batch_chars == config.tools.results.trim_batch_chars
    assert rc.tool_result_stale_trim_min_share == config.tools.results.trim_min_share


async def test_read_finds_a_file_whose_name_the_disk_keeps_decomposed(tmp_path: Path) -> None:
    # Uploaded from macOS, the name keeps "й" as "и" and a combining breve; the model writes "й".
    import unicodedata

    locator.register(SessionServices(session_id="nfd", workspace_dir=tmp_path))
    try:
        name = "3_Заявление_о_регистрации.txt"
        (tmp_path / unicodedata.normalize("NFD", name)).write_text("заявление\n", encoding="utf-8")
        result = await read_file().invoke(_ctx("nfd"), {"path": unicodedata.normalize("NFC", name)})
        assert not result.is_error and "заявление" in result.content
    finally:
        locator.unregister("nfd")
