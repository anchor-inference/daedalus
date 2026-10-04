"""A launcher stops its host only after the exact durable update decision."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from launcher.update_drain import DrainRefused, UpdateDrainClient, candidate_digest, checkpoint_refs_digest

BOT = 'a' * 40
CORE = 'b' * 40


class Host(UpdateDrainClient):
    def __init__(self, *, state: str = 'ready', lost: str = '', generation_drift: bool = False,
                 mismatch: bool = False) -> None:
        super().__init__(Path('.'), wait_seconds=0.01)
        self.state_name = state
        self.lost = lost
        self.generation_drift = generation_drift
        self.mismatch = mismatch
        self.drain: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.revision = 1

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(f'{method} {path}')
        if method == 'GET':
            return {'host_generation': 2 if self.generation_drift and self.drain else 1,
                    'collection_revision': self.revision, 'admission_open': self.drain is None,
                    'drain': self.drain}
        assert body is not None
        if path == '/api/runtime/updates/drain':
            self.drain = {'id': 'drain-1', 'receipt_id': 'receipt-1', 'host_generation': 1,
                          'policy': body['policy'], 'candidate_bot_sha': body['candidate_bot_sha'],
                          'candidate_core_sha': body['candidate_core_sha'],
                          'candidate_compatibility_digest': body['candidate_compatibility_digest'],
                          'state': self.state_name, 'checkpoint_refs': []}
            if self.mismatch:
                self.drain['candidate_core_sha'] = 'c' * 40
            self.revision += 1
            if self.lost == 'begin':
                raise DrainRefused('response lost')
        elif path.endswith('/recheck'):
            assert self.drain is not None
            self.revision += 1
        else:
            assert path.endswith('/decision') and self.drain is not None
            if self.state_name != 'ready':
                raise DrainRefused('blocked')
            self.drain['state'] = 'committed'
            self.drain['commit_receipt_id'] = 'receipt-2'
            self.revision += 1
            if self.lost == 'commit':
                raise DrainRefused('response lost')
        return {'drain': self.drain, 'host_generation': 1, 'admission_open': False,
                'collection_revision': self.revision}


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', ['begin', 'commit'])
async def test_lost_response_is_resolved_by_exact_durable_receipt(lost: str) -> None:
    host = Host(lost=lost)
    receipt = await host.commit(BOT, CORE, 'checked')
    assert receipt['state'] == 'committed' and receipt['commit_receipt_id'] == 'receipt-2'
    with pytest.raises(DrainRefused, match='awaits successor recovery'):
        await host.commit(BOT, CORE, 'checked')
    assert host.calls.count('POST /api/runtime/updates/drain') == 1
    assert host.calls.count('POST /api/runtime/updates/drain/drain-1/decision') == 1


@pytest.mark.asyncio
async def test_a_changed_host_generation_or_candidate_never_commits() -> None:
    for host in (Host(generation_drift=True), Host(mismatch=True)):
        with pytest.raises(DrainRefused):
            await host.commit(BOT, CORE, 'checked')
        assert not any(call.endswith('/decision') for call in host.calls)


@pytest.mark.asyncio
async def test_blocked_physical_work_stays_closed_without_commit() -> None:
    host = Host(state='blocked')
    with pytest.raises(DrainRefused):
        await host.commit(BOT, CORE, 'checked')
    assert host.drain is not None and host.drain['state'] == 'blocked'
    assert not any(call.endswith('/decision') for call in host.calls)


@pytest.mark.asyncio
async def test_checkpointed_work_requires_candidate_side_compatibility_proof() -> None:
    host = Host()
    host.drain = {'id': 'drain-1', 'receipt_id': 'receipt-1', 'host_generation': 1,
                  'policy': 'stop_all', 'candidate_bot_sha': BOT, 'candidate_core_sha': CORE,
                  'candidate_compatibility_digest': candidate_digest(BOT, CORE, 'checked'),
                  'state': 'ready', 'checkpoint_refs': [{'run_id': 'one'}]}
    with pytest.raises(DrainRefused, match='checkpoint compatibility'):
        await host.commit(BOT, CORE, 'checked')
    assert not any(call.endswith('/decision') for call in host.calls)


@pytest.mark.asyncio
async def test_checkpoint_policy_requires_verified_refs_before_commit() -> None:
    host = Host()
    references = [{'run_id': 'parked', 'sha256': 'd' * 64}]
    host.drain = {'id': 'drain-1', 'receipt_id': 'receipt-1', 'host_generation': 1,
                  'policy': 'checkpoint_supported', 'candidate_bot_sha': BOT,
                  'candidate_core_sha': CORE,
                  'candidate_compatibility_digest': candidate_digest(BOT, CORE, 'checked'),
                  'state': 'ready', 'checkpoint_refs': references}
    checked: list[list[dict[str, Any]]] = []
    def verify(refs: list[dict[str, Any]]) -> str:
        checked.append(refs)
        return checkpoint_refs_digest(refs)
    receipt = await host.commit(BOT, CORE, 'checked', policy='checkpoint_supported', verify_checkpoints=verify)
    assert receipt['state'] == 'committed' and checked == [references]
    assert host.calls.count('POST /api/runtime/updates/drain/drain-1/decision') == 1


@pytest.mark.asyncio
async def test_successor_client_reconciles_only_the_installed_committed_pair() -> None:
    class Successor(Host):
        def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == 'POST' and path.endswith('/recover'):
                assert body is not None and self.drain is not None
                assert body['commit_receipt_id'] == self.drain['commit_receipt_id']
                assert body['installed_bot_sha'] == BOT and body['installed_core_sha'] == CORE
                self.calls.append(f'{method} {path}')
                self.drain['state'] = 'resumed'
                return {'admission_open': True, 'drain': self.drain}
            if method == 'GET':
                self.calls.append(f'{method} {path}')
                return {'host_generation': 2, 'collection_revision': self.revision,
                        'admission_open': self.drain is None or self.drain['state'] == 'resumed',
                        'drain': self.drain}
            return super()._request(method, path, body)

    host = Successor()
    host.drain = {'id': 'drain-1', 'receipt_id': 'receipt-1', 'commit_receipt_id': 'receipt-2',
                  'host_generation': 1, 'policy': 'stop_all', 'candidate_bot_sha': BOT,
                  'candidate_core_sha': CORE, 'state': 'committed', 'checkpoint_refs': []}
    with pytest.raises(DrainRefused, match='installed successor'):
        await host.recover(BOT, 'c' * 40)
    assert not any(call.endswith('/recover') for call in host.calls)
    recovered = await host.recover(BOT, CORE)
    assert recovered is not None and recovered['state'] == 'resumed'
    assert host.calls.count('POST /api/runtime/updates/drain/drain-1/recover') == 1


def _supervisor() -> Any:
    source = Path(__file__).resolve().parents[2] / 'launcher' / 'supervisor.py'
    spec = importlib.util.spec_from_file_location('launcher_drain_supervisor_test', source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_client_reads_only_the_host_saved_api_credential(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / 'daedalus.sqlite') as conn:
        conn.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        conn.execute("INSERT INTO kv VALUES ('api_token','saved-secret')")
    (tmp_path / 'supervisor.token').write_text('launcher-secret\n')
    client = UpdateDrainClient(tmp_path)
    assert client._token() == 'saved-secret'
    assert client._supervisor_token() == 'launcher-secret'


@pytest.mark.asyncio
async def test_remote_restart_stops_only_after_durable_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _supervisor()
    supervisor = module.Supervisor()
    events: list[str] = []
    monkeypatch.setattr(module, 'head', lambda repo: BOT if repo == module.BOT_REPO else CORE)

    async def committed(bot: str, core: str, transcript: str) -> dict[str, str]:
        assert (bot, core) == (BOT, CORE)
        events.append('commit')
        return {'state': 'committed', 'receipt_id': 'receipt-1'}

    async def stop_child() -> None:
        events.append('stop')

    supervisor._drain_before_stop = committed
    supervisor.stop_child = stop_child
    await supervisor._restart_remote('operator asked')
    assert events == ['commit', 'stop'] and supervisor.restart_requested.is_set()


@pytest.mark.asyncio
async def test_rollback_preflights_then_commits_before_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _supervisor()
    supervisor = module.Supervisor()
    events: list[str] = []
    monkeypatch.setattr(module, 'head', lambda repo: BOT if repo == module.BOT_REPO else CORE)
    monkeypatch.setattr(module, 'load_history', lambda: [{'bot': 'c' * 40, 'core': 'd' * 40, 'at': 'then'}])
    monkeypatch.setattr(module, 'prepare_candidate', lambda repo, sha: (events.append('candidate'), (True, ''))[1])
    monkeypatch.setattr(module, 'preflight', lambda repo: (events.append('preflight'), (True, 'checked'))[1])
    supervisor._checkout = lambda bot, core: events.append('checkout')

    async def commit(bot: str, core: str, transcript: str) -> dict[str, str]:
        assert (bot, core, transcript) == ('c' * 40, 'd' * 40, 'checked')
        events.append('commit')
        return {'state': 'committed', 'receipt_id': 'receipt-1'}

    async def stop_child() -> None:
        events.append('stop')

    supervisor._drain_before_stop = commit
    supervisor.stop_child = stop_child
    result = await supervisor._rollback(0)
    assert result.startswith('rolled back')
    assert events == ['candidate', 'candidate', 'preflight', 'commit', 'stop', 'checkout']


@pytest.mark.asyncio
async def test_refused_rollback_keeps_running_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _supervisor()
    supervisor = module.Supervisor()
    events: list[str] = []
    monkeypatch.setattr(module, 'head', lambda repo: BOT if repo == module.BOT_REPO else CORE)
    monkeypatch.setattr(module, 'load_history', lambda: [{'bot': 'c' * 40, 'core': 'd' * 40, 'at': 'then'}])
    monkeypatch.setattr(module, 'prepare_candidate', lambda repo, sha: (True, ''))
    monkeypatch.setattr(module, 'preflight', lambda repo: (True, 'checked'))
    supervisor._checkout = lambda bot, core: events.append('checkout')

    async def refuse(*args: Any) -> None:
        raise module.update_drain.DrainRefused('blocked')

    async def stop_child() -> None:
        events.append('stop')

    supervisor._drain_before_stop = refuse
    supervisor.stop_child = stop_child
    assert 'drain refused' in await supervisor._rollback(0)
    assert events == []
