"""Promoted project facts reach the project's chats, not only the launch packets of its workers."""

from __future__ import annotations

from pathlib import Path

from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.knowledge import KnowledgeStore
from tests.support.models import model_config
from tests.unit.test_orchestrator import rig


async def _promote(db: Database, project_id: str, claim: str) -> None:
    await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,created_at)"
                     " VALUES ('123456789abc','proof.txt','text/plain',1,'digest-one','operator','now')")
    await db.execute("INSERT INTO file_access(file_id,scope,added_at) VALUES ('123456789abc',?,'now')", (project_id,))
    store = KnowledgeStore(db)
    candidate = await store.candidate(project_id, claim, source_kind="file", source_id="123456789abc", actor="extractor")
    await store.review(candidate["fact_id"], project_id, expected_version=1, verdict="promote",
                       actor="operator", reason="checked")


async def _opened(manager: SessionManager, session_id: str) -> str:
    state = await manager.get_state(session_id)
    assert state is not None
    message = await manager._with_turn_context(state, Message(role=MessageRole.user, content_blocks=[TextBlock(text="hello")]))
    return "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))


async def test_a_project_chat_hears_promoted_facts_and_a_worker_chat_does_not(settings: Settings, db: Database) -> None:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    try:
        project = await manager.projects.create("Bakery", [])
        await db.execute("INSERT INTO files(id,name,mime,size,sha256,origin,created_at)"
                         " VALUES ('123456789abc','proof.txt','text/plain',1,'digest-one','operator','now')")
        await db.execute("INSERT INTO file_access(file_id,scope,added_at) VALUES ('123456789abc',?,'now')", (project.id,))
        store = KnowledgeStore(db)
        candidate = await store.candidate(project.id, "The oven runs at two hundred degrees", source_kind="file",
                                          source_id="123456789abc", actor="extractor")
        await store.candidate(project.id, "An unreviewed rumour about the flour", source_kind="file",
                              source_id="123456789abc", actor="extractor")
        chat = await manager.create_session("chat")
        await manager.attach_project(chat.session.id, project)
        assert "Project facts" not in await _opened(manager, chat.session.id)
        await store.review(candidate["fact_id"], project.id, expected_version=1, verdict="promote",
                           actor="operator", reason="checked")
        said = await _opened(manager, chat.session.id)
        assert "Project facts" in said and "The oven runs at two hundred degrees" in said
        assert "rumour" not in said
        worker = await manager.create_session("worker", metadata={"staff_id": "member"})
        await manager.attach_project(worker.session.id, project)
        assert "Project facts" not in await _opened(manager, worker.session.id)
    finally:
        await manager.close()


async def test_the_coordinator_hears_promoted_facts_beside_its_project_state(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    run = await rig(settings, db, tmp_path)
    try:
        session_id = (await run.orch.enable(run.project.id)).settings.orchestrator.session_id
        await _promote(db, run.project.id, "Deploys happen only on Fridays after lunch")
        said = await _opened(run.manager, session_id)
        assert "Project facts" in said and "Deploys happen only on Fridays after lunch" in said
        assert "Project:" in said  # the coordinator's own state block is still there
    finally:
        await run.manager.close()
