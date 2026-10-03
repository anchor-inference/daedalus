"""An evaluation cannot turn missing provider billing evidence into free usage."""

import json
from dataclasses import asdict

import pytest

from tests.browser_eval import run as browser
from tests.orchestrator_eval import run as orchestrator


@pytest.mark.parametrize("evaluation", [orchestrator, browser])
def test_unpriced_call_is_unknown_but_reported_zero_is_free(evaluation: object) -> None:
    model = evaluation.Model("unlisted", "new-model", 1.0)
    assert model.cost({"prompt_tokens": 5, "completion_tokens": 2}) is None
    assert model.cost({"cost": 0, "prompt_tokens": 5, "completion_tokens": 2}) == 0.0
    priced = evaluation.Model("deepseek", "deepseek-flash", 1.0)
    assert priced.cost({}) is None
    assert priced.cost({"prompt_tokens": 0, "completion_tokens": 0}) == 0.0
    subscription = evaluation.Model("codex", "licensed", 1.0)
    assert subscription.cost({}) == 0.0


def test_orchestrator_report_and_json_keep_unknown_after_known_cost() -> None:
    model = orchestrator.Model("unlisted", "new-model", 1.0)
    result = orchestrator.Episode("sample", model.label, 1, cost=None)
    zero = orchestrator.Episode("sample", "codex:licensed", 1, cost=0.0)
    report = orchestrator.summary([result, zero], [model, orchestrator.Model("codex", "licensed", 1.0)], [])
    assert "| unlisted:new-model |" in report and "| UNKNOWN |" in report
    assert "| codex:licensed |" in report and "| 0.000 |" in report
    assert json.loads(json.dumps(asdict(result)))["cost"] is None


def test_browser_report_and_json_keep_unknown_after_known_cost() -> None:
    model = browser.Model("unlisted", "new-model", 1.0)
    result = browser.Episode("sample", model.label, 1, cost=None)
    zero = browser.Episode("sample", "codex:licensed", 1, cost=0.0)
    report = browser.summary([result, zero], [model, browser.Model("codex", "licensed", 1.0)], [], "now")
    assert "| unlisted:new-model |" in report and "| UNKNOWN |" in report
    assert "| codex:licensed |" in report and "| $0.000 |" in report
    assert json.loads(json.dumps({"spend": {model.label: None}, "episodes": [asdict(result)]}))["episodes"][0]["cost"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation", [orchestrator, browser])
async def test_unknown_cost_stops_another_paid_request(evaluation: object) -> None:
    model = evaluation.Model("unlisted", "new-model", 1.0, unknown_cost=True)
    with pytest.raises(evaluation.OutOfBudget, match="unknown cost"):
        await evaluation.complete(None, model, [], [])
