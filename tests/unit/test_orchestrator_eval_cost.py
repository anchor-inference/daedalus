"""Replay costs distinguish measured spend, missing prices and subscription access."""

from types import SimpleNamespace

from tests.orchestrator_eval.run import Episode, Model, summary


def test_replay_cost_does_not_present_subscription_or_missing_price_as_free() -> None:
    subscription = Model("codex", "example", 1.0)
    unpriced = Model("other", "example", 1.0)
    priced = Model("deepseek", "deepseek-flash", 1.0)

    assert subscription.cost({"cost": 0, "prompt_tokens": 100}) == (None, "subscription")
    assert unpriced.cost({"prompt_tokens": 100, "completion_tokens": 10}) == (None, "unpriced")
    assert priced.cost({"cost": 0}) == (0.0, "provider_reported")
    assert priced.cost({"prompt_tokens": 100, "completion_tokens": 10}) == (0.000042, "local_estimate")
    assert Episode("case", unpriced.label, 1).cost is None


def test_replay_summary_keeps_unknown_and_subscription_costs_visible() -> None:
    subscription = Model("codex", "example", 1.0)
    unpriced = Model("other", "example", 1.0)
    priced = Model("deepseek", "deepseek-flash", 1.0)
    cases = [SimpleNamespace(id="case", counterexample=False)]
    results = [
        Episode("case", subscription.label, 1, cost=None, cost_basis="subscription"),
        Episode("case", unpriced.label, 1, cost=None, cost_basis="unpriced"),
        Episode("case", priced.label, 1, cost=0.001, cost_basis="local_estimate"),
    ]

    report = summary(results, [subscription, unpriced, priced], cases)
    rows = {model.label: next(row for row in report.splitlines() if row.startswith(f"| {model.label} |"))
            for model in (subscription, unpriced, priced)}
    assert "SUBSCRIPTION (per-replay USD unavailable)" in rows[subscription.label]
    assert "UNKNOWN" in rows[unpriced.label]
    assert "$0.001 (local estimate)" in rows[priced.label]
