from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from daedalus.app import Application
from daedalus.config import RuntimeConfig
from daedalus.extensions.api import build_app
from daedalus.host.config_validation import ConfigConflict, config_revision, validate_candidate
from tests.unit.test_components import HEAD, FakeApp, settings_for


def test_candidate_validation_is_pure_and_classifies_effective_changes() -> None:
    current = RuntimeConfig()
    raw = current.model_dump(mode="json")
    raw["limits"]["max_iterations"] += 1
    raw["mcp"]["servers"]["docs"] = {"transport": "http", "url": "https://example.test/mcp"}
    report, candidate = validate_candidate(current, raw, base_revision=config_revision(current))
    assert report.valid and candidate is not None
    assert {(change.path, change.apply_at) for change in report.changes} >= {
        ("limits.max_iterations", "next_step"),
        ("mcp.servers.docs.transport", "reconnect"),
    }
    assert current.mcp.servers == {}


def test_candidate_validation_rejects_stale_and_invalid_values_without_echoing_secrets() -> None:
    current = RuntimeConfig()
    stale, candidate = validate_candidate(current, current.model_dump(mode="json"), base_revision="old")
    assert stale.stale and not stale.valid and candidate is None
    raw = current.model_dump(mode="json")
    raw["compaction"]["auto_ratio"] = 2
    report, candidate = validate_candidate(current, raw, base_revision=config_revision(current))
    assert not report.valid and candidate is None
    assert report.problems[0].path == "compaction.auto_ratio"


def test_settings_validation_and_save_use_one_revision(tmp_path) -> None:
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:
        revision = client.get("/api/settings", headers=HEAD).json()["revision"]
        invalid = client.post(
            "/api/settings/validate",
            headers=HEAD,
            json={"base_revision": revision, "candidate": {"compaction": {"auto_ratio": 2}}},
        ).json()
        assert not invalid["valid"] and invalid["problems"][0]["path"] == "compaction.auto_ratio"
        assert app.config.limits.max_iterations == 0  # the default, untouched by a validation
        assert client.put("/api/settings", headers=HEAD, json={"limits": {"max_iterations": 9}}).status_code == 409
        saved = client.put(
            "/api/settings",
            headers=HEAD,
            json={"base_revision": revision, "limits": {"max_iterations": 9}},
        )
        assert saved.status_code == 200 and app.config.limits.max_iterations == 9
        stale = client.put(
            "/api/settings",
            headers=HEAD,
            json={"base_revision": revision, "limits": {"max_iterations": 10}},
        )
        assert stale.status_code == 409 and app.config.limits.max_iterations == 9


def test_the_terminal_cap_is_set_from_settings_with_no_upper_bound(tmp_path) -> None:
    # The cap on running terminals is raised from Settings. A value past what the machine carries is
    # the operator's call (the page warns), so only a cap below one is refused.
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:
        view = client.get("/api/settings", headers=HEAD).json()
        assert view["terminals"]["running_cap"] == 20
        saved = client.put("/api/settings", headers=HEAD, json={"base_revision": view["revision"], "terminals": {"running_cap": 500}})
        assert saved.status_code == 200 and saved.json()["terminals"]["running_cap"] == 500
        assert app.config.terminals.running_cap == 500
        # The rest of the section is kept: a patch of one field is not a reset of the others.
        assert app.config.terminals.ring_bytes == 8 << 20
        refused = client.put("/api/settings", headers=HEAD, json={"base_revision": saved.json()["revision"], "terminals": {"running_cap": 0}})
        assert refused.status_code == 400 and app.config.terminals.running_cap == 500


@pytest.mark.asyncio
async def test_failed_manager_reload_restores_the_last_working_file(tmp_path) -> None:
    settings = settings_for(tmp_path, native=True)
    application = Application(settings)
    old = application.config
    candidate = old.model_copy(update={"answer_language": "Russian"})

    class Manager:
        def reload_config(self, config: RuntimeConfig) -> None:
            if config.answer_language == "Russian":
                raise RuntimeError("reconnect failed")

    application.manager = Manager()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="reconnect failed"):
        await application.save_config(candidate, expected_revision=config_revision(old))
    assert application.config == old
    assert RuntimeConfig.load(settings.config_path) == old
    with pytest.raises(ConfigConflict):
        await application.save_config(candidate, expected_revision="stale")


def test_the_summary_model_names_a_configured_preset_or_is_refused(tmp_path) -> None:
    # The summariser's preset is chosen in Settings. An id that names no preset is refused with 422
    # and a message the app shows as it is; a preset deleted after it was chosen is kept (the runtime
    # falls back to the session's own model), so a later save of another field still passes.
    app = FakeApp(tmp_path, native=True)
    raw = app.config.model_dump(mode="json")
    raw["presets"] = {"flash": {"provider": "deepseek", "model": "deepseek-flash", "label": "Flash"}}
    app.config = RuntimeConfig.model_validate(raw)
    with TestClient(build_app(app, "tok")) as client:
        view = client.get("/api/settings", headers=HEAD).json()
        assert view["compaction"]["preset"] == ""
        report = client.post("/api/settings/validate", headers=HEAD, json={"base_revision": view["revision"], "candidate": {"compaction": {"preset": "nowhere"}}}).json()
        assert not report["valid"] and report["problems"][0]["path"] == "compaction.preset"
        refused = client.put("/api/settings", headers=HEAD, json={"base_revision": view["revision"], "compaction": {"preset": "nowhere"}})
        assert refused.status_code == 422
        assert "no such model preset 'nowhere'" in refused.json()["detail"]
        assert app.config.compaction.preset == ""

        saved = client.put("/api/settings", headers=HEAD, json={"base_revision": view["revision"], "compaction": {"preset": "flash"}})
        assert saved.status_code == 200 and saved.json()["compaction"]["preset"] == "flash"
        assert app.config.compaction.preset == "flash"

        gone = client.delete("/api/presets/flash", headers=HEAD)
        assert gone.status_code == 200 and gone.json()["compaction"]["preset"] == "flash"
        again = client.put("/api/settings", headers=HEAD, json={"base_revision": gone.json()["revision"], "compaction": {"preset": "flash", "max_words": 900}})
        assert again.status_code == 200 and app.config.compaction.max_words == 900
        cleared = client.put("/api/settings", headers=HEAD, json={"base_revision": again.json()["revision"], "compaction": {"preset": ""}})
        assert cleared.status_code == 200 and app.config.compaction.preset == ""
