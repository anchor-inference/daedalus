"""What the harness manager reads from each CLI and each release feed, against bodies as the real
ones answered, and the install and update commands it would run."""

from __future__ import annotations

import json
from typing import Any

import pytest

from daedalus.harness.contract import ExecResult, InstallInfo, ProgramNotFound
from daedalus.harness.tools import (
    NODE_SHA256,
    NODE_VERSION,
    TOOLING,
    ClaudeTooling,
    install_method,
    installer_script,
    node_install_script,
    npm_prefix,
    npm_tags_url,
    parse_codex_models,
    parse_frontmatter,
    parse_grok_check,
    parse_npm_dist_tags,
    parse_opencode_agents,
    parse_opencode_credentials,
    parse_pi_models,
    parse_plain_version,
    tooling,
)

# As the feeds answered on 2026-09-24 (shortened: the snapshot tags are dozens).
CLAUDE_LATEST = "2.1.281\n"
CODEX_TAGS = {"beta": "0.1.2505172116", "native": "0.1.2505291658", "latest": "0.156.1", "linux-x64": "0.156.1-linux-x64", "alpha": "0.158.0-alpha.9"}
OPENCODE_TAGS = {"snapshot-pnpm": "0.0.0-snapshot-pnpm-202511302105", "latest-0": "1.0.142", "latest-1": "1.1.4", "tui-v2": "0.0.0-tui-v2-202606261840", "beta": "0.0.0-beta-202608110357", "latest": "1.18.32", "dev": "0.0.0-dev-202609221946"}
PI_TAGS = {"legacy-node20": "0.74.2", "latest": "0.87.1"}
GROK_CHECK = {"currentVersion": "1.0.41", "latestVersion": "1.0.41", "updateAvailable": False, "installer": "internal", "channel": "stable", "autoUpdate": None, "error": None}


def test_the_release_feeds_parse_to_the_version_to_install() -> None:
    assert parse_plain_version(CLAUDE_LATEST) == "2.1.281"
    with pytest.raises(ValueError):
        parse_plain_version("<html>maintenance</html>")
    assert parse_npm_dist_tags(json.dumps(CODEX_TAGS)) == "0.156.1"
    assert parse_npm_dist_tags(json.dumps(PI_TAGS)) == "0.87.1"
    assert parse_npm_dist_tags(json.dumps(OPENCODE_TAGS), major=1) == "1.18.32"
    # The day OpenCode's latest becomes 2.x, the pinned major still gets the newest 1.x any tag names,
    # and never a snapshot or a pre-release.
    moved = {**OPENCODE_TAGS, "latest": "2.0.3", "v1": "1.19.2", "next-1": "1.20.0-rc.1"}
    assert parse_npm_dist_tags(json.dumps(moved), major=1) == "1.19.2"
    with pytest.raises(ValueError):
        parse_npm_dist_tags(json.dumps({"latest": "2.0.3"}), major=1)
    with pytest.raises(ValueError):
        parse_npm_dist_tags("[]")
    assert parse_grok_check(json.dumps(GROK_CHECK)) == GROK_CHECK
    assert parse_grok_check('{"version": "0.1.0"}') is None and parse_grok_check("usage: grok [options]") is None
    assert npm_tags_url("@openai/codex") == "https://registry.npmjs.org/-/package/@openai%2Fcodex/dist-tags"


def test_the_lists_the_clis_print_are_read_by_name_only() -> None:
    assert parse_codex_models(json.dumps({"models": [{"id": "gpt-5-codex"}, {"id": "gpt-5"}, {"id": "gpt-5"}]})) == ("gpt-5-codex", "gpt-5")
    assert parse_codex_models('["a", "b"]') == ("a", "b") and parse_codex_models("not json") == ()
    agents = parse_opencode_agents("build (primary)\nplan (primary)\ngeneral (subagent)\n", source="user")
    assert [(a.name, a.description, a.source) for a in agents] == [("build", "primary", "user"), ("plan", "primary", "user"), ("general", "subagent", "user")]
    credentials = "Credentials ~/.local/share/opencode/auth.json\n●  Anthropic oauth\n●  OpenAI api\n\n2 credentials"
    assert parse_opencode_credentials(credentials) == ("Anthropic", "OpenAI")
    assert parse_opencode_credentials("Credentials ~/.local/share/opencode/auth.json\n\n0 credentials") == ()
    assert parse_pi_models("provider   model\nanthropic  claude-sonnet-4\nopenai     gpt-5\n") == ("anthropic/claude-sonnet-4", "openai/gpt-5")
    meta = parse_frontmatter("---\nname: code-reviewer\ndescription: \"Reviews a diff\"\nmodel: opus\ntools:\n  - Read\n---\nYou review code.\n")
    assert meta == {"name": "code-reviewer", "description": "Reviews a diff", "model": "opus", "tools": ""}
    assert parse_frontmatter("No header at all") == {}


def test_how_a_cli_was_installed_is_read_from_where_it_lives() -> None:
    assert install_method("/home/operator/.npm-global/bin/codex") == "npm"
    assert install_method("/home/operator/.nvm/versions/node/v24.14.0/bin/pi") == "npm"
    assert install_method("/usr/lib/node_modules/opencode-ai/bin/opencode") == "npm"
    assert install_method("/home/operator/.local/bin/claude") == "native"
    assert install_method("/home/operator/.opencode/bin/opencode") == "installer"
    assert install_method("/home/operator/.grok/bin/grok") == "installer"
    assert install_method("/opt/tools/claude") == "" and install_method("") == ""
    assert npm_prefix("/home/operator/.npm-global/bin/codex") == "/home/operator/.npm-global"
    assert npm_prefix("/home/operator/.nvm/versions/node/v24.14.0/bin/pi") == ""


def test_updates_keep_the_install_method_and_the_pinned_major() -> None:
    codex, opencode, claude, grok, pi = (tooling(n) for n in ("codex", "opencode", "claude", "grok", "pi"))
    npm = InstallInfo(True, "/home/operator/.npm-global/bin/codex", "0.155.1", "npm")
    plan = codex.update_plan("container", npm, "0.156.1")
    assert plan.argv == ("npm", "install", "--global", "--no-fund", "--no-audit", "--prefix", "/home/operator/.npm-global", "@openai/codex@0.156.1") and plan.needs_node
    assert codex.update_plan("host", InstallInfo(True, "/opt/codex", "0.155.1", ""), "0.156.1").argv == ("codex", "update")
    # OpenCode's own upgrader is always told the exact 1.x version: without one it installs 2.x.
    assert opencode.update_plan("host", InstallInfo(True, "/home/operator/.opencode/bin/opencode", "1.18.23", "installer"), "1.18.32").argv == ("opencode", "upgrade", "1.18.32")
    assert claude.update_plan("container", InstallInfo(True, "/home/operator/.local/bin/claude", "2.1.200", "native"), "2.1.281").argv == ("claude", "update")
    assert grok.update_plan("host", InstallInfo(True, "/home/operator/.grok/bin/grok", "1.0.40", "internal"), "1.0.41").argv == ("grok", "update", "--version", "1.0.41")
    assert pi.update_plan("host", InstallInfo(True, "/home/operator/.nvm/versions/node/v24.14.0/bin/pi", "0.84.2", "npm"), "0.87.1").argv[-1] == "@earendil-works/pi-coding-agent@0.87.1"


def test_installs_go_into_the_container_home_or_through_the_vendor_installer() -> None:
    assert tooling("pi").install_plan("container", "/home/operator", "0.87.1").argv == ("npm", "install", "--global", "--no-fund", "--no-audit", "--prefix", "/home/operator/.npm-global", "@earendil-works/pi-coding-agent@0.87.1")
    assert tooling("codex").install_plan("host", "/home/operator", "0.156.1").argv == ("npm", "install", "--global", "--no-fund", "--no-audit", "@openai/codex@0.156.1")
    claude = tooling("claude").install_plan("container", "/home/operator", "")
    assert claude.argv == () and "https://claude.ai/install.sh" in claude.script and not claude.needs_node
    assert "https://x.ai/cli/install.sh" in tooling("grok").install_plan("host", "/home/operator", "").script
    # Downloaded to a file and then run, so a failed download fails the script instead of running half of one.
    script = installer_script("https://example.invalid/install.sh")
    assert "set -eu" in script and 'curl -fsSL -o "$tmp"' in script and "| bash" not in script


def test_the_pinned_node_is_checked_against_its_checksum_before_it_is_unpacked() -> None:
    script = node_install_script()
    assert f"v{NODE_VERSION}/$name.tar.gz" in script and "sha256sum -c -" in script
    assert all(checksum in script for checksum in NODE_SHA256.values())
    assert all(len(checksum) == 64 and int(checksum, 16) >= 0 for checksum in NODE_SHA256.values())
    assert script.index("sha256sum -c -") < script.index("tar -xzf")
    for program in ("node", "npm", "npx"):
        assert program in script.split("for program in", 1)[1]


def test_the_self_check_picks_the_cheap_model_of_each_family() -> None:
    assert tooling("claude").cheapest_model(["fable", "opus", "sonnet", "haiku"]) == "haiku"
    assert tooling("codex").cheapest_model(["gpt-5-codex", "gpt-5-codex-mini", "gpt-5"]) == "gpt-5-codex-mini"
    assert tooling("grok").cheapest_model(["grok-4", "grok-code-fast"]) == "grok-code-fast"
    assert tooling("pi").cheapest_model(["openai/gpt-5"]) == ""


def test_every_cli_has_tooling_and_a_way_to_sign_in() -> None:
    assert list(TOOLING) == ["claude", "codex", "opencode", "pi", "grok"]
    assert all(t.sign_in for t in TOOLING.values())
    # The subscription is the sign-in wherever the CLI asks which one.
    assert tooling("claude").sign_in == ("claude", "auth", "login", "--claudeai")
    assert tooling("codex").sign_in == ("codex", "login", "--device-auth")
    with pytest.raises(KeyError):
        tooling("daedalus")


# What ``git grep --only-matching`` found in the native Claude Code 2.1.284 program, a line per match
# (shortened): dated twins, versions the picker dropped, and a longer id no model has.
CLAUDE_PROGRAM_IDS = """claude-opus-4-20250514
claude-haiku-4-5
claude-sonnet-3-7
claude-opus-4-1-20250805
claude-opus-5-5
claude-fable-5
claude-haiku-3-55
claude-opus-4-8
claude-opus-4
claude-sonnet-4-5-20250929
claude-sonnet-5-5
claude-opus-4-1
claude-fable-5-1
claude-opus-5
claude-sonnet-4-0
claude-sonnet-5
claude-opus-5-5
"""


class ProgramPort:
    """An environment with ``claude`` on PATH as a link, answering ``git grep`` in the places named."""

    name = "host"
    home = "/home/operator"

    def __init__(self, found: dict[str, str], *, version: str = "2.1.284 (Claude Code)") -> None:
        self.found = found
        self.version = version
        self.greps: list[str] = []

    async def run(self, argv: list[str], *, cwd: str | None = None, env: Any = None, timeout: float = 30.0) -> ExecResult:
        if argv[:2] == ["claude", "--version"]:
            return ExecResult(exit_code=0, stdout=self.version, stderr="", path="/home/operator/.local/bin/claude")
        assert argv[0] == "git" and "--no-index" in argv, argv
        place = f"{cwd}/{argv[-1]}"
        self.greps.append(place)
        if place not in self.found:
            return ExecResult(exit_code=1, stdout="", stderr="", path="/usr/bin/git")
        return ExecResult(exit_code=0, stdout=self.found[place], stderr="", path="/usr/bin/git")


def test_claude_models_are_the_undated_versions_its_program_names_newest_first() -> None:
    assert ClaudeTooling.model_ids(CLAUDE_PROGRAM_IDS) == (
        "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-1",
        "claude-fable-5-1", "claude-fable-5",
        "claude-sonnet-5-5", "claude-sonnet-5",
        "claude-haiku-4-5",
    )
    assert ClaudeTooling.model_ids("claude-opus-4-20250514 claude-sonnet-3-7 claude-haiku-3-5") == ()


async def test_claude_models_are_read_through_the_link_and_fall_back_to_the_aliases() -> None:
    ClaudeTooling._scanned.clear()
    native = "/home/operator/.local/share/claude/versions/2.1.284"
    port = ProgramPort({native: CLAUDE_PROGRAM_IDS})
    models = await ClaudeTooling().models(port)  # type: ignore[arg-type]
    assert models[:4] == ("fable", "opus", "sonnet", "haiku") and models[4:6] == ("claude-opus-5-5", "claude-opus-5")
    # The link itself yields nothing (git does not follow it); the versions directory does.
    assert port.greps == ["/home/operator/.local/bin/claude", native]
    assert await ClaudeTooling().models(port) == models and len(port.greps) == 2, "one version is read once"  # type: ignore[arg-type]
    assert tooling("claude").cheapest_model(models) == "haiku"

    ClaudeTooling._scanned.clear()
    npm = ProgramPort({"/home/operator/.local/lib/node_modules/@anthropic-ai/claude-code/.": "claude-sonnet-5-5\n"}, version="2.1.290 (Claude Code)")
    assert await ClaudeTooling().models(npm) == ("fable", "opus", "sonnet", "haiku", "claude-sonnet-5-5")  # type: ignore[arg-type]

    # A program that names no model, or no program at all: the aliases, which always work.
    ClaudeTooling._scanned.clear()
    assert await ClaudeTooling().models(ProgramPort({})) == ClaudeTooling.ALIASES  # type: ignore[arg-type]

    class Missing(ProgramPort):
        async def run(self, argv: list[str], **_: Any) -> ExecResult:
            raise ProgramNotFound("claude is not an executable on PATH")

    assert await ClaudeTooling().models(Missing({})) == ClaudeTooling.ALIASES  # type: ignore[arg-type]
