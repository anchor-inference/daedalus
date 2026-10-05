"""The supervisor's authenticated, fail-closed handshake with the running host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


class DrainRefused(RuntimeError):
    """The host has not durably committed an update for the exact candidate."""


def checkpoint_refs_digest(references: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(references, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_candidate_checkpoints(state: Path, candidate_bot: Path, candidate_core: Path,
                                 preflight_venv: Path, bot_sha: str, core_sha: str,
                                 references: list[dict[str, Any]]) -> str:
    """Read exact saved bytes with the detached candidate decoder before stopping the host."""
    if not isinstance(references, list):
        raise DrainRefused('the checkpoint reference list is invalid')
    def pinned() -> None:
        for repo, expected in ((candidate_bot, bot_sha), (candidate_core, core_sha)):
            try:
                observed = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                          capture_output=True, text=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DrainRefused('the candidate checkout cannot be verified') from exc
            if observed.returncode or observed.stdout.strip() != expected:
                raise DrainRefused('the candidate checkout changed before checkpoint verification')
    pinned()
    python = preflight_venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    decoder = candidate_core / 'protocore/contracts/snapshot.py'
    if not python.is_file() or not decoder.is_file():
        raise DrainRefused('the candidate checkpoint decoder or preflight environment is unavailable')
    try:
        committed_decoder = subprocess.run(
            ['git', '-C', str(candidate_core), 'rev-parse', 'HEAD:protocore/contracts/snapshot.py'],
            capture_output=True, text=True, timeout=5, check=False)
        actual_decoder = subprocess.run(['git', '-C', str(candidate_core), 'hash-object', str(decoder)],
                                        capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DrainRefused('the pinned candidate decoder bytes cannot be verified') from exc
    if (committed_decoder.returncode or actual_decoder.returncode
            or committed_decoder.stdout.strip() != actual_decoder.stdout.strip()):
        raise DrainRefused('the candidate decoder differs from its pinned revision')
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with sqlite3.connect(state.joinpath('daedalus.sqlite').as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
            conn.execute('PRAGMA query_only = ON')
            conn.execute('BEGIN')
            for reference in references:
                if (not isinstance(reference, dict) or not isinstance(reference.get('run_id'), str)
                        or reference['run_id'] in seen):
                    raise DrainRefused('the checkpoint reference list is invalid')
                seen.add(reference['run_id'])
                row = conn.execute('SELECT r.tenant_id,r.session_id,r.status,s.snapshot,s.session_id,s.updated_at'
                                   ' FROM runs r JOIN snapshots s ON s.run_id = r.id WHERE r.id = ?',
                                   (reference['run_id'],)).fetchone()
                if (row is None or row[2] != 'paused' or row[1] != reference.get('session_id')
                        or row[4] != reference.get('session_id') or row[5] != reference.get('updated_at')
                        or hashlib.sha256(row[3].encode()).hexdigest() != reference.get('sha256')):
                    raise DrainRefused('a saved checkpoint changed before candidate verification')
                entries.append({'run_id': reference['run_id'], 'tenant_id': row[0],
                                'session_id': row[1], 'snapshot': row[3]})
            active = conn.execute("SELECT id,status FROM runs WHERE status IN ('queued','running','paused')").fetchall()
            if any(status != 'paused' or run_id not in seen for run_id, status in active):
                raise DrainRefused('an unverified native run remains before candidate verification')
    except (sqlite3.Error, OSError) as exc:
        raise DrainRefused('the saved checkpoints cannot be read without changing them') from exc
    sandbox = shutil.which('bwrap') if os.name == 'posix' else None
    if sandbox is None:
        raise DrainRefused('checkpoint verification requires a read-only network-isolated sandbox')
    command = [sandbox, '--unshare-all', '--unshare-net', '--die-with-parent', '--new-session',
               '--ro-bind', '/usr/lib', '/usr/lib', '--ro-bind', '/usr/lib64', '/usr/lib64',
               '--symlink', 'usr/lib64', '/lib64', '--symlink', 'usr/lib', '/lib',
               '--ro-bind', str(preflight_venv), '/venv',
               '--ro-bind', str(Path(__file__).with_name('checkpoint_probe.py')), '/probe.py',
               '--ro-bind', str(decoder), '/snapshot.py',
               '--tmpfs', '/tmp', '--proc', '/proc', '--dev', '/dev', '--chdir', '/']
    real_python = python.resolve()
    if real_python.parent == Path('/usr/bin'):
        command.extend(['--ro-bind', '/usr/bin', '/usr/bin'])
    elif real_python.parent == Path('/usr/local/bin'):
        command.extend(['--ro-bind', '/usr/local/bin', '/usr/local/bin',
                        '--ro-bind', '/usr/local/lib', '/usr/local/lib'])
    else:
        alias = python.readlink() if python.is_symlink() else real_python
        if not alias.is_absolute():
            alias = (python.parent / alias).absolute()
        runtime_root = Path(os.path.commonpath((str(alias), str(real_python))))
        if not runtime_root.is_dir():
            runtime_root = runtime_root.parent
        if (runtime_root == Path('/') or runtime_root.is_relative_to(Path.home())
                or Path.home().is_relative_to(runtime_root)
                or state.is_relative_to(runtime_root) or candidate_bot.is_relative_to(runtime_root)):
            raise DrainRefused('the candidate Python runtime cannot be mounted without private state')
        command.extend(['--ro-bind', str(runtime_root), str(runtime_root)])
    command.extend(['/venv/bin/python', '-I', '-S', '-B', '/probe.py', '/snapshot.py'])
    try:
        probe = subprocess.run(command, input=json.dumps(entries), capture_output=True,
                               text=True, timeout=30, cwd='/', env={'PYTHONUTF8': '1'}, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DrainRefused('the pinned candidate checkpoint decoder did not finish') from exc
    if probe.returncode:
        raise DrainRefused('the pinned candidate cannot read every saved checkpoint: ' + probe.stderr[-500:])
    pinned()
    return checkpoint_refs_digest(references)


def candidate_digest(bot_sha: str, core_sha: str, transcript: str) -> str:
    """Bind the checked pair and its preflight output into one update proposal."""
    payload = json.dumps([bot_sha, core_sha, transcript], separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class UpdateDrainClient:
    def __init__(self, state: Path, *, port: int | None = None, wait_seconds: float = 300.0) -> None:
        self.state = state
        self.port = port if port is not None else int(os.environ.get('API_PORT', '8765'))
        self.wait_seconds = wait_seconds

    def _token(self) -> str:
        path = self.state / 'daedalus.sqlite'
        try:
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2) as conn:
                row = conn.execute("SELECT value FROM kv WHERE key = 'api_token'").fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise DrainRefused('the host API credential is unavailable') from exc
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise DrainRefused('the host API credential is unavailable')
        return row[0]

    def _supervisor_token(self) -> str:
        try:
            token = (self.state / 'supervisor.token').read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise DrainRefused('the supervisor credential is unavailable') from exc
        if not token:
            raise DrainRefused('the supervisor credential is unavailable')
        return token

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=data, method=method,
            headers={'x-daedalus-token': self._token(),
                     'x-daedalus-supervisor-token': self._supervisor_token(),
                     'content-type': 'application/json'},
        )
        try:
            # The credential belongs only on this installation's loopback channel.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
                result = json.load(response)
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            raise DrainRefused(f'the host did not confirm {method} {path}') from exc
        if not isinstance(result, dict):
            raise DrainRefused('the host returned an invalid update receipt')
        return result

    @staticmethod
    def _drain(reply: dict[str, Any], expected: dict[str, Any], generation: int | None = None) -> dict[str, Any]:
        drain = reply.get('drain')
        if not isinstance(drain, dict):
            raise DrainRefused('the host has no durable update drain')
        for field, value in expected.items():
            if drain.get(field) != value:
                raise DrainRefused(f'the host update drain changed {field}')
        if generation is not None and (reply.get('host_generation') != generation
                                       or drain.get('host_generation') != generation):
            raise DrainRefused('the execution host generation changed')
        if reply.get('admission_open') is not False or not isinstance(drain.get('receipt_id'), str):
            raise DrainRefused('admission closure has no durable receipt')
        return drain

    async def commit(self, bot_sha: str, core_sha: str, transcript: str, *, policy: str = 'stop_all',
                     verify_checkpoints: Callable[[list[dict[str, Any]]], str] | None = None) -> dict[str, Any]:
        if (len(bot_sha) != 40 or len(core_sha) != 40
                or any(c not in '0123456789abcdef' for c in bot_sha + core_sha)):
            raise DrainRefused('the candidate does not have two pinned commit hashes')
        if policy not in ('stop_all', 'checkpoint_supported'):
            raise DrainRefused('the update policy is unknown')
        expected = {'candidate_bot_sha': bot_sha, 'candidate_core_sha': core_sha,
                    'candidate_compatibility_digest': candidate_digest(bot_sha, core_sha, transcript),
                    'policy': policy}
        first = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
        generation = first.get('host_generation')
        if not isinstance(generation, int) or generation < 1:
            raise DrainRefused('the host generation is unavailable')
        current = first.get('drain')
        if isinstance(current, dict) and current.get('state') in ('aborted', 'resumed'):
            # The host retains the last drain for inspection. Its terminal record
            # cannot be reused for a new candidate, but open admission permits a new one.
            if first.get('admission_open') is not True:
                raise DrainRefused('the terminal update drain did not reopen admission')
            current = None
        if current is not None:
            # A previous lost response may have closed admission. Only the exact pinned
            # candidate can be resumed; a committed unknown update cannot be repurposed.
            drain = self._drain(first, expected, generation)
            if drain.get('state') == 'committed':
                raise DrainRefused('the previous committed update awaits successor recovery')
            if drain.get('state') not in ('draining', 'blocked', 'ready'):
                raise DrainRefused('the existing update drain cannot be resumed')
        else:
            revision = first.get('collection_revision')
            if not isinstance(revision, int) or revision < 1:
                raise DrainRefused('the host collection revision is unavailable')
            body = {**expected, 'expected_host_generation': generation,
                    'expected_collection_revision': revision, 'client_operation_id': secrets.token_hex(16)}
            try:
                await asyncio.to_thread(self._request, 'POST', '/api/runtime/updates/drain', body)
            except DrainRefused:
                # A response can be lost after the host has closed admission. Inspect
                # durable state instead of issuing a second, differently keyed command.
                pass
            observed = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
            drain = self._drain(observed, expected, generation)
        deadline = time.monotonic() + self.wait_seconds
        while drain.get('state') != 'ready':
            if drain.get('state') not in ('blocked', 'draining') or time.monotonic() >= deadline:
                raise DrainRefused(f"the host update drain is {drain.get('state')}")
            await asyncio.sleep(0.5)
            observed = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
            drain = self._drain(observed, expected, generation)
            if drain.get('state') == 'committed':
                if drain.get('checkpoint_refs'):
                    raise DrainRefused('candidate checkpoint compatibility is unverified')
                if not isinstance(drain.get('commit_receipt_id'), str) or not drain['commit_receipt_id']:
                    raise DrainRefused('the update commitment has no durable receipt')
                return drain
            revision = observed.get('collection_revision')
            if not isinstance(revision, int) or revision < 1:
                raise DrainRefused('the host collection revision is unavailable')
            body = {'expected_host_generation': generation, 'expected_collection_revision': revision,
                    'client_operation_id': secrets.token_hex(16)}
            try:
                await asyncio.to_thread(self._request, 'POST',
                                        f"/api/runtime/updates/drain/{drain['id']}/recheck", body)
            except DrainRefused:
                # The recheck may have been applied; the next GET is authoritative.
                pass
            observed = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
            drain = self._drain(observed, expected, generation)
        references = drain.get('checkpoint_refs')
        if not isinstance(references, list):
            raise DrainRefused('the host returned invalid checkpoint references')
        if references:
            if policy != 'checkpoint_supported' or verify_checkpoints is None:
                raise DrainRefused('candidate checkpoint compatibility is unverified')
            verified_digest = await asyncio.to_thread(verify_checkpoints, references)
            if verified_digest != checkpoint_refs_digest(references):
                raise DrainRefused('candidate checkpoint verification changed its references')
        else:
            verified_digest = checkpoint_refs_digest([])
        observed = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
        drain = self._drain(observed, expected, generation)
        if drain.get('state') != 'ready' or drain.get('checkpoint_refs') != references:
            raise DrainRefused('the update was no longer ready before commitment')
        revision = observed.get('collection_revision')
        if not isinstance(revision, int) or revision < 1:
            raise DrainRefused('the host collection revision is unavailable')
        body = {**expected, 'decision': 'commit', 'checkpoint_refs_digest': verified_digest,
                'expected_host_generation': generation,
                'expected_collection_revision': revision, 'client_operation_id': secrets.token_hex(16)}
        try:
            await asyncio.to_thread(self._request, 'POST',
                                    f"/api/runtime/updates/drain/{drain['id']}/decision", body)
        except DrainRefused:
            # A lost commit response is not a rejection. Confirm the saved decision.
            pass
        final = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
        committed = self._drain(final, expected, generation)
        if (committed.get('id') != drain.get('id') or committed.get('state') != 'committed'
                or committed.get('checkpoint_refs') != references
                or not isinstance(committed.get('commit_receipt_id'), str)
                or not committed['commit_receipt_id']):
            raise DrainRefused('the exact update commitment is unconfirmed')
        return committed

    async def recover(self, installed_bot_sha: str, installed_core_sha: str) -> dict[str, Any] | None:
        """Ask the new host to reconcile only the update committed by its predecessor."""
        observed = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
        drain = observed.get('drain')
        if drain is None:
            return None
        if not isinstance(drain, dict):
            raise DrainRefused('the successor returned an invalid drain')
        if drain.get('state') in ('resumed', 'aborted'):
            return None
        if drain.get('state') != 'committed':
            raise DrainRefused('an unfinished update drain still holds admission closed')
        generation = observed.get('host_generation')
        if (not isinstance(generation, int) or generation <= drain.get('host_generation', generation)
                or drain.get('candidate_bot_sha') != installed_bot_sha
                or drain.get('candidate_core_sha') != installed_core_sha
                or not isinstance(drain.get('commit_receipt_id'), str) or not drain['commit_receipt_id']):
            raise DrainRefused('the installed successor does not match the committed update')
        revision = observed.get('collection_revision')
        if not isinstance(revision, int) or revision < 1:
            raise DrainRefused('the successor collection revision is unavailable')
        body = {'commit_receipt_id': drain['commit_receipt_id'], 'installed_bot_sha': installed_bot_sha,
                'installed_core_sha': installed_core_sha, 'expected_host_generation': generation,
                'expected_collection_revision': revision, 'client_operation_id': secrets.token_hex(16)}
        try:
            await asyncio.to_thread(self._request, 'POST',
                                    f"/api/runtime/updates/drain/{drain['id']}/recover", body)
        except DrainRefused:
            # The response may be lost after the successor committed admission reopening.
            pass
        final = await asyncio.to_thread(self._request, 'GET', '/api/runtime/updates/drain')
        recovered = final.get('drain')
        if (not isinstance(recovered, dict) or recovered.get('id') != drain['id']
                or recovered.get('state') != 'resumed' or final.get('admission_open') is not True
                or final.get('host_generation') != generation):
            raise DrainRefused('the successor did not confirm recovery of the exact update')
        return recovered
