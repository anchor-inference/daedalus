"""An evaluation cannot turn missing provider billing evidence into free usage."""

import json
from dataclasses import asdict

import pytest

from tests.browser_eval import run as browser
from tests.orchestrator_eval import run as orchestrator


@pytest.mark.parametrize("evaluation", [orchestrator, browser])
def test_unpriced_call_is_unknown_but_reported_zero_is_free(evaluation: object) -> None:
    model = evaluation.Model("unlisted", "new-model", 1.0)
    assert model.cost({"prompt_tokens": 5, "completion_tokens": 2}) == (None, "unpriced")
    assert model.cost({"cost": 0, "prompt_tokens": 5, "completion_tokens": 2}) == (0.0, "provider_reported")
    priced = evaluation.Model("deepseek", "deepseek-flash", 1.0)
    assert priced.cost({}) == (None, "unpriced")
    assert priced.cost({"prompt_tokens": 0, "completion_tokens": 0}) == (0.0, "local_estimate")
    subscription = evaluation.Model("codex", "licensed", 1.0)
    assert subscription.cost({}) == (None, "subscription")


def test_orchestrator_report_and_json_keep_unknown_after_known_cost() -> None:
    model = orchestrator.Model("unlisted", "new-model", 1.0)
    result = orchestrator.Episode("sample", model.label, 1, cost=None)
    subscription = orchestrator.Episode("sample", "codex:licensed", 1, cost=None, cost_basis="subscription")
    report = orchestrator.summary([result, subscription], [model, orchestrator.Model("codex", "licensed", 1.0)], [])
    assert "| unlisted:new-model |" in report and "| UNKNOWN |" in report
    assert "| codex:licensed |" in report and "SUBSCRIPTION (per-replay USD unavailable)" in report
    assert json.loads(json.dumps(asdict(result)))["cost"] is None


def test_browser_report_and_json_keep_unknown_after_known_cost() -> None:
    model = browser.Model("unlisted", "new-model", 1.0)
    result = browser.Episode("sample", model.label, 1, cost=None)
    subscription = browser.Episode("sample", "codex:licensed", 1, cost=None, cost_basis="subscription")
    report = browser.summary([result, subscription], [model, browser.Model("codex", "licensed", 1.0)], [], "now")
    assert "| unlisted:new-model |" in report and "| UNKNOWN |" in report
    assert "| codex:licensed |" in report and "SUBSCRIPTION (per-replay USD unavailable)" in report
    assert json.loads(json.dumps({"spend": {model.label: None}, "episodes": [asdict(result)]}))["episodes"][0]["cost"] is None


@pytest.mark.parametrize("evaluation", [orchestrator, browser])
def test_a_later_priced_response_cannot_erase_unpriced_spend(evaluation: object) -> None:
    episode = evaluation.Episode("sample", "unlisted:new-model", 1)
    episode.record_cost(0.001, "provider_reported")
    episode.record_cost(None, "unpriced")
    episode.record_cost(0.002, "provider_reported")
    assert episode.model_calls == 3
    assert episode.cost is None and episode.cost_basis == "unpriced"


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation", [orchestrator, browser])
async def test_unknown_cost_stops_another_paid_request(evaluation: object) -> None:
    model = evaluation.Model("unlisted", "new-model", 1.0, unknown_cost=True)
    with pytest.raises(evaluation.OutOfBudget, match="unknown cost"):
        await evaluation.complete(None, model, [], [])


@pytest.mark.parametrize("evaluation", [orchestrator, browser])
def test_cost_per_checked_pass_includes_failed_spend(evaluation: object) -> None:
    model = evaluation.Model("deepseek", "deepseek-flash", 1.0)
    episodes = [
        evaluation.Episode("sample", model.label, 1, success=True, cost=0.001, cost_basis="provider_reported"),
        evaluation.Episode("sample", model.label, 2, success=False, cost=0.002, cost_basis="provider_reported"),
        evaluation.Episode("sample", model.label, 3, success=False, cost=0.003, cost_basis="provider_reported"),
    ]
    report = (evaluation.summary(episodes, [model], [], "now") if evaluation is browser
              else evaluation.summary(episodes, [model], []))
    row = next(line for line in report.splitlines() if line.startswith(f"| {model.label} |"))
    assert "cost / checked pass" in report
    assert "1/3" in row and "| $0.006 (provider reported) | $0.006 (provider reported) |" in row
    assert "failed episodes" in report and "not an accepted task result" in report


@pytest.mark.parametrize("evaluation", [orchestrator, browser])
def test_cost_per_checked_pass_keeps_unknown_and_zero_passes_distinct(evaluation: object) -> None:
    model = evaluation.Model("deepseek", "deepseek-flash", 1.0)
    checked = [evaluation.Episode("sample", model.label, 1, success=True, cost=0.001, cost_basis="local_estimate"),
               evaluation.Episode("sample", model.label, 2, success=False, cost=None, cost_basis="unpriced")]
    failed = [evaluation.Episode("sample", model.label, 1, success=False, cost=0.001, cost_basis="local_estimate")]
    def render(episodes: list[object]) -> str:
        return (evaluation.summary(episodes, [model], [], "now") if evaluation is browser
                else evaluation.summary(episodes, [model], []))
    unknown_row = next(line for line in render(checked).splitlines() if line.startswith(f"| {model.label} |"))
    zero_row = next(line for line in render(failed).splitlines() if line.startswith(f"| {model.label} |"))
    assert "| UNKNOWN | UNKNOWN |" in unknown_row
    assert "| $0.001 (local estimate) | undefined (0 passes) |" in zero_row


@pytest.mark.parametrize("evaluation", [orchestrator, browser])
def test_cost_per_checked_pass_does_not_price_subscription_or_reported_zero(evaluation: object) -> None:
    subscription = evaluation.Model("codex", "licensed", 1.0)
    priced = evaluation.Model("deepseek", "deepseek-flash", 1.0)
    episodes = [evaluation.Episode("sample", subscription.label, 1, success=True, cost=None, cost_basis="subscription"),
                evaluation.Episode("sample", priced.label, 1, success=True, cost=0, cost_basis="provider_reported")]
    report = (evaluation.summary(episodes, [subscription, priced], [], "now") if evaluation is browser
              else evaluation.summary(episodes, [subscription, priced], []))
    subscription_row = next(line for line in report.splitlines() if line.startswith(f"| {subscription.label} |"))
    priced_row = next(line for line in report.splitlines() if line.startswith(f"| {priced.label} |"))
    assert subscription_row.count("SUBSCRIPTION (per-replay USD unavailable)") == 2
    assert "| $0.000 (provider reported) | $0.000 (provider reported) |" in priced_row
