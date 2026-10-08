"""The operator's secrets and a project's staff: a coding CLI's launch carries the project's secrets as variables
and owner-only files, and a coordinator hands one of its chat's secrets to a member by name, never by value."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.stores.database import Database
from daedalus.terminals.model import TerminalSpec
from tests.unit.test_cli_staff_runtime import stand, trust
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import fake, office, working
from tests.unit.test_staff_runtime import close_team

VALUE = "Tr0ub4dor-and-3"
PLACEHOLDER = "«secret:router_admin»"


@pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")
async def test_a_coding_cli_starts_with_the_projects_secret_in_its_environment_and_launch(settings: Settings, db: Database) -> None:
    async with stand(settings, db) as s:
        trust(s)
        await s.manager.secrets.put("project", s.project.id, "router_admin", VALUE, "ISP router web admin")
        created: list[TerminalSpec] = []
        create = s.terminals.create

        async def watched(spec: TerminalSpec, **kwargs: Any) -> Any:
            created.append(spec)
            return await create(spec, **kwargs)

        s.terminals.create = watched  # type: ignore[method-assign]
        ada = await s.hire()
        await s.assign(ada, await s.task())
        await s.status_event(ada, "working")
        main = next(spec for spec in created if spec.owner.kind == "staff")
        assert main.env_vars["DAEDALUS_SECRET_ROUTER_ADMIN"] == VALUE
        row = await s.session_row(ada)
        launch_dir = Path((await s.runtime.store.open_launch_for(row.id)).launch_dir)  # type: ignore[union-attr]
        copy = Path(main.env_vars["DAEDALUS_SECRET_ROUTER_ADMIN_FILE"])
        assert copy == launch_dir / "secret-router_admin"
        assert copy.read_text() == VALUE and stat.S_IMODE(copy.stat().st_mode) == 0o600
        # The audit names the file and keeps no hash of it; the brief names the secret and holds no value.
        audits = [json.loads(r["detail_json"]) for r in await db.fetchall("SELECT detail_json FROM terminal_audit WHERE action = 'launch'")]
        assert audits and audits[-1]["files"]["secret-router_admin"] == "operator secret"
        first = (await s.manager.staff.messages(ada.id))[0].text
        assert PLACEHOLDER in first and "$DAEDALUS_SECRET_ROUTER_ADMIN" in first and VALUE not in first
        await s.manager.secrets.settle()
        assert s.manager.secrets.listed()[0].last_used_by.startswith("staff Ada")
        # One handed over while it works arrives as a file of the launch: its environment is already fixed.
        chat = await s.manager.create_session("coordinator", project_id=s.project.id)
        await s.manager.get_state(chat.session.id)
        await s.manager.secrets.put("session", chat.session.id, "wifi", "wifi-password-9")
        handed, _ = await s.manager.secrets.grant(["wifi"], from_session=chat.session.id, staff_id=ada.id, project_id=s.project.id)
        note = await s.team.secrets_note(ada, handed)
        assert (launch_dir / "secret-wifi").read_text() == "wifi-password-9"
        assert "$DAEDALUS_LAUNCH_DIR/secret-wifi" in note and "wifi-password-9" not in note
        # Handed again with a corrected value: the running launch's file takes the new one, and both values
        # stay masked in whatever a command prints afterwards.
        await s.manager.secrets.put("session", chat.session.id, "wifi", "wifi-password-10")
        handed, _ = await s.manager.secrets.grant(["wifi"], from_session=chat.session.id, staff_id=ada.id, project_id=s.project.id)
        note = await s.team.secrets_note(ada, handed)
        assert (launch_dir / "secret-wifi").read_text() == "wifi-password-10"
        assert "not delivered" not in note and "wifi-password-10" not in note
        masked = s.manager.secrets.redactor.redact("old wifi-password-9 new wifi-password-10")
        assert "wifi-password" not in masked and masked.count("«secret:wifi»") == 2
        assert await s.team.release(ada)


async def test_a_coordinator_hands_its_chats_secret_to_a_member_by_name(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await r.manager.secrets.put("session", sid, "router_admin", VALUE, "ISP router web admin")
        member, live = await working(r)
        # Not handed yet: a member sees the project's secrets, not its coordinator's chat's.
        assert r.manager.secrets.for_staff(member.id, r.project.id) == []
        staff_session = await r.manager.db.fetchone("SELECT id FROM sessions WHERE json_extract(metadata, '$.staff_id') = ?", (member.id,))
        assert staff_session is not None
        await r.manager.get_state(staff_session["id"])
        assert r.manager.secrets.available(staff_session["id"]) == []
        with pytest.raises(Refused, match="no secret called wifi"):
            await r.call(sid, "tell", staff="Ada", text="Use the wifi password", secrets=["wifi"])
        cards = await r.manager.db.fetchone("SELECT count(*) AS n FROM board_tasks")
        with pytest.raises(Refused, match="no secret called wifi"):
            await r.call(sid, "assign", staff="Ada", title="Router check", objective="Check the router's firmware version",
                         deliverable="A note with the version", boundaries="Read only, change nothing", done_when="The note names the version",
                         expected_collection_revision=1, secrets=["wifi"])
        # Refused before anything was written: no half-made card is left behind.
        assert (await r.manager.db.fetchone("SELECT count(*) AS n FROM board_tasks"))["n"] == cards["n"]
        said = await r.call(sid, "tell", staff="Ada", text="Log into the router and read the firmware version", secrets=["router_admin"])
        assert "handing over «secret:router_admin»" in said and "do not name a variable" in said
        assert [s.name for s in r.manager.secrets.for_staff(member.id, r.project.id)] == ["router_admin"]
        # A Daedalus member is a session: its commands have the variable from now on.
        assert [s.name for s in r.manager.secrets.available(staff_session["id"])] == ["router_admin"]
        told = runtime.sent[-1][1].text
        assert PLACEHOLDER in told and VALUE not in told and "Never try to print" in told
        # Taking the secret back takes it from the member too.
        await r.manager.secrets.delete(r.manager.secrets.listed()[0].id)
        assert r.manager.secrets.for_staff(member.id, r.project.id) == []
        assert r.manager.secrets.available(staff_session["id"]) == []
        assert live is not None
    finally:
        await close_team(r.manager)
        await r.manager.close()
