import { useEffect, useState } from "react";
import { useEvent } from "../events";
import { t } from "../i18n";
import { invalidate, useOffline, useQuery } from "../store";

export type MoneyBalance = {
  limit_usd: string;
  spent_usd: string | null;
  held_usd: string;
  uncertain_usd: string;
  available_usd: string | null;
  state: "known" | "uncertain" | "unknown_usage";
};

export type GoalBudget = {
  configured: boolean;
  project_id: string;
  entity_revision: number;
  goal_revision: number;
  budget_id?: string;
  activated_goal_revision?: number;
  current_goal_revision?: number;
  total?: MoneyBalance;
  coordination?: MoneyBalance;
};

export const budgetKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/budget`;

export function useGoalBudget(projectId: string) {
  const key = budgetKey(projectId);
  const result = useQuery<GoalBudget>(key, { pollMs: 30000, staleMs: 10000 });
  useEvent(["run.finished", "staff.status", "project.changed"], (event) => {
    if (event.project_id === projectId) invalidate(key);
  }, [projectId]);
  return result;
}

function dollars(value: string | null | undefined): string {
  return value === null || value === undefined ? t("budget.unknown") : `$${value}`;
}

export function budgetCompact(budget: GoalBudget | null | undefined): string | null {
  if (!budget?.configured || !budget.total) return null;
  return budget.total.available_usd === null
    ? t("budget.compact.unknown")
    : t("budget.compact.remaining", { amount: dollars(budget.total.available_usd) });
}

function validAmount(value: string): boolean {
  return /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/.test(value);
}

type Write = (method: "PUT", path: string, fields: Record<string, unknown>, label: string,
              onSuccess?: () => void) => Promise<boolean>;

export function ProjectBudget({ projectId, write, canWrite }: {
  projectId: string;
  write: Write;
  canWrite: boolean;
}) {
  const { data, error, refresh } = useGoalBudget(projectId);
  const offline = useOffline();
  const [total, setTotal] = useState("");
  const [coordination, setCoordination] = useState("");
  const [initialized, setInitialized] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (data && !initialized) {
      setTotal(data.total?.limit_usd ?? "");
      setCoordination(data.coordination?.limit_usd ?? "");
      setInitialized(true);
    }
  }, [data, initialized]);
  const valid = validAmount(total) && validAmount(coordination)
    && Number(coordination) <= Number(total);
  const changed = !data?.configured || total !== data.total?.limit_usd
    || coordination !== data.coordination?.limit_usd;

  async function save() {
    if (!data || !initialized || !valid || !changed || saving || offline || !canWrite) return;
    setSaving(true);
    try {
      await write("PUT", budgetKey(projectId), {
        limit_usd: total,
        coordination_limit_usd: coordination,
        expected_goal_revision: data.goal_revision,
      }, t("budget.saved"), () => invalidate(budgetKey(projectId)));
    } finally { setSaving(false); }
  }

  return <details className="project-budget">
    <summary>{t("budget.title")} {data?.configured && <span className="sub">· {budgetCompact(data)}</span>}</summary>
    {error && <div className="sub" role="status">{t("budget.readError")} <button type="button" className="linkbtn" onClick={refresh}>{t("common.retry")}</button></div>}
    {!data && !error && <div className="sub">{t("common.loading")}</div>}
    {data?.configured && data.total && data.coordination && <div className="project-budget-balances">
      <div><b>{t("budget.total")}</b> · {t("budget.observed", { amount: dollars(data.total.spent_usd) })} · {t("budget.held", { amount: dollars(data.total.held_usd) })}</div>
      <div><b>{t("budget.coordination")}</b> · {t("budget.observed", { amount: dollars(data.coordination.spent_usd) })} · {t("budget.held", { amount: dollars(data.coordination.held_usd) })}</div>
      {(data.total.state === "uncertain" || data.coordination.state === "uncertain") && <div className="sub">{t("budget.uncertain", { amount: dollars(data.total.uncertain_usd) })}</div>}
      {(data.total.state === "unknown_usage" || data.coordination.state === "unknown_usage") && <div className="sub">{t("budget.unknownUsage")}</div>}
      <div>{budgetCompact(data)}</div>
    </div>}
    <p className="sub">{t("budget.scope")}</p>
    <div className="project-budget-fields">
      <label htmlFor={`budget-total-${projectId}`}>{t("budget.total")}</label>
      <input id={`budget-total-${projectId}`} className="field" inputMode="decimal" value={total} onChange={(event) => setTotal(event.target.value)} placeholder="1.000000" />
      <label htmlFor={`budget-coordination-${projectId}`}>{t("budget.coordination")}</label>
      <input id={`budget-coordination-${projectId}`} className="field" inputMode="decimal" value={coordination} onChange={(event) => setCoordination(event.target.value)} placeholder="0.500000" />
    </div>
    {data?.configured && <div className="sub">{t("budget.cli")}</div>}
    <button type="button" className="btn small" onClick={() => void save()} disabled={!data || !initialized || !valid || !changed || saving || offline || !canWrite}>{t("budget.save")}</button>
  </details>;
}
