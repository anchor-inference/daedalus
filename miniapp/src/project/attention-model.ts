import type { Ask } from "../api";
import type { ProjectTask } from "../board/board";
import type { GoalBudget, MoneyBalance } from "./ProjectBudget";
import { operatorReviewReady } from "./focus";

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

const ACTION_BLOCKERS = new Set([
  "stale_contract", "dependency_not_ready", "result_not_verified", "artifact_missing", "ask_unanswered_or_stale",
]);

/** Unknown server blockers stay visible as an unknown prerequisite instead of looking actionable. */
export function actionBlockerKeys(blockers: string[]): string[] {
  const keys = [...new Set(blockers.map((blocker) => ACTION_BLOCKERS.has(blocker) ? blocker : "unknown"))];
  return (keys.length ? keys : ["unknown"]).map((key) => `focus.attention.blocker.${key}`);
}

/** Collapse only a matching request or the board's current, unbound operator review. */
export function distinctOperatorActions(actions: NextAction[], asks: Ask[], tasks: ProjectTask[]): NextAction[] {
  const current = new Map(tasks.map((task) => [task.id, task]));
  const requests = new Map(asks.filter((ask) => !ask.resolved_at && ask.routed_to === "operator")
    .map((ask) => [ask.id, ask]));
  const seen = new Set<string>();
  return actions.filter((action) => {
    if (action.owner_kind !== "operator" || seen.has(action.action_id)) return false;
    seen.add(action.action_id);
    const ask = action.context_ref ? requests.get(action.context_ref) : null;
    const task = current.get(action.task_id);
    // A task match or a similar title is not a source identity. A stale contract cannot prove one either.
    if (ask && ask.task_id === action.task_id && task?.contract_revision === action.contract_revision
      && (action.kind === "answer_question" || action.kind === "provide_input")) return false;
    // The board's current operator review already opens this task; a blocked or source-bound action
    // may name a different decision, so it keeps its own explanation.
    if (action.kind === "review" && action.enabled && !action.context_ref && task
      && task.contract_revision === action.contract_revision && operatorReviewReady(task)) return false;
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

/** Count each current decision by its source identity so navigation agrees with the inbox. */
export function operatorAttentionCount(asks: Ask[], tasks: ProjectTask[], actions: NextAction[], budget: GoalBudget | null | undefined): number {
  const open = [...new Map(asks.filter((ask) => ask.routed_to === "operator" && !ask.resolved_at)
    .map((ask) => [ask.id, ask])).values()];
  return open.length + tasks.filter(operatorReviewReady).length
    + distinctOperatorActions(actions, open, tasks).length + (budgetAttention(budget) ? 1 : 0);
}
