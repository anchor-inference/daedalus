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
  coordinator_quote?: { model: string; reserve_usd: string; input_bound: number; output_bound: number } | null;
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

function micros(value: string): bigint | null {
  if (!validAmount(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  return BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"));
}

type Write = (method: "PUT", path: string, fields: Record<string, unknown>, label: string,
              onSuccess?: () => void) => Promise<boolean>;

type BudgetSource = { goalRevision: number; total: string | null; coordination: string | null };
type BudgetDraft = { version: 1; total: string; coordination: string; source: BudgetSource };
type ConfirmedWrite = { path: string; body: Record<string, unknown> };

function sourceOf(data: GoalBudget): BudgetSource {
  return { goalRevision: data.goal_revision, total: data.total?.limit_usd ?? null,
    coordination: data.coordination?.limit_usd ?? null };
}

function storedDraft(projectId: string): BudgetDraft | null {
  try {
    const value = JSON.parse(localStorage.getItem(`daedalus.project.budget.draft.${projectId}`) ?? "null");
    return value?.version === 1 && typeof value?.total === "string" && value.total.length <= 32
      && typeof value?.coordination === "string" && value.coordination.length <= 32
      && Number.isInteger(value?.source?.goalRevision)
      && (value.source.total === null || (typeof value.source.total === "string" && value.source.total.length <= 32))
      && (value.source.coordination === null || (typeof value.source.coordination === "string" && value.source.coordination.length <= 32))
      ? value as BudgetDraft : null;
  } catch { return null; }
}

export function ProjectBudget({ projectId, write, canWrite, confirmed }: {
  projectId: string;
  write: Write;
  canWrite: boolean;
  confirmed?: ConfirmedWrite | null;
}) {
  const { data, error, refresh } = useGoalBudget(projectId);
  const offline = useOffline();
  const [draft, setDraft] = useState<BudgetDraft | null>(() => storedDraft(projectId));
  const [saving, setSaving] = useState(false);
  const key = `daedalus.project.budget.draft.${projectId}`;
  const total = draft?.total ?? data?.total?.limit_usd ?? "";
  const coordination = draft?.coordination ?? data?.coordination?.limit_usd ?? "";
  const source = data ? sourceOf(data) : null;
  const sourceChanged = !!draft && !!source && (draft.source.goalRevision !== source.goalRevision
    || draft.source.total !== source.total || draft.source.coordination !== source.coordination);

  function remember(next: BudgetDraft | null) {
    setDraft(next);
    try {
      if (next) localStorage.setItem(key, JSON.stringify(next));
      else localStorage.removeItem(key);
    } catch { /* This mounted form still retains the typed values. */ }
  }

  function edit(field: "total" | "coordination", value: string) {
    if (!source) return;
    remember({ version: 1, total: field === "total" ? value : total,
      coordination: field === "coordination" ? value : coordination,
      source: draft?.source ?? source });
  }

  useEffect(() => {
    if (confirmed?.path !== budgetKey(projectId) || !draft) return;
    if (confirmed.body.limit_usd === draft.total && confirmed.body.coordination_limit_usd === draft.coordination)
      remember(null);
  }, [confirmed, draft, projectId]);
  const valid = validAmount(total) && validAmount(coordination)
    && Number(coordination) <= Number(total);
  const changed = !data?.configured || total !== data.total?.limit_usd
    || coordination !== data.coordination?.limit_usd;
  const quoted = data?.coordinator_quote;
  const quoteAmount = quoted ? micros(quoted.reserve_usd) : null;
  const belowQuote = quoteAmount !== null && ((micros(total) !== null && micros(total)! < quoteAmount)
    || (micros(coordination) !== null && micros(coordination)! < quoteAmount));

  async function save() {
    if (!data || !valid || !changed || sourceChanged || saving || offline || !canWrite) return;
    setSaving(true);
    try {
      await write("PUT", budgetKey(projectId), {
        limit_usd: total,
        coordination_limit_usd: coordination,
        expected_goal_revision: data.goal_revision,
      }, t("budget.saved"));
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
    {quoted && <p className={belowQuote ? "sub attn" : "sub"} role={belowQuote ? "status" : undefined}>
      {t("budget.coordinatorQuote", { model: quoted.model, amount: dollars(quoted.reserve_usd) })}
      {belowQuote && <> {t("budget.belowQuote")}</>}
    </p>}
    {sourceChanged && <p className="sub attn" role="status">{t("budget.draft.sourceChanged")}{" "}
      <button type="button" className="linkbtn" disabled={!data || offline} onClick={() => source && draft && remember({ ...draft, source })}>{t("budget.draft.review")}</button>
    </p>}
    <div className="project-budget-fields">
      <label htmlFor={`budget-total-${projectId}`}>{t("budget.total")}</label>
      <input id={`budget-total-${projectId}`} className="field" inputMode="decimal" maxLength={32} value={total} disabled={!data} onChange={(event) => edit("total", event.target.value)} placeholder="1.000000" />
      <label htmlFor={`budget-coordination-${projectId}`}>{t("budget.coordination")}</label>
      <input id={`budget-coordination-${projectId}`} className="field" inputMode="decimal" maxLength={32} value={coordination} disabled={!data} onChange={(event) => edit("coordination", event.target.value)} placeholder="0.500000" />
    </div>
    {draft && <button type="button" className="linkbtn" onClick={() => remember(null)}>{t("budget.draft.discard")}</button>}
    {data?.configured && <div className="sub">{t("budget.cli")}</div>}
    <button type="button" className="btn small" onClick={() => void save()} disabled={!data || !valid || !changed || sourceChanged || saving || offline || !canWrite}>{t("budget.save")}</button>
  </details>;
}
