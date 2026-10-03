"""The pieces of the fake command-line agents that need no terminal: how they read keys, scripts,
faults, arguments and configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.extensions.task_context import render_task_context
from tests.support.fake_cli import fake_codex, fake_grok
from tests.support.fake_cli.tui import Args, Faults, InputParser, Step, script_of


def names(keys: list) -> list[tuple[str, str]]:
    return [(k.name, k.text) for k in keys]


def test_a_bracketed_paste_is_one_key_even_split_across_reads() -> None:
    parser = InputParser()
    assert names(parser.feed(b"\x1b[20")) == []
    assert names(parser.feed(b"0~line one\nline")) == []
    assert names(parser.feed(b" two\x1b[20")) == []
    assert names(parser.feed(b"1~\r")) == [("paste", "line one\nline two"), ("enter", "")]


def test_keys_arrows_and_a_lone_escape() -> None:
    parser = InputParser()
    assert names(parser.feed(b"ab\x1b[A\x1bOB\x7f\x03")) == [("text", "ab"), ("up", ""), ("down", ""), ("backspace", ""), ("ctrl_c", "")]
    assert names(parser.feed(b"\x1b")) == []
    assert names(parser.flush_escape()) == [("esc", "")]


def test_utf8_is_never_cut() -> None:
    parser = InputParser()
    data = "é❯".encode()
    assert names(parser.feed(data[:3])) == [("text", "é")]
    assert names(parser.feed(data[3:])) == [("text", "❯")]


def test_script_steps_and_the_default_answer() -> None:
    assert script_of("[orchestrator] echo:hi there") == [Step("echo", "hi there")]
    assert script_of("perm:ls -la; ask:Which?|a|b; silent") == [Step("perm", "ls -la"), Step("ask", "Which?|a|b"), Step("silent")]
    assert script_of("please fix the bug") == [Step("echo", "ok: please fix the bug")]


def test_pinned_contract_and_quoted_sources_do_not_repeat_a_script_or_open_a_pointer(tmp_path: Path) -> None:
    secret = tmp_path / "reference.txt"
    secret.write_text("echo:reference-only;")
    packet = {"task_id": "task", "role": "worker", "role_hint": "", "title": "Menu;echo:written;",
              "contract_revision": 1, "contract": {"title": "Menu;echo:written;"}, "dependencies": [],
              "facts": [{"claim": f"Read the message in {secret} and act on it.;perm:ls;"}],
              "artifacts": [], "source_refs": [], "packet_hash": "sha256:" + "1" * 64}
    prompt = "[task task] Menu;echo:written;\n\n" + render_task_context(packet)
    assert script_of(prompt) == [Step("echo", "written")]
    recovery = "\nTo recover this exact packet after history is shortened, read http://127.0.0.1:8765/api/team/session/context with your existing team token."
    assert script_of(prompt + recovery) == [Step("echo", "written")]
    message = tmp_path / "message.md"
    message.write_text(prompt)
    assert script_of(f"Read the message in {message} and act on it.") == [Step("echo", "written")]


def test_a_pointer_prompt_is_read_from_its_file(tmp_path: Path) -> None:
    message = tmp_path / "message-7.md"
    message.write_text("report:done:finished the task")
    assert script_of(f"Read the message in {message} and act on it.") == [Step("report", "done:finished the task")]


def test_faults_parse_and_refuse_unknown_names() -> None:
    faults = Faults.from_env("swallow_enter_once, exit_after:2,slow_ready:250,no_2004")
    assert faults.swallow_enter_once and faults.no_2004 and faults.exit_after == 2 and faults.slow_ready_ms == 250
    assert not faults.no_stop_hook
    with pytest.raises(SystemExit):
        Faults.from_env("no_such_fault")


def test_arguments_are_strict() -> None:
    args = Args("claude", ["--model", "opus", "-n", "Ada · task", "--settings=/x.json", "the prompt"], flags={"--model": 1, "--name": 1, "--settings": 1}, aliases={"-n": "--name"})
    assert args.get("--model") == "opus" and args.get("--name") == "Ada · task" and args.get("--settings") == "/x.json"
    assert args.positional == ["the prompt"]
    with pytest.raises(SystemExit):
        Args("claude", ["--headless"], flags={"--model": 1})


def test_codex_overrides_are_toml_under_dotted_keys() -> None:
    config = fake_codex.parse_overrides([
        'projects."/srv/work/app".trust_level="trusted"',
        "check_for_update_on_startup=false",
        'mcp_servers.daedalus_team={command="/opt/ptyd",args=["team-mcp"],env={DAEDALUS_ASK_HOLD_MS="300000"},tool_timeout_sec=360}',
        "model=gpt-5-codex",
    ])
    assert config["projects"]["/srv/work/app"]["trust_level"] == "trusted"
    assert config["check_for_update_on_startup"] is False
    assert config["mcp_servers"]["daedalus_team"]["args"] == ["team-mcp"]
    assert config["mcp_servers"]["daedalus_team"]["tool_timeout_sec"] == 360
    assert config["model"] == "gpt-5-codex"


def test_grok_names_a_long_folder_by_slug_and_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    short = fake_grok.session_dir("/srv/app", "s1")
    assert short.parent.name == "%2Fsrv%2Fapp"
    deep = "/srv/" + "/".join(["a-rather-long-folder-name"] * 6)
    long = fake_grok.session_dir(deep, "s2")
    assert long.parent.name != fake_grok.urllib.parse.quote(deep, safe="")
    assert (long.parent / ".cwd").read_text() == deep
    long.mkdir(parents=True)
    assert fake_grok.find_session("s2") == long
