"""The agent does not step into this installation's own containers from outside them."""

from __future__ import annotations

import pytest

from daedalus.host import own_containers
from daedalus.host.policy import shell_segments


def entry(line: str) -> str | None:
    for words in shell_segments(line):
        if (found := own_containers.entry_in(words, stack="deploy", ident="3f2a9c1b7d4e")) is not None:
            return found
    return None


@pytest.mark.parametrize("line", [
    "sudo -n nsenter -t 295749 -m -u -i -n -p sh -c 'ls /srv/state'",
    "nsenter --target=4242 --mount cat /etc/hostname",
    "docker exec deploy-daedalus-1 ls /srv/state",
    "docker exec -it deploy-keyproxy-1 sh",
    "sudo docker cp key.pem deploy-daedalus-1:/root/.android/adbkey",
    "docker cp deploy_daedalus_1:/srv/state/daedalus.sqlite ./copy.sqlite",
    "docker container exec deploy-daedalus-1 env",
    "docker stop deploy-daedalus-1",
    "docker run --rm --pid=container:deploy-daedalus-1 alpine ps",
    "docker run --rm --volumes-from deploy-daedalus-1 alpine ls /srv/state",
    "podman exec deploy-daedalus-1 sh",
    "docker exec 3f2a9c1b7d4e ls",
    "docker compose -p deploy exec daedalus sh",
    "bash -c 'docker exec deploy-daedalus-1 cat /srv/state/.env'",
    "cd /tmp && docker exec deploy-daedalus-1 id",
])
def test_entering_our_containers_is_refused(line: str) -> None:
    assert entry(line) is not None


@pytest.mark.parametrize("line", [
    "docker ps",
    "docker ps --format '{{.Names}}'",
    "docker logs deploy-daedalus-1 --tail 50",
    "docker inspect deploy-daedalus-1 -f '{{.State.Status}}'",
    "docker exec dresselhaus-v3-web-1 ls /app",
    "docker exec -it too-pult sh",
    "docker cp ./dist stalwart:/opt/www",
    "docker run --rm -v $PWD:/src -w /src golang:1.23 go test ./...",
    "docker compose up -d",
    "docker compose -p shop exec web sh",
    "docker build -t site:local .",
    "docker stop test-redis",
    "grep -rn nsenter docs/",
    "echo 'docker exec deploy-daedalus-1' >> notes.md",
    "nsenter --help",
    "man nsenter",
])
def test_ordinary_docker_and_mentions_pass(line: str) -> None:
    assert entry(line) is None


def test_the_refusal_names_the_rule_and_the_way_out() -> None:
    text = own_containers.REFUSAL.format(what="docker exec deploy-daedalus-1")
    assert "host.own_containers" in text and "AskUser" in text


def test_the_policy_refuses_it_for_every_tool_that_runs_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.host.policy import ALLOW, DENY, Policy, Rule

    monkeypatch.setenv("DAEDALUS_COMPOSE_PROJECT", "deploy")
    for tool in ("Exec", "Verify", "ServiceStart"):
        decision = Policy().evaluate(tool, {"command": "sudo docker exec deploy-daedalus-1 cat /srv/state/.env"})
        assert decision.action == DENY and decision.rule == "host.own_containers", tool
    lifting = Policy(rules=[Rule("let-it", "Exec", ALLOW, "the operator's own", pattern="docker")])
    assert lifting.evaluate("Exec", {"command": "docker exec deploy-daedalus-1 id"}).action == DENY
    assert Policy().evaluate("Exec", {"command": "docker logs deploy-daedalus-1 --tail 5"}).rule != "host.own_containers"


def test_a_project_named_otherwise_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_COMPOSE_PROJECT", "home")
    assert own_containers.is_ours("home-daedalus-1", ident="")
    assert not own_containers.is_ours("deploy-daedalus-1", ident="")
