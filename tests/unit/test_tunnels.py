"""The agent does not raise a public tunnel, and the operator is shown the ones that run anyway.

Two directions, and the second is the one that matters to the agent's work: everything below the
refusals is a command that names a tunnel program, an SSH host or the word itself and raises nothing,
and it must pass. A rule that refused ``grep ngrok`` would be one more thing the agent has to guess
about, so each false refusal here is a defect, not a cautious default.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from daedalus.host import tunnels
from daedalus.host.engine_factory import refusal_text
from daedalus.host.policy import ALLOW, DENY, Policy, Rule, shell_segments

RULE = "egress.tunnel"

DIRECT = [
    "ngrok http 3000",
    "ngrok tcp 22",
    "/usr/local/bin/ngrok http 8080",
    "ngrok.exe http 80",
    "sudo ngrok http 80",
    "env NGROK_AUTHTOKEN=x timeout 3600 ngrok http 80",
    "cloudflared tunnel --url http://localhost:3000",
    "cloudflared --url http://localhost:3000",
    "cloudflared tunnel run mytunnel",
    "cloudflared tunnel --hello-world",
    "tailscale funnel 3000",
    "tailscale funnel --bg 8080",
    "tailscale serve --bg --funnel 3000",
    "frpc -c frpc.toml",
    "zrok share public localhost:8080",
    "bore local 8000 --to bore.pub",
    "chisel client host:8080 R:3000:localhost:3000",
    "lt --port 3000",
    "tuna http 3000",
    "devtunnel host -p 3000",
    "code tunnel --accept-server-license-terms",
    "expo start --tunnel",
    "netlify dev --live",
    "wstunnel client -R tcp://[::]:8080:localhost:8080 wss://relay.example.test",
    "tor --HiddenServiceDir /tmp/h --HiddenServicePort '80 127.0.0.1:3000'",
]
"""The programs the operator named and the ones like them, called plainly."""

BACKGROUND_AND_SHAPES = [
    "nohup ngrok http 80 > /dev/null 2>&1 &",
    "ngrok http 80 &",
    "setsid ngrok http 80",
    "ngrok http 80 | tee tunnel.log",
    "cd /tmp && ngrok http 80",
    "T=ngrok; $T http 80",
    "\\ngrok http 80",
    "n''grok http 80",
    "bash -c 'ngrok http 80'",
    "sh -lc \"cd x && ngrok http 80\"",
    "eval 'ngrok http 80'",
    "xargs ngrok http < ports",
    "nohup -- ngrok http 80",
    "exec -a other ngrok http 80",
    "stdbuf -oL ngrok http 80",
    "timeout -s KILL 10 ngrok http 80",
    "timeout --foreground 5 ngrok http 80",
    "nice -n 5 ngrok http 80",
]
"""Backgrounding, pipelines, quoting, a variable the line sets itself and a shell opened on the way."""

LAUNCHED = [
    "npx ngrok http 80",
    "npx -y localtunnel --port 3000",
    "npx --yes cloudflared tunnel --url http://localhost:3000",
    "npx expo start --tunnel",
    "pnpm dlx ngrok http 80",
    "bunx localtunnel --port 80",
    "npm exec -- ngrok http 80",
    "uvx pyngrok http 80",
    "uvx --from pyngrok ngrok http 80",
    "uv run ngrok http 80",
    "python -m pyngrok http 80",
    "python3 -m pyngrok http 80",
    "python -c 'from pyngrok import ngrok; ngrok.connect(8000)'",
    "node -e \"require('ngrok').connect(80)\"",
    "node /usr/lib/node_modules/localtunnel/bin/lt.js --port 3000",
    "docker run --rm -it ngrok/ngrok http host.docker.internal:80",
    "docker run -d --name t cloudflare/cloudflared:latest tunnel --no-autoupdate run --token abc",
    "podman run -e NGROK_AUTHTOKEN=x ngrok/ngrok:latest http 80",
    "docker run --entrypoint ngrok wernight/ngrok http 80",
    "docker run alpine sh -c 'ngrok http 80'",
    "sh <<< 'ngrok http 80'",
    "python3 - <<EOF\nfrom pyngrok import ngrok\nngrok.connect(80)\nEOF",
    "kubectl exec pod -- ngrok http 80",
    "tmux new -d 'ngrok http 80'",
    "tmux send-keys 'ngrok http 80' Enter",
    "screen -dmS t ngrok http 80",
    "watch -n 5 'ngrok http 80'",
    "systemd-run --user ngrok http 80",
]
"""A package runner, a module, a container, a terminal multiplexer: the commands an agent reaches for next."""

SSH = [
    "ssh -R 80:localhost:3000 localhost.run",
    "ssh -R 80:localhost:3000 nokey@localhost.run",
    "ssh -p 443 -R0:localhost:8000 a.pinggy.io",
    "ssh -R 80:localhost:3000 serveo.net",
    "ssh -t -R 443:localhost:3000 tunnl.gg",
    "ssh localhost.run",
    "ssh -NR 8080:localhost:80 me@box",
    "ssh -fNR8080:localhost:80 box",
    "ssh -o ExitOnForwardFailure=yes -R 0.0.0.0:80:localhost:80 box",
    "ssh -oRemoteForward='80 localhost:80' box",
    "ssh -o 'RemoteForward 80 localhost:80' box",
    "autossh -M 0 -N -R 80:localhost:3000 box",
    "sshpass -p x ssh -R 80:localhost:80 box",
    "ssh box 'ngrok http 80'",
    "ssh box cloudflared tunnel run x",
]
"""A reverse forward in each spelling, the services that answer one with an address, and a tunnel raised
on the far side."""

ALLOWED = [
    # The word, in a file name, a search, a document, a message.
    "grep -r ngrok .",
    "cat ngrok.yml",
    "ls ~/.config/ngrok",
    "echo 'run ngrok http 3000 to expose it'",
    "git commit -m 'document cloudflared tunnel and ngrok http usage'",
    "cat <<EOF > docs.md\nngrok http 3000\ncloudflared tunnel --url x\nEOF",
    "sed -i s/ngrok/something/ README.md",
    "tail -f ngrok.log",
    "ps aux | grep ngrok",
    "man ngrok",
    "which ngrok cloudflared",
    "command -v cloudflared",
    "pkill ngrok",
    "kill 1234",
    "docker logs ngrok",
    "docker run --name ngrok-test nginx",
    "docker run -e MODE=tcp --rm alpine sh -c 'echo tcp'",
    "tmux new -s ngrok",
    "echo ssh -R 80:localhost:80 box",
    "curl -s https://ngrok.com/docs | head",
    "curl https://localhost.run/docs",
    # Installing, configuring, asking: the tool known, nothing raised.
    "pip install pyngrok",
    "python -m pip install pyngrok",
    "npm install -g ngrok localtunnel",
    "pnpm add ngrok",
    "uv add pyngrok",
    "brew install cloudflared",
    "apt-get install -y cloudflared",
    "python -c 'import pyngrok; print(pyngrok.__version__)'",
    "python3 - <<EOF\nimport pyngrok\nprint(pyngrok.__version__)\nEOF",
    "cat > t.py <<EOF\nfrom pyngrok import ngrok\nngrok.connect(80)\nEOF",
    "ngrok",
    "ngrok --version",
    "ngrok --help",
    "ngrok http --help",
    "ngrok version",
    "ngrok config add-authtoken abc",
    "cloudflared --version",
    "cloudflared tunnel --help",
    "cloudflared tunnel list",
    "cloudflared tunnel info mytunnel",
    "cloudflared tunnel create x",
    "cloudflared tunnel route dns x a.example.test",
    "cloudflared access ssh --hostname ssh.example.test",
    "tailscale up",
    "tailscale status",
    "tailscale ip -4",
    "tailscale ping box",
    "tailscale serve 3000",
    "tailscale funnel status",
    "tailscale funnel reset",
    "tailscale funnel --help",
    "frpc --version",
    "frpc verify -c frpc.toml",
    "frpc status -c frpc.toml",
    "zrok enable abc",
    "zrok access private tok",
    "zrok share --help",
    "code .",
    "code tunnel status",
    "expo start",
    "netlify dev",
    "netlify deploy",
    "tor --version",
    "lt --help",
    "bore server",
    "chisel server --reverse",
    "tuna --show_threads",
    # Ordinary SSH, and the forwards that open nothing to anyone.
    "ssh box",
    "ssh me@box uptime",
    "ssh -p 2222 -i ~/.ssh/key me@192.0.2.10 'ls -R /var/log'",
    "ssh box ls -R",
    "ssh -o StrictHostKeyChecking=no box 'rsync -R a b'",
    "ssh -L 8080:localhost:80 box",
    "ssh -D 1080 box",
    "ssh -J jump box",
    "ssh -R /run/user/1000/gnupg/S.gpg-agent:/run/user/1000/gnupg/S.gpg-agent.extra box",
    "ssh-keygen -R localhost.run",
    "ssh-keyscan localhost.run",
    "ssh-copy-id box",
    "scp file box:/tmp/",
    "rsync -avR src/ box:/dst",
    "git clone git@github.com:o/r.git",
    # Other commands with the same flags.
    "ls -R",
    "rm -R build",
    "cp -R a b",
    "grep -R foo .",
    "docker run --rm alpine echo hi",
    "docker ps",
    "npx prettier --check .",
    "npx vitest run",
    "npm run dev -- --port 3000",
    "uv run pytest tests/unit/test_tunnels.py -q",
    "uvx ruff check",
    "watch -n 5 ls",
    "nohup python app.py > log 2>&1 &",
    "python -m http.server 8000",
    "python manage.py runserver",
    "node server.js",
    "node -e 'console.log(1)'",
]


def _policy() -> Policy:
    return Policy()


def _says_no(command: str, tool: str = "Exec") -> bool:
    decision = _policy().evaluate(tool, {"command": command})
    return decision.action == DENY and decision.rule == RULE


@pytest.mark.parametrize("command", [*DIRECT, *BACKGROUND_AND_SHAPES, *LAUNCHED, *SSH])
def test_a_command_that_raises_a_tunnel_is_refused(command: str) -> None:
    assert _says_no(command), command


@pytest.mark.parametrize("command", ALLOWED)
def test_a_command_that_only_mentions_one_is_not(command: str) -> None:
    decision = _policy().evaluate("Exec", {"command": command})
    assert decision.rule != RULE, (command, decision.reason)


@pytest.mark.parametrize("tool", ["Verify", "ServiceStart"])
def test_the_other_tools_that_start_a_command_are_read_the_same_way(tool: str) -> None:
    assert _says_no("ngrok http 3000", tool)
    assert _says_no("cd app && nohup cloudflared tunnel --url http://localhost:3000 &", tool)
    assert not _says_no("python -m http.server 8000", tool)
    assert not _says_no("grep -r ngrok .", tool)


def test_stacked_wrappers_are_read_and_a_pathological_line_costs_little() -> None:
    """The rule runs on the event loop before every command, so a line built to be slow to read must not be."""
    assert _says_no("setsid setsid setsid nohup setsid ngrok http 80")
    assert _says_no("docker run " + " ".join(f"-e A{i}=x" for i in range(20)) + " ngrok/ngrok http 80")
    for command in ("npx " * 1000, "stdbuf " + "-a " * 2000 + "x", "ssh a " * 100 + "ls", "tmux new " + "'tmux new " * 5 + "ls" + "'" * 5):
        started = time.perf_counter()
        assert not _says_no(command)
        assert time.perf_counter() - started < 1.0, command[:40]


def test_a_background_job_and_a_service_are_the_same_command() -> None:
    policy = _policy()
    for arguments in ({"command": "ngrok http 80", "background": True}, {"command": "ngrok http 80", "service": True}, {"command": "ngrok http 80", "cwd": "/tmp"}):
        decision = policy.evaluate("Exec", arguments)
        assert decision.action == DENY and decision.rule == RULE, arguments


def test_an_mcp_server_the_agent_adds_is_a_command_it_typed() -> None:
    """McpAdd starts a process as surely as Exec does, and would be the way round the rule."""
    policy = _policy()
    for arguments in (
        {"name": "t", "command": "cloudflared", "args": ["tunnel", "--url", "http://localhost:3000"]},
        {"name": "t", "command": "npx", "args": ["-y", "localtunnel", "--port", "80"]},
        {"name": "t", "command": "ngrok http 80"},
        {"name": "t", "command": "sh", "args": ["-c", "ngrok http 80"]},
        {"name": "t", "command": "ssh", "args": ["-R", "80:localhost:3000", "localhost.run"]},
        {"name": "t", "command": "docker", "args": ["run", "--rm", "ngrok/ngrok", "http", "80"]},
    ):
        decision = policy.evaluate("McpAdd", arguments)
        assert decision.action == DENY and decision.rule == RULE, arguments
    for arguments in (
        {"name": "files", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
        {"name": "git", "command": "uvx", "args": ["mcp-server-git", "--repository", "/tmp/r"]},
        {"name": "remote", "transport": "http", "url": "https://mcp.example.test/sse"},
        {"name": "docs", "command": "python", "args": ["-m", "docs_server"]},
    ):
        assert policy.evaluate("McpAdd", arguments).action == ALLOW, arguments


def test_the_refusal_says_whose_rule_it_is_and_what_to_do() -> None:
    """The generic refusal ends "do the task another way", which is what an agent that was refused a
    tunnel would take as leave to try a script. This one names the owner and the way to get it."""
    text = refusal_text(_policy().evaluate("Exec", {"command": "ngrok http 3000"}))
    assert text.startswith("Refused: ngrok would raise a public tunnel")
    assert "operator's rule (egress.tunnel)" in text and "operator opens tunnels from settings" in text
    assert "AskUser" in text and "Do not try another way" in text
    assert "do the task another way" not in text
    assert "ssh -R would raise" in refusal_text(_policy().evaluate("Exec", {"command": "ssh -R 80:localhost:80 box"}))


def test_neither_an_operator_rule_nor_a_standing_grant_lifts_it() -> None:
    lifting = Rule("let-it", "Exec", ALLOW, "the operator's own", pattern="ngrok")
    policy = Policy(rules=[lifting])
    assert policy.evaluate("Exec", {"command": "ngrok http 80"}).action == DENY
    similar = [{"kind": "prefix", "value": "ngrok http", "rule": RULE, "tool": "Exec", "hosts": [], "label": "ngrok http"}]
    assert policy.evaluate("Exec", {"command": "ngrok http 80"}, similar=similar).action == DENY


def test_the_rule_is_listed_among_the_built_in_ones() -> None:
    row = next(r for r in _policy().describe() if r["id"] == RULE)
    assert row["action"] == DENY and "McpAdd" in row["tool"] and "settings" in row["note"]


def test_a_denial_elsewhere_in_the_line_is_not_hidden_by_a_tunnel_one() -> None:
    """The strictest answer is the one given, and a tunnel is a denial like any: same rank, first found."""
    decision = _policy().evaluate("Exec", {"command": "ngrok http 80; rm -rf /"})
    assert decision.action == DENY


# -- what is running -------------------------------------------------------------------------


def _process(root: Path, pid: int, argv: list[str], *, started_ticks: int = 0) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(w.encode() for w in argv) + (b"\0" if argv else b""))
    # pid (name) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime cutime cstime priority nice threads itrealvalue starttime
    (entry / "stat").write_text(f"{pid} (a b) c) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {started_ticks} 0 0\n", encoding="utf-8")


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("1000.00 900.00\n", encoding="ascii")
    (root / "self").mkdir()
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    return root


def test_a_running_tunnel_is_found_by_the_reading_that_refuses_one(proc: Path) -> None:
    _process(proc, 101, ["ngrok", "http", "3000"], started_ticks=70_000)  # 700 s into a 1000 s uptime: 300 s old
    _process(proc, 102, ["ssh", "-N", "-R", "80:localhost:3000", "localhost.run"], started_ticks=99_000)
    _process(proc, 103, ["bash", "-lc", "sleep 5; cloudflared tunnel --url http://localhost:3000"])
    _process(proc, 104, ["ssh", "box", "ls", "-R"])
    _process(proc, 105, ["python", "-m", "http.server"])
    _process(proc, 106, ["grep", "-r", "ngrok", "."])
    _process(proc, 107, [])  # a kernel thread has no command line
    (proc / "108").mkdir()  # gone between the listing and the read
    (proc / "sys").mkdir()
    found = tunnels.scan_proc(shell_segments, root=str(proc))
    assert [(r.pid, r.kind) for r in found] == [(101, "ngrok"), (102, "ssh -R"), (103, "cloudflared")]
    assert found[0].age_seconds == pytest.approx(300.0) and found[1].age_seconds == pytest.approx(10.0)
    assert found[0].line == "ngrok · pid 101 · running 5 min · ngrok http 3000"


def test_only_this_users_processes_are_looked_at(proc: Path) -> None:
    _process(proc, 101, ["ngrok", "http", "3000"])
    assert tunnels.scan_proc(shell_segments, root=str(proc), uid=os.getuid() + 1) == []
    assert [r.pid for r in tunnels.scan_proc(shell_segments, root=str(proc), uid=os.getuid())] == [101]


def test_a_token_in_a_command_line_is_not_put_in_front_of_anyone(proc: Path) -> None:
    secret = "eyJhIjoiMDAwMDAwMDAwMDAwMDAwMDAwMDAiLCJ0IjoiMTExMSJ9"
    _process(proc, 101, ["cloudflared", "tunnel", "run", "--token", secret])
    _process(proc, 102, ["ngrok", "http", "--authtoken=2abcdefghijklmnopqrstuvwxyz0123456789", "3000"])
    _process(proc, 103, ["cloudflared", "service", "install", secret])
    lines = [r.line for r in tunnels.scan_proc(shell_segments, root=str(proc))]
    assert len(lines) == 3 and not any(secret in line or "2abcdefgh" in line for line in lines)
    assert "--token …" in lines[0] and "--authtoken=…" in lines[1]


def test_ps_output_is_read_the_same_way() -> None:
    text = (
        "  412 02:03:04 /usr/local/bin/ngrok http 3000\n"
        "  413    05:10 ssh -fN -R 80:localhost:3000 serveo.net\n"
        "  414 1-00:00:01 /usr/bin/ssh box ls -R\n"
        "  415       00:09 vim ngrok.yml\n"
        "garbage\n"
    )
    found = tunnels.scan_ps(text, shell_segments)
    assert [(r.pid, r.kind, r.age_seconds) for r in found] == [(412, "ngrok", 7384.0), (413, "ssh -R", 310.0)]


def test_elapsed_time_in_the_form_ps_prints_it() -> None:
    assert tunnels.parse_etime("00:09") == 9 and tunnels.parse_etime("02:03:04") == 7384 and tunnels.parse_etime("1-00:00:01") == 86401
    assert tunnels.parse_etime("soon") is None


def test_running_answers_for_this_machine() -> None:
    found = tunnels.running()
    assert found is None or all(isinstance(r, tunnels.Running) for r in found)
