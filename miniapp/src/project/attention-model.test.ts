import { describe, expect, it } from "vitest";
import type { Ask } from "../api";
import type { ProjectTask } from "../board/board";
import { actionBlockerKeys, budgetAttention, distinctOperatorActions, type NextAction } from "./attention-model";
import type { GoalBudget, MoneyBalance } from "./ProjectBudget";

const task = { id: "task-one", contract_revision: 4 } as ProjectTask;
const ask = { id: "ask-one", task_id: task.id, routed_to: "operator", resolved_at: null } as Ask;
const action = (id: string, kind: string, ref: string | null, revision = 4): NextAction => ({
  action_id: id, task_id: task.id, contract_revision: revision, kind, owner_kind: "operator",
  context_ref: ref, enabled: true, blockers: [],
});

describe("attention source identity", () => {
  it("keeps separate decisions on one task and collapses only the exact current request", () => {
    const actions = [action("same-ask", "answer_question", ask.id), action("retry", "retry", ask.id),
      action("missing-ref", "provide_input", null), action("stale-contract", "answer_question", ask.id, 3),
      action("same-ask", "answer_question", ask.id)];
    expect(distinctOperatorActions(actions, [ask], [task]).map((row) => row.action_id))
      .toEqual(["retry", "missing-ref", "stale-contract"]);
    expect(distinctOperatorActions(actions, [{ ...ask, resolved_at: "2026-01-01" }], [task])
      .map((row) => row.action_id)).toEqual(["same-ask", "retry", "missing-ref", "stale-contract"]);
  });

  it("does not treat a matching request ID on a different task as the same source", () => {
    expect(distinctOperatorActions([action("action-one", "answer_question", ask.id)],
      [{ ...ask, task_id: "another-task" }], [task])).toHaveLength(1);
  });
});

describe("blocked actions", () => {
  it("names distinct known blockers and keeps unknown or missing reasons explicit", () => {
    expect(actionBlockerKeys(["dependency_not_ready", "stale_contract", "dependency_not_ready"]))
      .toEqual(["focus.attention.blocker.dependency_not_ready", "focus.attention.blocker.stale_contract"]);
    expect(actionBlockerKeys(["new_server_reason"])).toEqual(["focus.attention.blocker.unknown"]);
    expect(actionBlockerKeys([])).toEqual(["focus.attention.blocker.unknown"]);
  });
});

const balance = (state: MoneyBalance["state"], available: string | null): MoneyBalance => ({
  limit_usd: "5.000000", spent_usd: state === "unknown_usage" ? null : "0.000000",
  held_usd: "0.000000", uncertain_usd: state === "uncertain" ? "1.000000" : "0.000000",
  available_usd: available, state,
});

describe("project budget attention", () => {
  it("shows only confirmed exhaustion or an unknown balance and never invents a denied call", () => {
    const budget = { configured: true, total: balance("known", "0.000000"),
      coordination: balance("known", "2.000000") } as GoalBudget;
    expect(budgetAttention(budget)).toEqual({ scopes: ["total"], reason: "exhausted" });
    expect(budgetAttention({ ...budget, total: balance("known", "3.000000") })).toBeNull();
    expect(budgetAttention({ ...budget, total: balance("unknown_usage", null) }))
      .toEqual({ scopes: ["total"], reason: "unknown" });
    expect(budgetAttention({ ...budget, total: balance("uncertain", "2.000000") })).toBeNull();
    expect(budgetAttention({ ...budget, total: balance("uncertain", "0.000000") }))
      .toEqual({ scopes: ["total"], reason: "unknown" });
    expect(budgetAttention({ configured: false } as GoalBudget)).toBeNull();
  });
});
