"""The speech model chosen in the launcher's setup wizard: the copy of the catalog it carries, the
transcriber its answer sets, and the download that follows the first start."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import RuntimeConfig
from daedalus.speech import catalog
from daedalus.speech.models import Progress
from daedalus.speech.setup_model import fetch_setup_model, marker

ROOT = Path(__file__).resolve().parents[2]
CHOSEN = "gigaam-ru"


def test_the_wizard_carries_the_catalog_as_it_is() -> None:
    copy = (ROOT / "desktop" / "speech_models.json").read_text(encoding="utf-8")
    assert copy == catalog.wizard_json(), (
        "desktop/speech_models.json is not the catalog any more; write it again with "
        "`uv run python -c \"from daedalus.speech.catalog import wizard_json; print(wizard_json(), end='')\" > desktop/speech_models.json`"
    )


def test_every_model_in_the_copy_has_a_line_in_the_wizard_in_both_languages() -> None:
    words = (ROOT / "desktop" / "i18n.go").read_text(encoding="utf-8")
    for model in catalog.MODELS:
        assert words.count(f'"hero.speech.m.{model.id}"') == 2, f"the wizard has no line for {model.id} in one of its languages"


@pytest.fixture(autouse=True)
def _no_setup_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DAEDALUS_LOCAL_MODEL", "DAEDALUS_FREE_PROVIDER", "DAEDALUS_FREE_MODEL", "DAEDALUS_VOICE", "DAEDALUS_STT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_the_chosen_model_becomes_the_transcriber_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_STT_MODEL", CHOSEN)
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)
    assert config.asr.transcriber == CHOSEN and f"stt-model:{CHOSEN}" in config.seeded
    # What the operator picks afterwards in Settings stays picked across restarts.
    config.asr.transcriber = "cloud"
    config.save(path)
    assert RuntimeConfig.load(path).asr.transcriber == "cloud"


def test_a_model_the_catalog_does_not_have_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_STT_MODEL", "no-such-model")
    config = RuntimeConfig.load(tmp_path / "config.toml")
    assert config.asr.transcriber == "cloud" and not any(s.startswith("stt-model:") for s in config.seeded)


class FakeDownloads:
    """The part of Downloads the fetch uses, finishing or failing when the test says so."""

    def __init__(self, installed: bool = False) -> None:
        self.installed = installed
        self.started: list[str] = []
        self.queues: list[asyncio.Queue[Progress]] = []

    def is_installed(self, model_id: str) -> bool:
        return self.installed

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue[Progress]]:
        queue: asyncio.Queue[Progress] = asyncio.Queue()
        self.queues.append(queue)
        try:
            yield queue
        finally:
            self.queues.remove(queue)

    def start(self, model_id: str) -> Progress:
        self.started.append(model_id)
        return Progress(id=model_id, state="downloading")

    def publish(self, model_id: str, state: str) -> None:
        for queue in self.queues:
            queue.put_nowait(Progress(id=model_id, state=state))


class FakeApp:
    def __init__(self, config: RuntimeConfig, downloads: FakeDownloads) -> None:
        self.config = config
        self.speech = type("Speech", (), {"downloads": downloads})()
        self.saved: list[RuntimeConfig] = []

    async def save_config(self, config: RuntimeConfig) -> None:
        self.saved.append(config)
        self.config = config


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: FakeDownloads) -> FakeApp:
    monkeypatch.setenv("DAEDALUS_STT_MODEL", CHOSEN)
    return FakeApp(RuntimeConfig.load(tmp_path / "config.toml"), downloads)


async def _until_watching(downloads: FakeDownloads) -> None:
    while not downloads.queues:
        await asyncio.sleep(0)


async def test_the_chosen_model_is_downloaded_and_remembered_when_it_arrives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloads = FakeDownloads()
    app = _app(tmp_path, monkeypatch, downloads)
    task = asyncio.create_task(fetch_setup_model(app))
    await _until_watching(downloads)
    assert downloads.started == [CHOSEN] and not app.saved
    downloads.publish("whisper-tiny", "installed")  # someone else's download is not this one
    downloads.publish(CHOSEN, "unpacking")
    downloads.publish(CHOSEN, "installed")
    await asyncio.wait_for(task, 2)
    assert marker(CHOSEN) in app.config.seeded
    # Fetched once: deleted on the Voice page afterwards, it is not fetched again at the next start.
    again = FakeDownloads()
    app.speech.downloads = again
    await fetch_setup_model(app)
    assert again.started == []


async def test_a_failed_download_is_not_remembered_so_the_next_start_tries_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloads = FakeDownloads()
    app = _app(tmp_path, monkeypatch, downloads)
    task = asyncio.create_task(fetch_setup_model(app))
    await _until_watching(downloads)
    downloads.publish(CHOSEN, "failed")
    await asyncio.wait_for(task, 2)
    assert not app.saved and marker(CHOSEN) not in app.config.seeded


async def test_nothing_is_fetched_once_the_operator_chose_another_transcriber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloads = FakeDownloads()
    app = _app(tmp_path, monkeypatch, downloads)
    app.config.asr.transcriber = "cloud"
    await fetch_setup_model(app)
    assert downloads.started == [] and not app.saved


async def test_a_model_already_on_the_disk_is_only_remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloads = FakeDownloads(installed=True)
    app: Any = _app(tmp_path, monkeypatch, downloads)
    await fetch_setup_model(app)
    assert downloads.started == [] and marker(CHOSEN) in app.config.seeded
