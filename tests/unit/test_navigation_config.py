"""The ``[navigation]`` section: where the sidebar's pinned rows and its More launcher put each destination."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from daedalus.config import NavigationConfig, RuntimeConfig
from daedalus.extensions.api import build_app
from daedalus.host.config_validation import config_revision, validate_candidate
from tests.unit.test_components import HEAD, FakeApp


def test_an_empty_section_is_the_shipped_layout_and_survives_the_file(tmp_path) -> None:
    config = RuntimeConfig()
    assert config.navigation == NavigationConfig()
    assert config.navigation.model_dump() == {"pinned": [], "more": [], "hidden": [], "icons": {}}
    path = tmp_path / "config.toml"
    laid_out = config.model_copy(update={"navigation": NavigationConfig(pinned=["board", "page:dosing"], hidden=["harnesses"], icons={"board": "flag"})})
    laid_out.save(path)
    assert RuntimeConfig.load(path).navigation == laid_out.navigation


def test_ids_are_screens_or_pages_and_a_repeat_is_dropped() -> None:
    nav = NavigationConfig(pinned=[" inbox ", "page:dosing", "inbox", "screen-of-a-later-build"])
    # An id nobody knows yet is kept: the app ignores it where it draws, and a page that comes back
    # finds its place again.
    assert nav.pinned == ["inbox", "page:dosing", "screen-of-a-later-build"]
    with pytest.raises(ValidationError):
        NavigationConfig(pinned=["<script>"])
    with pytest.raises(ValidationError):
        NavigationConfig(hidden=["page:"])
    with pytest.raises(ValidationError):
        NavigationConfig(icons={"inbox": "M4 4h16"})


def test_a_change_applies_at_the_next_step() -> None:
    current = RuntimeConfig()
    raw = current.model_dump(mode="json")
    raw["navigation"]["pinned"] = ["terminals", "inbox"]
    report, candidate = validate_candidate(current, raw, base_revision=config_revision(current))
    assert report.valid and candidate is not None
    assert [(change.path, change.apply_at) for change in report.changes] == [("navigation.pinned", "next_step")]


def test_settings_save_the_section_and_a_field_sent_replaces_it_whole(tmp_path) -> None:
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:
        view = client.get("/api/settings", headers=HEAD).json()
        assert view["navigation"] == {"pinned": [], "more": [], "hidden": [], "icons": {}}
        assert client.get("/api/navigation", headers=HEAD).json() == view["navigation"]

        saved = client.put("/api/settings", headers=HEAD, json={
            "base_revision": view["revision"],
            "navigation": {"pinned": ["terminals", "inbox"], "hidden": ["harnesses"], "icons": {"terminals": "bolt", "inbox": "bell"}},
        })
        assert saved.status_code == 200, saved.text
        assert app.config.navigation.pinned == ["terminals", "inbox"]
        assert client.get("/api/navigation", headers=HEAD).json()["icons"] == {"terminals": "bolt", "inbox": "bell"}

        # Merged key by key, taking an icon away was impossible: the old copy put it back.
        again = client.put("/api/settings", headers=HEAD, json={"base_revision": saved.json()["revision"], "navigation": {"icons": {"inbox": "bell"}}})
        assert again.status_code == 200, again.text
        assert app.config.navigation.icons == {"inbox": "bell"}
        # A field left out is kept.
        assert app.config.navigation.pinned == ["terminals", "inbox"] and app.config.navigation.hidden == ["harnesses"]

        refused = client.put("/api/settings", headers=HEAD, json={"base_revision": again.json()["revision"], "navigation": {"pinned": ["no spaces allowed"]}})
        assert refused.status_code == 400
        assert app.config.navigation.pinned == ["terminals", "inbox"]


def test_a_file_written_with_the_rail_keeps_its_places_as_pinned(tmp_path) -> None:
    # The rail of icons became the sidebar's pinned rows: what the operator put on it carries over.
    path = tmp_path / "config.toml"
    path.write_text('[navigation]\nrail = ["terminals", "page:dosing", "inbox"]\nhidden = ["harnesses"]\n', encoding="utf-8")
    config = RuntimeConfig.load(path)
    assert config.navigation.pinned == ["terminals", "page:dosing", "inbox"]
    assert config.navigation.hidden == ["harnesses"]
    written = path.read_text(encoding="utf-8")
    assert "rail" not in written and "pinned" in written
    # Loading the rewritten file changes nothing more.
    assert RuntimeConfig.load(path) == config


def test_a_file_written_with_a_phone_drawer_loses_the_list_once(tmp_path) -> None:
    # The phone's drawer is the sidebar and follows `pinned`; a list kept for it is dropped from the
    # file, and the places that were not about the drawer carry over untouched.
    path = tmp_path / "config.toml"
    path.write_text('[navigation]\npinned = ["inbox"]\ndrawer = ["voice", "board"]\nhidden = ["voice"]\n', encoding="utf-8")
    config = RuntimeConfig.load(path)
    assert config.navigation.pinned == ["inbox"]
    assert "drawer" not in config.navigation.model_dump()
    rewritten = path.read_text(encoding="utf-8")
    assert "drawer" not in rewritten
    assert RuntimeConfig.load(path).navigation.hidden == ["companion"]
