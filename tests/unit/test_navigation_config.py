"""The ``[navigation]`` section: where the rail, its More menu and the phone drawer put each destination."""

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
    assert config.navigation.model_dump() == {"rail": [], "more": [], "drawer": [], "hidden": [], "icons": {}}
    path = tmp_path / "config.toml"
    laid_out = config.model_copy(update={"navigation": NavigationConfig(rail=["board", "page:dosing"], hidden=["voice"], icons={"board": "flag"})})
    laid_out.save(path)
    assert RuntimeConfig.load(path).navigation == laid_out.navigation


def test_ids_are_screens_or_pages_and_a_repeat_is_dropped() -> None:
    nav = NavigationConfig(rail=[" inbox ", "page:dosing", "inbox", "screen-of-a-later-build"])
    # An id nobody knows yet is kept: the app ignores it where it draws, and a page that comes back
    # finds its place again.
    assert nav.rail == ["inbox", "page:dosing", "screen-of-a-later-build"]
    with pytest.raises(ValidationError):
        NavigationConfig(rail=["<script>"])
    with pytest.raises(ValidationError):
        NavigationConfig(hidden=["page:"])
    with pytest.raises(ValidationError):
        NavigationConfig(icons={"inbox": "M4 4h16"})


def test_a_change_applies_at_the_next_step() -> None:
    current = RuntimeConfig()
    raw = current.model_dump(mode="json")
    raw["navigation"]["rail"] = ["terminals", "inbox"]
    report, candidate = validate_candidate(current, raw, base_revision=config_revision(current))
    assert report.valid and candidate is not None
    assert [(change.path, change.apply_at) for change in report.changes] == [("navigation.rail", "next_step")]


def test_settings_save_the_section_and_a_field_sent_replaces_it_whole(tmp_path) -> None:
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:
        view = client.get("/api/settings", headers=HEAD).json()
        assert view["navigation"] == {"rail": [], "more": [], "drawer": [], "hidden": [], "icons": {}}
        assert client.get("/api/navigation", headers=HEAD).json() == view["navigation"]

        saved = client.put("/api/settings", headers=HEAD, json={
            "base_revision": view["revision"],
            "navigation": {"rail": ["terminals", "inbox"], "hidden": ["voice"], "icons": {"terminals": "bolt", "inbox": "bell"}},
        })
        assert saved.status_code == 200, saved.text
        assert app.config.navigation.rail == ["terminals", "inbox"]
        assert client.get("/api/navigation", headers=HEAD).json()["icons"] == {"terminals": "bolt", "inbox": "bell"}

        # Merged key by key, taking an icon away was impossible: the old copy put it back.
        again = client.put("/api/settings", headers=HEAD, json={"base_revision": saved.json()["revision"], "navigation": {"icons": {"inbox": "bell"}}})
        assert again.status_code == 200, again.text
        assert app.config.navigation.icons == {"inbox": "bell"}
        # A field left out is kept.
        assert app.config.navigation.rail == ["terminals", "inbox"] and app.config.navigation.hidden == ["voice"]

        refused = client.put("/api/settings", headers=HEAD, json={"base_revision": again.json()["revision"], "navigation": {"rail": ["no spaces allowed"]}})
        assert refused.status_code == 400
        assert app.config.navigation.rail == ["terminals", "inbox"]
