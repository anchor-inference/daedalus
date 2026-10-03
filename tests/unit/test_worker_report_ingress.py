"""Worker transport retries preserve authenticated report identity and current authority."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from daedalus.config import Settings
from daedalus.extensions.api import build_app
from daedalus.stores.control import ControlStore, Principal
from daedalus.stores.database import Database
from tests.support.waiting import until
from tests.unit.test_cli_staff_runtime import StubClaude, stand, trust


async def test_bridge_replay_checks_payload_and_revoked_authority(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude(script="slow:10000")) as s:
        trust(s)
        member = await s.hire()
        await s.assign(member, await s.task())
        await s.status_event(member, "working")
        live = await s.session_row(member)
        session = s.runtime.sessions[live.id]

        async def report(note="First checkpoint"):
            await s.runtime._team(session, SimpleNamespace(
                body={"tool": "report", "call_id": "one-call", "kind": "checkpoint", "note": note},
                at=datetime.now(UTC).isoformat(), reply_id=None,
            ))

        await report()
        assert session.calls["one-call"][1].get("error") is not True
        await report("Changed checkpoint under the same call")
        assert session.calls["one-call"][1]["error"] is True
        assert (await db.fetchone("SELECT count(*) FROM staff_report_records"))[0] == 1
        await report()
        assert session.calls["one-call"][1].get("error") is not True
        grant = await db.fetchone("SELECT grant_id FROM execution_attempts WHERE staff_session_id = ?", (live.id,))
        await ControlStore(db).revoke_grant(Principal.operator({"via": "token", "user_id": 1}), grant["grant_id"],
                                          reason="Withdraw worker permission")
        await report()
        assert session.calls["one-call"][1]["error"] is True
        assert (await db.fetchone("SELECT count(*) FROM staff_report_records"))[0] == 1


async def test_http_worker_report_uses_launch_token_and_stable_operation_identity(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude(script="slow:10000")) as s:
        trust(s)
        requests = []
        start = s.runtime.start

        async def capture(request):
            requests.append(request)
            return await start(request)

        s.runtime.start = capture
        member = await s.hire()
        await s.assign(member, await s.task())
        await until(lambda: bool(requests), "the admitted runtime has a launch token")
        await s.status_event(member, "working")
        request = requests[0]
        app = s.team.app
        app.config, app.front, app.guard = s.manager.config, None, None
        api = build_app(app, "tok")
        path = f"/api/team/{request.staff_session_id}/report"
        body = {"kind": "checkpoint", "note": "Original checkpoint", "client_operation_id": "h" * 160}
        headers = {"X-Daedalus-Team-Token": request.team_token}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            assert (await client.post(path, json=body, headers={"X-Daedalus-Token": "tok"})).status_code == 401
            assert (await client.post(path, json={"kind": "checkpoint", "note": "missing identity"}, headers=headers)).status_code == 422
            first = await client.post(path, json=body, headers=headers)
            assert first.status_code == 200, first.text
            assert (await client.post(path, json=body, headers=headers)).json() == first.json()
            assert (await client.post(path, json={**body, "note": "Changed"}, headers=headers)).status_code == 409
            assert (await db.fetchone("SELECT count(*) FROM staff_report_records"))[0] == 1
            grant = await db.fetchone("SELECT grant_id FROM execution_attempts WHERE staff_session_id = ?", (request.staff_session_id,))
            await ControlStore(db).revoke_grant(Principal.operator({"via": "token", "user_id": 1}), grant["grant_id"],
                                              reason="Withdraw worker permission")
            assert (await client.post(path, json=body, headers=headers)).status_code == 403
