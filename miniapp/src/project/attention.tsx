// Only decisions that can change the project's next action belong here. Progress and diagnostics
// stay with their task, so a phone never has to search a fleet of status cards for one question.

import { type ReactNode, useState } from "react";
import type { Ask } from "../api";
import type { ProjectBoardData, ProjectTask } from "../board/board";
import { Icon } from "../icons";
import { askWords } from "../staff/model";
import { StaffAvatar } from "../team/parts";
import type { Staff } from "../team/team";
import { Banner, EmptyState, IconButton, ListRow, SectionHeader, TopBar } from "../ui/phone";
import { DecisionSheet } from "./needs";
import { useStreamUp } from "../events";
import { clock, relTime } from "../format";
import { plural, t } from "../i18n";
import { back as goBack, navigate, projectPagePath } from "../router";
import { ProjectSettingsSheet } from "../projects";
import { PageHeader } from "../shell";
import { useOffline, useQuery } from "../store";
import { Skeleton } from "../ui/components";
import { boardKey, staffKey, useProject } from "./data";
import { actionBlockerKeys, budgetAttention, distinctOperatorActions, type BudgetAttention, type NextAction } from "./attention-model";
import { operatorReviewReady } from "./focus";
import { AskAnswers } from "./phone";
import { useGoalBudget } from "./ProjectBudget";

export function AttentionPage({ projectId, back, toast, phone = false }: { projectId: string; back: string | null; toast: (text: string) => void; phone?: boolean }) {
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
  if (phone) return <PhoneAttention projectId={projectId} name={project?.name ?? ""} back={back} toast={toast} open={open} review={review} actions={otherActions}
    budgetIssue={budgetIssue} titles={titles} known={known} uncertain={uncertain} failed={!!(asks.error || board.error || next.error || budget.error)}
    lastConfirmed={lastConfirmed} refresh={refresh} onBudget={project ? () => setSettingsOpen(true) : undefined}
    settings={settingsOpen && project ? <ProjectSettingsSheet project={project} onClose={() => setSettingsOpen(false)} onRemoved={() => setSettingsOpen(false)} toast={toast} /> : null} />;

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
        <div className="focus-attention-kind">{t(action.enabled ? `focus.attention.action.${action.kind}` : "focus.attention.waiting")}</div>
        <h2>{titles.get(action.task_id) || t("focus.attention.task")}</h2>
        {!action.enabled && <p className="sub">{t("focus.attention.blocked", {
          reason: actionBlockerKeys(action.blockers).map((key) => t(key)).join(" · "),
        })}</p>}
        <button type="button" className="btn small" onClick={() => navigate(projectPagePath(projectId, "board", { task: action.task_id }))}>{t("focus.attention.task")}</button>
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

type PhoneAttentionProps = {
  projectId: string; name: string; back: string | null; toast: (text: string) => void;
  open: Ask[]; review: ProjectTask[]; actions: NextAction[]; budgetIssue: BudgetAttention | null; titles: Map<string, string>;
  known: boolean; uncertain: boolean; failed: boolean; lastConfirmed: number; refresh: () => void; onBudget?: () => void; settings: ReactNode;
};

/**
 * Needs decision on a phone, as the design draws it: the requests to answer as rows that open the one
 * decision sheet, the results to review as cards with the way into them, and what is blocked with what
 * it waits for. The answers themselves are not repeated here: they live in the sheet, so a request is
 * answered in one place whichever page found it.
 */
function PhoneAttention({ projectId, name, back, toast, open, review, actions, budgetIssue, titles, known, uncertain, failed, lastConfirmed, refresh, onBudget, settings }: PhoneAttentionProps) {
  const [sheet, setSheet] = useState(false);
  const { data: team } = useQuery<{ staff: Staff[] }>(staffKey(projectId), { staleMs: 5000 });
  const names = new Map((team?.staff ?? []).map((m) => [m.id, m]));
  const total = open.length + review.length + actions.length + (budgetIssue ? 1 : 0);
  const checked = Number.isFinite(lastConfirmed) ? t("pattn.checked", { time: clock(lastConfirmed) }) : "";
  const sub = [name, known && !uncertain && total > 0 ? plural("pattn.waiting", total) : checked].filter(Boolean).join(" · ");
  const empty = known && !uncertain && total === 0;
  return <div className="ph-page ph-attention">
    <TopBar title={t("focus.nav.attention")} sub={sub} back={back ? () => goBack(back) : undefined}
      actions={<IconButton icon="reload" label={t("pattn.refresh")} onClick={refresh} />} />
    <div className="ph-page-body list">
      {uncertain && <div className="ph-page-pad"><Banner tone="warn" icon="alert" className="focus-attention-warning" sub={checked || undefined} action={<button type="button" className="ph-btn sm" onClick={refresh}>{t("common.retry")}</button>}>{t("focus.attention.stale")}</Banner></div>}
      {!known && !failed && [0, 1, 2].map((i) => <div key={i} className="ph-skrow"><span className="ph-sk" style={{ width: 44, height: 44, borderRadius: "50%" }} /><span className="grow"><span className="ph-sk" style={{ height: 14, width: `${60 - i * 10}%` }} /><span className="ph-sk" style={{ height: 11, width: "45%", marginTop: 8 }} /></span></div>)}
      {!known && failed && <EmptyState icon="alert" tone="bad" title={t("pattn.error")} body={t("focus.attention.stale")} action={<button type="button" className="ph-btn primary" onClick={refresh}>{t("common.retry")}</button>} />}
      {open.length > 0 && <>
        <SectionHeader tone="warn" count={open.length}>{t("pattn.now")}</SectionHeader>
        {open.map((ask) => {
          const permission = ask.kind === "permission" || ask.kind === "folder";
          const who = ask.staff_id ? names.get(ask.staff_id)?.name : undefined;
          const heading = ask.heading || ask.title || ask.text;
          return <ListRow key={ask.id} className="focus-attention-item" data={{ ask: ask.short_id, kind: "ask" }}
            lead={<span className="ph-avatar warn" aria-hidden><Icon name={permission ? "shield" : "ask"} size={22} /></span>}
            title={permission ? t("pattn.permission") : t("pattn.question", { title: heading })}
            meta={[who ?? t("pattn.orchestrator"), permission ? askWords(ask) : ask.heading || ask.title ? ask.text : ""].filter(Boolean).join(" · ")}
            trail={<span className="ph-row-time">{relTime(ask.created_at)}</span>}
            onOpen={() => setSheet(true)} />;
        })}
      </>}
      {review.length > 0 && <>
        <SectionHeader count={review.length}>{t("pattn.review")}</SectionHeader>
        {review.map((task) => {
          const done = task.checklist.filter((c) => c.done).length;
          return <section className="ph-card focus-attention-item" key={task.id} data-task={task.id} data-kind="review">
            <div className="ph-card-head">
              {task.assignee && <StaffAvatar name={task.assignee.name} color={task.assignee.color} size="small" />}
              <b className="truncate">{task.title}</b>
              {task.checklist.length > 0 && <span className="ph-pill">{done}/{task.checklist.length}</span>}
            </div>
            <p>{task.acceptance || t("focus.attention.reviewHint")}</p>
            <button type="button" className="ph-btn primary" onClick={() => navigate(projectPagePath(projectId, "board", { task: task.id }))}>{t("pattn.inspect")}</button>
          </section>;
        })}
      </>}
      {(actions.length > 0 || budgetIssue) && <>
        <SectionHeader count={actions.length + (budgetIssue ? 1 : 0)}>{t("pattn.blocked")}</SectionHeader>
        {actions.map((action) => <section className="ph-card focus-attention-item" key={action.action_id} data-kind="action">
          <div className="ph-card-head"><b className="truncate">{titles.get(action.task_id) || t("focus.attention.task")}</b></div>
          <p>{action.enabled ? t(`focus.attention.action.${action.kind}`)
            : <><span className="waiting">{t("pattn.waitsFor")}</span> {actionBlockerKeys(action.blockers).map((key) => t(key)).join(" · ")}</>}</p>
          <button type="button" className="ph-btn" onClick={() => navigate(projectPagePath(projectId, "board", { task: action.task_id }))}>{t("focus.attention.task")}</button>
        </section>)}
        {budgetIssue && <section className="ph-card focus-attention-item" data-kind="budget">
          <div className="ph-card-head"><b>{t(`focus.attention.budget.${budgetIssue.reason}`)}</b></div>
          <p><span className="waiting">{t("pattn.waitsFor")}</span> {t("focus.attention.budget.scope", { scope: budgetIssue.scopes.map((scope) => t(`budget.${scope}`)).join(" · ") })}</p>
          {onBudget && <button type="button" className="ph-btn" onClick={onBudget}>{t("focus.attention.budget.inspect")}</button>}
        </section>}
      </>}
      {empty && <EmptyState icon="check" tone="ok" title={t("focus.attention.empty")} body={t("pattn.empty.sub")} />}
    </div>
    {sheet && <DecisionSheet projectId={projectId} toast={toast} onClose={() => setSheet(false)} />}
    {settings}
  </div>;
}
