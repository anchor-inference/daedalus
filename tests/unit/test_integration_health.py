"""An auth probe cannot infer provider permissions or leak credentials."""

from __future__ import annotations

import httpx

from daedalus.extensions.integration_health import capability_matrix, github_auth_probe


async def test_probe_distinguishes_authenticated_expired_and_unknown() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user"
        if request.headers["Authorization"] == "Bearer valid":
            return httpx.Response(200, json={"login": "someone"})
        return httpx.Response(401, json={"message": "Bad credentials"})

    transport = httpx.MockTransport(respond)
    good = await github_auth_probe("valid", transport=transport)
    assert good["state"] == "authenticated"
    assert "someone" not in str(good) and "valid" not in str(good)
    expired = await github_auth_probe("expired", transport=transport)
    assert expired["state"] == "needs_reconnect"
    absent = await github_auth_probe("", transport=transport)
    assert absent["reason"] == "no_token"
    unknown = await github_auth_probe("valid", transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    assert unknown["state"] == "unknown"


def test_matrix_does_not_infer_other_provider_parity() -> None:
    matrix = capability_matrix(github_configured=True, webhooks={"github"})
    assert matrix[0]["capabilities"]["pull_request_write"]
    assert not matrix[1]["capabilities"]["pull_request_write"]
    assert not matrix[2]["capabilities"]["issue_sync"]
