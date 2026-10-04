import json

import pytest

from daedalus.stores.attempt_diagnostics import attempt_diagnostics, support_bundle


def test_bundle_omits_raw_fault_material_and_other_attempts():
    secret = "sk-private-token"
    path = "private-folder/secret-workspace"
    attempt = {"id": "attempt_1", "task_id": "task_1", "state": "failed",
               "fence_token_hash": secret, "worktree_path": path}
    faults = [
        {"attempt_id": "attempt_2", "kind": "adapter_error", "diagnostic_ref": "other"},
        {"attempt_id": "attempt_1", "kind": "adapter_error", "id": 1, "diagnostic_ref": "fault_1",
         "stderr": f"{secret} at {path}", "exception": f"{secret} at {path}",
         "created_at": "2026-10-04T10:00:00Z"},
        {"attempt_id": "attempt_1", "kind": f"renderer_error:{secret}",
         "diagnostic_ref": path, "created_at": path},
    ]
    data = support_bundle(attempt, faults)
    assert secret.encode() not in data
    assert path.encode() not in data
    assert b"other" not in data
    assert json.loads(data) == {
        "attempt_id": "attempt_1", "task_id": "task_1", "state": "failed", "redacted": True,
        "redaction_version": 1, "truncated": False,
        "faults": [{"kind": "adapter_error", "diagnostic_ref": "fault-1",
                    "created_at": "2026-10-04T10:00:00Z"}, {"kind": "runtime_error"}],
    }


def test_cancelled_is_a_normal_outcome_and_faults_are_bounded():
    attempt = {"id": "attempt_1", "task_id": "task_1", "state": "cancelled"}
    faults = [{"attempt_id": "attempt_1", "kind": "cancelled", "cancelled_by": "operator"}] * 30
    projection = attempt_diagnostics(attempt, faults)
    assert projection["state"] == "cancelled"
    assert projection["faults"] == [{"kind": "cancelled", "cancelled_by": "operator"}] * 20
    assert projection["truncated"] is True


def test_rejects_unbounded_identity():
    with pytest.raises(ValueError):
        attempt_diagnostics({"id": "../secret", "task_id": "task_1"}, [])
