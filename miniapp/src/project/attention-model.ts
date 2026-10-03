import type { Ask } from "../api";
import type { ProjectTask } from "../board/board";
import type { GoalBudget, MoneyBalance } from "./ProjectBudget";

export type NextAction = {
  action_id: string;
  task_id: string;
  contract_revision: number;
  kind: string;
  owner_kind: string;
  context_ref: string | null;
  enabled: boolean;
  blockers: string[];
};

/** Only an exact request reference can prove that two projections name one decision. */
export function distinctOperatorActions(actions: NextAction[], asks: Ask[], tasks: ProjectTask[]): NextAction[] {
  const current = new Map(tasks.map((task) => [task.id, task.contract_revision]));
  const requests = new Map(asks.filter((ask) => !ask.resolved_at && ask.routed_to === "operator")
    .map((ask) => [ask.id, ask]));
  const seen = new Set<string>();
  return actions.filter((action) => {
    if (action.owner_kind !== "operator" || seen.has(action.action_id)) return false;
    seen.add(action.action_id);
    const ask = action.context_ref ? requests.get(action.context_ref) : null;
    // A task match or a similar title is not a source identity. A stale contract cannot prove one either.
    if (ask && ask.task_id === action.task_id && current.get(action.task_id) === action.contract_revision
      && (action.kind === "answer_question" || action.kind === "provide_input")) return false;
    return true;
  });
}

export type BudgetAttention = { scopes: ("total" | "coordination")[]; reason: "exhausted" | "unknown" };

function issue(balance: MoneyBalance | undefined): BudgetAttention["reason"] | null {
  if (!balance) return null;
  if (balance.state === "unknown_usage" || balance.available_usd === null) return "unknown";
  if (/^0(?:\.0+)?$/.test(balance.available_usd))
    return balance.state === "uncertain" ? "unknown" : "exhausted";
  return null;
}

/** A balance is a current limit, not a receipt proving that a particular action was denied. */
export function budgetAttention(budget: GoalBudget | null | undefined): BudgetAttention | null {
  if (!budget?.configured) return null;
  const total = issue(budget.total);
  const coordination = issue(budget.coordination);
  if (!total && !coordination) return null;
  return { scopes: [...(total ? ["total" as const] : []), ...(coordination ? ["coordination" as const] : [])],
    reason: total === "unknown" || coordination === "unknown" ? "unknown" : "exhausted" };
}
