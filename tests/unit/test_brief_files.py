"""A later brief of the same task names only the files new to the member.

A follow-up assigned to the same task, with nothing attached, arrived listing twenty screenshots from
earlier rounds as "files handed to you", because a task carries every file ever attached to it.
"""
from __future__ import annotations

from pathlib import Path

from daedalus.config import Settings
from daedalus.host import prompts
from daedalus.host.handoff import Delivered
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import rig


async def test_a_file_handed_before_is_counted_not_named_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        old = await r.manager.files.add(b"round one", name="shot-1.png", origin="operator", scope=r.project.id, actor="operator")
        new = await r.manager.files.add(b"round two", name="shot-2.png", origin="operator", scope=r.project.id, actor="operator")
        inbox = "/work/.agents/inbox/t1"
        await r.manager.files.record(old, "delivered", actor="orchestrator", target=f"{inbox}/shot-1.png", detail="Ira")
        # The second brief delivers both again: the old one for the second time, the new one first.
        await r.manager.files.record(old, "delivered", actor="orchestrator", target=f"{inbox}/shot-1.png", detail="Ira")
        await r.manager.files.record(new, "delivered", actor="orchestrator", target=f"{inbox}/shot-2.png", detail="Ira")
        fresh, earlier = await r.team.brief_files([Delivered(old, f"{inbox}/shot-1.png"), Delivered(new, f"{inbox}/shot-2.png")])
        assert [d.file.name for d in fresh] == ["shot-2.png"]
        assert [d.file.name for d in earlier] == ["shot-1.png"]
        note = prompts.STAFF_EARLIER_FILES.format(n=len(earlier), where=inbox)
        assert "1 file(s) handed to you earlier" in note and inbox in note
    finally:
        await r.manager.close()
