from __future__ import annotations

import os
from pathlib import Path

import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.stores.database import Database
from tests.support.models import model_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_collection_finish(session: pytest.Session) -> None:
    """Take everything that exists once the tests are collected out of the collector's sight.

    By then every module of the code base and of the suite is imported, which is well over a million
    objects that live until the process ends. The cyclic collector walks all of them on every full
    collection, and it runs one every so often for the whole of a suite that allocates constantly:
    measured over a third of the suite, thirty-one full collections took twenty seconds between them,
    and each one costs more as the run goes on. ``gc.freeze`` moves what exists now into a generation
    the collector never scans; what the tests themselves allocate is collected exactly as before.
    """
    import gc

    gc.collect()
    gc.freeze()


def rebuilder_at(tmp_path: Path) -> Path:
    """A trigger directory with a fresh heartbeat in it: what a running rebuilder looks like.

    The capability probe asks whether something is on the other end of that directory, so a test that
    wants the full self-development surface has to put a rebuilder there rather than rely on the
    compose file naming one (it always does; the service is behind a profile that is off by default).
    """
    directory = tmp_path / "rebuild-trigger"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "alive").write_text("")
    return directory


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    os.environ.setdefault("OWNER_USER_ID", "1")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        state_dir=tmp_path / "state",
        workspaces_dir=tmp_path / "workspaces",
        bot_repo_dir=REPO_ROOT,
        core_repo_dir=REPO_ROOT.parent / "protocore-exp",
        owner_user_id=1,
        telegram_bot_token="123:abc",
        # The self-development prerequisites, stubbed: the checkouts are really there in the repository
        # under test, and these two stand in for the token and for a running rebuilder, so the capability
        # probe resolves to server mode and the suite exercises the full surface.
        github_token="stub-token-for-the-capability-probe",
        rebuild_trigger_dir=rebuilder_at(tmp_path),
    )


@pytest.fixture
def config() -> RuntimeConfig:
    """A configuration that can run: the shipped defaults carry no model at all."""
    return model_config()


@pytest.fixture
async def db(settings: Settings) -> Database:
    database = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await database.open()
    yield database  # type: ignore[misc]
    await database.close()


@pytest.fixture(scope="session", autouse=True)
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> object:
    """Every new database in the suite starts as a copy of one migrated once, not as an empty file.

    Running the hundred-odd migrations on an empty file costs about 0.2 s, and nearly every test that
    touches the host opens a fresh database — directly, through ``DaedalusApp`` or through a session
    manager — so the suite spent minutes replaying the same schema. The copy is made by the very same
    ``Database.open`` on an empty path, and ``open`` still runs on the copy (pragmas, the version
    check, ``_migrate`` finding nothing left to do), so what a test sees is byte for byte what it
    would have built itself. The migrations on an empty database insert nothing that depends on the
    workspaces folder or the environment, which is why one template serves every test.

    The copy is skipped whenever a test has replaced ``MIGRATIONS`` (to stop at an older schema or to
    break one step) or the path already exists (a test that built an old database by hand): those
    tests are about migrating, and they get the real walk from wherever they start.
    """
    import asyncio
    import shutil

    from daedalus.stores import database as database_module

    template = tmp_path_factory.mktemp("migrated-template") / "template.sqlite"

    async def build() -> None:
        database = Database(template, workspaces_dir=template.parent / "workspaces")
        await database.open()
        await database.close()  # the last connection closing folds the write-ahead log into the file

    asyncio.run(build())
    migrations, count = database_module.MIGRATIONS, len(database_module.MIGRATIONS)
    real_open = Database.open

    async def open_from_template(self: Database) -> None:
        if database_module.MIGRATIONS is migrations and len(migrations) == count and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, self.path)
        await real_open(self)

    Database.open = open_from_template  # type: ignore[method-assign]
    try:
        yield template
    finally:
        Database.open = real_open  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def no_push_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The doctor's push dry run is answered in place for every test, not sent to the remote.

    It runs in a git subprocess, which the socket guard below cannot see: every test that ran the
    doctor in server mode asked GitHub twice, about a second each. See ``tests/support/doctor.py``.
    """
    from tests.support.doctor import answer_the_push_probe

    answer_the_push_probe(monkeypatch)


class NetworkBlocked(OSError):
    """Raised in place of a connection the test suite is not allowed to make.

    An ``OSError`` on purpose, and not something exotic: every client in this code base already has a
    path for "the network did not answer", and that path is what a test on a machine with no route
    out would take. Refusing the connection this way exercises it rather than bypassing it.
    """


BLOCKED: list[str] = []
"""Every non-loopback address something tried to reach, in order, for when a test has to be found."""


def _loopback(address: object) -> bool:
    """Whether an address is this machine talking to itself, which the suite does constantly.

    The launcher fake, the model host fake, the supervisor's TCP port and every ASGI client are real
    sockets on 127.0.0.1 or a unix path, so the ban is on leaving the machine and not on sockets.
    """
    if not isinstance(address, tuple) or not address:
        return True  # a unix socket, or something with no host in it at all
    host = str(address[0] or "")
    return host in ("", "::", "0.0.0.0") or host.startswith("127.") or host in ("localhost", "::1", "::ffff:127.0.0.1")


@pytest.fixture(scope="session", autouse=True)
def no_network() -> object:
    """The suite may not leave the machine, and an attempt is refused rather than waited out.

    This is not tidiness. A real outbound call in a unit test is a test whose result depends on
    somebody else's server: it fails when that server is down, it leaks the machine's address to it,
    and — the reason this exists — it can hang. ``SessionManager.start`` refreshes model prices from
    models.dev on its first tick, so *every* test that starts a manager opened an HTTPS connection;
    when one of those was left half-closed the suite stopped dead, with the sockets in CLOSE-WAIT and
    nothing on the terminal, for as long as anybody was willing to wait.

    Patched at ``socket.socket.connect``, which is underneath httpx, aiohttp, asyncio's own
    ``create_connection`` and the standard library alike, so there is no client left to forget.
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self: socket.socket, address: object) -> object:
        if _loopback(address):
            return real_connect(self, address)  # type: ignore[arg-type]
        BLOCKED.append(str(address))
        raise NetworkBlocked(f"the test suite may not open a connection to {address!r}")

    def guard_ex(self: socket.socket, address: object) -> int:
        if _loopback(address):
            return real_connect_ex(self, address)  # type: ignore[arg-type]
        BLOCKED.append(str(address))
        raise NetworkBlocked(f"the test suite may not open a connection to {address!r}")

    socket.socket.connect = guard  # type: ignore[method-assign]
    socket.socket.connect_ex = guard_ex  # type: ignore[method-assign]
    try:
        yield None
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]


@pytest.fixture(scope="session", autouse=True)
def no_price_refresh() -> object:
    """A session manager started by a test does not go to models.dev for prices.

    The guard above refused the connection, but only after the name had been looked up: every test
    whose manager lived past the first tick of the daily refresh resolved models.dev through the
    machine's resolver, which the guard cannot see, and then logged "models.dev prices not
    refreshed". The background refresh is the only caller; a test about prices patches
    ``modelsdev.fetch_catalog`` and calls ``refresh_prices`` itself, which this leaves alone.
    """
    from daedalus.host.session_runner import SessionManager

    real_refresh = SessionManager._refresh_prices_daily

    async def refresh_nothing(self: SessionManager) -> None:
        return None

    SessionManager._refresh_prices_daily = refresh_nothing  # type: ignore[method-assign]
    try:
        yield None
    finally:
        SessionManager._refresh_prices_daily = real_refresh  # type: ignore[method-assign]
