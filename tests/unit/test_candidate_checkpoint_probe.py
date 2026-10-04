"""A pinned candidate must read the exact parked bytes before its predecessor exits."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import protocore.contracts.snapshot as snapshot_contract
import pytest

from launcher import update_drain
from launcher.update_drain import DrainRefused, verify_candidate_checkpoints


def candidate(repo: Path, source: Path | None = None) -> str:
    repo.mkdir()
    if source is None:
        (repo / 'README').write_text('candidate', encoding='utf-8')
    else:
        target = repo / 'protocore/contracts/snapshot.py'
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    environment = dict(os.environ, GIT_AUTHOR_NAME='someone', GIT_AUTHOR_EMAIL='someone@example.invalid',
                       GIT_COMMITTER_NAME='someone', GIT_COMMITTER_EMAIL='someone@example.invalid')
    for args in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'Candidate snapshot contract']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, env=environment, capture_output=True)
    return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                          capture_output=True, text=True).stdout.strip()


def saved_run(state: Path, saved: dict) -> tuple[list[dict], str]:
    state.mkdir()
    db_path = state / 'daedalus.sqlite'
    raw = json.dumps(saved)
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE runs(id TEXT,tenant_id TEXT,session_id TEXT,status TEXT)')
        conn.execute('CREATE TABLE snapshots(run_id TEXT,session_id TEXT,snapshot TEXT,updated_at TEXT)')
        conn.execute("INSERT INTO runs VALUES ('parked','tenant','session','paused')")
        conn.execute('INSERT INTO snapshots VALUES (?,?,?,?)', ('parked', 'session', raw, 'when'))
    return ([{'run_id': 'parked', 'session_id': 'session', 'updated_at': 'when',
              'sha256': hashlib.sha256(raw.encode()).hexdigest()}], raw)


def test_candidate_decoder_reads_pinned_bytes_without_writing_state(tmp_path: Path) -> None:
    bot, core, state = tmp_path / 'bot', tmp_path / 'core', tmp_path / 'state'
    bot_sha = candidate(bot)
    core_sha = candidate(core, Path(snapshot_contract.__file__))
    saved = {'schema_version': 7, 'run_id': 'parked', 'tenant_id': 'tenant',
             'session_id': 'session', 'model_name': 'model', 'history': []}
    references, _ = saved_run(state, saved)
    before = (state / 'daedalus.sqlite').read_bytes()
    digest = verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                          bot_sha, core_sha, references)
    assert len(digest) == 64
    assert (state / 'daedalus.sqlite').read_bytes() == before
    with pytest.raises(DrainRefused, match='candidate checkout changed'):
        verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                     'a' * 40, core_sha, references)
    with pytest.raises(DrainRefused, match='saved checkpoint changed'):
        verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                     bot_sha, core_sha, [{**references[0], 'sha256': '0' * 64}])
    with pytest.raises(DrainRefused, match='preflight environment is unavailable'):
        verify_candidate_checkpoints(state, bot, core, tmp_path / 'missing-venv',
                                     bot_sha, core_sha, references)
    with (core / 'protocore/contracts/snapshot.py').open('a', encoding='utf-8') as handle:
        handle.write('\n# A dirty decoder must not stand in for its pinned commit.\n')
    with pytest.raises(DrainRefused, match='decoder differs from its pinned revision'):
        verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                     bot_sha, core_sha, references)


@pytest.mark.parametrize('change', [
    {'session_id': 'different'},
    {'schema_version': 999},
    {'background_task_ids': ['unconfirmed']},
    {'open_intents': [{'state': 'DISPATCHED', 'outcome': 'pending'}]},
])
def test_candidate_decoder_refuses_incompatible_or_unsafe_snapshot(tmp_path: Path, change: dict) -> None:
    bot, core, state = tmp_path / 'bot', tmp_path / 'core', tmp_path / 'state'
    bot_sha = candidate(bot)
    core_sha = candidate(core, Path(snapshot_contract.__file__))
    saved = {'schema_version': 7, 'run_id': 'parked', 'tenant_id': 'tenant',
             'session_id': 'session', 'model_name': 'model', 'history': [], **change}
    references, _ = saved_run(state, saved)
    with pytest.raises(DrainRefused, match='pinned candidate cannot read'):
        verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                     bot_sha, core_sha, references)


def test_candidate_decoder_refuses_import_time_effect_before_it_runs(tmp_path: Path) -> None:
    bot, core, state = tmp_path / 'bot', tmp_path / 'core', tmp_path / 'state'
    marker = tmp_path / 'unexpected-effect'
    modified = tmp_path / 'snapshot.py'
    modified.write_text(Path(snapshot_contract.__file__).read_text(encoding='utf-8')
                        + f"\nopen({str(marker)!r}, 'w').write('bad')\n", encoding='utf-8')
    bot_sha = candidate(bot)
    core_sha = candidate(core, modified)
    references, _ = saved_run(state, {'schema_version': 7, 'run_id': 'parked',
                                      'tenant_id': 'tenant', 'session_id': 'session',
                                      'model_name': 'model', 'history': []})
    with pytest.raises(DrainRefused, match='pinned candidate cannot read'):
        verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                     bot_sha, core_sha, references)
    assert not marker.exists()


@pytest.mark.parametrize('body', ['write', 'read', 'network'])
def test_candidate_function_cannot_reach_host_files_or_network(tmp_path: Path, body: str) -> None:
    bot, core, state = tmp_path / 'bot', tmp_path / 'core', tmp_path / 'state'
    marker = tmp_path / 'outside-sandbox'
    if body == 'read':
        marker.write_text('secret', encoding='utf-8')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    try:
        statement = {
            'write': f"open({str(marker)!r}, 'w').write('bad')",
            'read': f"open({str(marker)!r}).read()",
            'network': f"__import__('socket').create_connection(('127.0.0.1', {listener.getsockname()[1]}), 1)",
        }[body]
        modified = tmp_path / 'snapshot.py'
        modified.write_text('from __future__ import annotations\n'
                            'def migrate_snapshot(saved):\n'
                            f'    {statement}\n'
                            '    return saved\n', encoding='utf-8')
        bot_sha = candidate(bot)
        core_sha = candidate(core, modified)
        references, _ = saved_run(state, {'schema_version': 7, 'run_id': 'parked',
                                          'tenant_id': 'tenant', 'session_id': 'session',
                                          'model_name': 'model', 'history': []})
        with pytest.raises(DrainRefused, match='pinned candidate cannot read'):
            verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                         bot_sha, core_sha, references)
        if body == 'write':
            assert not marker.exists()
        elif body == 'read':
            assert marker.read_text(encoding='utf-8') == 'secret'
    finally:
        listener.close()


def test_checkpoint_verification_stays_closed_without_sandbox(tmp_path: Path, monkeypatch) -> None:
    bot, core, state = tmp_path / 'bot', tmp_path / 'core', tmp_path / 'state'
    bot_sha = candidate(bot)
    core_sha = candidate(core, Path(snapshot_contract.__file__))
    references, _ = saved_run(state, {'schema_version': 7, 'run_id': 'parked',
                                      'tenant_id': 'tenant', 'session_id': 'session',
                                      'model_name': 'model', 'history': []})
    monkeypatch.setattr(update_drain.shutil, 'which', lambda _: None)
    with pytest.raises(DrainRefused, match='read-only network-isolated sandbox'):
        verify_candidate_checkpoints(state, bot, core, Path(sys.executable).parents[1],
                                     bot_sha, core_sha, references)
