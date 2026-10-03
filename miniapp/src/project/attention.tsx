// Only decisions that can change the project's next action belong here. Progress and diagnostics
// stay with their task, so a phone never has to search a fleet of status cards for one question.

import { useState } from "react";
import type { Ask } from "../api";
import type { ProjectBoardData } from "../board/board";
import { useStreamUp } from "../events";
import { relTime } from "../format";
import { t } from "../i18n";
import { navigate, projectPagePath } from "../router";
import { ProjectSettingsSheet } from "../projects";
import { PageHeader } from "../shell";
import { useOffline, useQuery } from "../store";
import { Skeleton } from "../ui/components";
import { boardKey, useProject } from "./data";
import { budgetAttention, distinctOperatorActions, type NextAction } from "./attention-model";
import { operatorReviewReady } from "./focus";
import { AskAnswers } from "./phone";
import { useGoalBudget } from "./ProjectBudget";

export function AttentionPage({ projectId, back, toast }: { projectId: string; back: string | null; toast: (text: string) => void }) {
  const { project } = useProject(projectId);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const live = useStreamUp();
  const offline = useOffline();
  const asks = useQuery<{ asks: Ask[] }>(`/api/asks?project=${encodeURIComponent(projectId)}&routed_to=operator`, { pollMs: live ? 60000 : 10000, staleMs: 2000 });
  const board = useQuery<ProjectBoardData>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const next = useQuery<{ actions: NextAction[] }>(`/api/projects/${encodeURIComponent(projectId)}/next-actions`, { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const budget = useGoalBudget(projectId);
  const open = [...new Map((asks.data?.asks ?? []).filter((ask) => ask.routed_to === "operator" && !ask.resolved_at)
    .map((ask) => [ask.id, ask])).values()].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const review = (board.data?.tasks ?? []).filter(operatorReviewReady);
  const otherActions = distinctOperatorActions(next.data?.actions ?? [], open, board.data?.tasks ?? []);
  const budgetIssue = budgetAttention(budget.data);
  const titles = new Map((board.data?.tasks ?? []).map((task) => [task.id, task.title]));
  const known = !!asks.data && !!board.data && !!next.data && !!budget.data;
  const uncertain = offline || !!asks.error || !!board.error || !!next.error || !!budget.error;
  const lastConfirmed = Math.min(asks.updatedAt ?? Infinity, board.updatedAt ?? Infinity,
    next.updatedAt ?? Infinity, budget.updatedAt ?? Infinity);
  const refresh = () => { asks.refresh(); board.refresh(); next.refresh(); budget.refresh(); };

  return <>
    <PageHeader title={t("focus.nav.attention")} subtitle={project?.name} back={back ?? undefined} />
    <main className="screen narrow focus-attention">
      {uncertain && <div className="focus-attention-warning" role="status">{t("focus.attention.stale")} {Number.isFinite(lastConfirmed) && t("focus.attention.lastConfirmed", { time: relTime(new Date(lastConfirmed).toISOString()) })} <button type="button" className="linkbtn" onClick={refresh}>{t("common.retry")}</button></div>}
      {!known && !asks.error && !board.error && !next.error && !budget.error && <Skeleton rows={3} />}
      {open.map((ask) => <section className="focus-attention-item" key={ask.id} aria-label={ask.heading || ask.title || ask.text}>
        <div className="focus-attention-kind">{t(ask.kind === "permission" || ask.kind === "folder" ? "focus.attention.permission" : "focus.attention.question")} · {relTime(ask.created_at)}</div>
        <h2>{ask.heading || ask.title || ask.text}</h2>
        {(ask.heading || ask.title) && <p>{ask.text}</p>}
        {ask.task_id && <button type="button" className="btn small ghost" onClick={() => navigate(projectPagePath(projectId, "board", { task: ask.task_id }))}>{t("focus.attention.task")}</button>}
        <AskAnswers ask={ask} projectId={projectId} toast={toast} unverified={uncertain} />
      </section>)}
      {review.map((task) => <section className="focus-attention-item" key={task.id}>
        <div className="focus-attention-kind">{t("focus.attention.review")}</div>
        <h2>{task.title}</h2>
        <p>{task.acceptance || t("focus.attention.reviewHint")}</p>
        <button type="button" className="btn primary" onClick={() => navigate(projectPagePath(projectId, "board", { task: task.id }))}>{t("focus.attention.inspect")}</button>
      </section>)}
      {otherActions.map((action) => <section className="focus-attention-item" key={action.action_id}>
        <div className="focus-attention-kind">{t(`focus.attention.action.${action.kind}`)}</div>
        <h2>{titles.get(action.task_id) || t("focus.attention.task")}</h2>
        {!action.enabled && <p className="sub">{t("focus.attention.prerequisite")}</p>}
        <button type="button" className="btn small" onClick={() => navigate(projectPagePath(projectId, "board", { task: action.task_id }))}>{t("focus.attention.inspect")}</button>
      </section>)}
      {budgetIssue && <section className="focus-attention-item">
        <div className="focus-attention-kind">{t("focus.attention.budget")}</div>
        <h2>{t(`focus.attention.budget.${budgetIssue.reason}`)}</h2>
        <p>{t("focus.attention.budget.scope", { scope: budgetIssue.scopes.map((scope) => t(`budget.${scope}`)).join(" · ") })}</p>
        <p className="sub">{t("focus.attention.budget.notReceipt")}</p>
        {project && <button type="button" className="btn small" onClick={() => setSettingsOpen(true)}>{t("focus.attention.budget.inspect")}</button>}
      </section>}
      {known && !uncertain && open.length === 0 && review.length === 0 && otherActions.length === 0 && !budgetIssue && <div className="empty"><b>{t("focus.attention.empty")}</b></div>}
      {!known && (asks.error || board.error || next.error || budget.error) && <button type="button" className="btn" onClick={refresh}>{t("common.retry")}</button>}
      {settingsOpen && project && <ProjectSettingsSheet project={project} onClose={() => setSettingsOpen(false)} onRemoved={() => setSettingsOpen(false)} toast={toast} />}
    </main>
  </>;
}
