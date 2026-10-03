// Only decisions that can change the project's next action belong here. Progress and diagnostics
// stay with their task, so a phone never has to search a fleet of status cards for one question.

import type { Ask } from "../api";
import type { ProjectBoardData } from "../board/board";
import { useStreamUp } from "../events";
import { relTime } from "../format";
import { t } from "../i18n";
import { navigate, projectPagePath } from "../router";
import { PageHeader } from "../shell";
import { useOffline, useQuery } from "../store";
import { Skeleton } from "../ui/components";
import { boardKey, useProject } from "./data";
import { operatorReviewReady } from "./focus";
import { AskAnswers } from "./phone";

type NextAction = { action_id: string; task_id: string; kind: string; owner_kind: string; enabled: boolean; blockers: string[] };

export function AttentionPage({ projectId, back, toast }: { projectId: string; back: string | null; toast: (text: string) => void }) {
  const { project } = useProject(projectId);
  const live = useStreamUp();
  const offline = useOffline();
  const asks = useQuery<{ asks: Ask[] }>(`/api/asks?project=${encodeURIComponent(projectId)}&routed_to=operator`, { pollMs: live ? 60000 : 10000, staleMs: 2000 });
  const board = useQuery<ProjectBoardData>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const next = useQuery<{ actions: NextAction[] }>(`/api/projects/${encodeURIComponent(projectId)}/next-actions`, { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const open = (asks.data?.asks ?? []).filter((ask) => ask.routed_to === "operator" && !ask.resolved_at).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const review = (board.data?.tasks ?? []).filter(operatorReviewReady);
  const coveredTasks = new Set([...open.map((ask) => ask.task_id), ...review.map((task) => task.id)]);
  const otherActions = (next.data?.actions ?? []).filter((action) => action.owner_kind === "operator" && !coveredTasks.has(action.task_id));
  const titles = new Map((board.data?.tasks ?? []).map((task) => [task.id, task.title]));
  const known = !!asks.data && !!board.data && !!next.data;
  const uncertain = offline || !!asks.error || !!board.error || !!next.error;
  const lastConfirmed = Math.min(asks.updatedAt ?? Infinity, board.updatedAt ?? Infinity, next.updatedAt ?? Infinity);

  return <>
    <PageHeader title={t("focus.nav.attention")} subtitle={project?.name} back={back ?? undefined} />
    <main className="screen narrow focus-attention">
      {uncertain && <div className="focus-attention-warning" role="status">{t("focus.attention.stale")} {Number.isFinite(lastConfirmed) && t("focus.attention.lastConfirmed", { time: relTime(new Date(lastConfirmed).toISOString()) })} <button type="button" className="linkbtn" onClick={() => { asks.refresh(); board.refresh(); next.refresh(); }}>{t("common.retry")}</button></div>}
      {!known && !asks.error && !board.error && !next.error && <Skeleton rows={3} />}
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
      {known && !uncertain && open.length === 0 && review.length === 0 && otherActions.length === 0 && <div className="empty"><b>{t("focus.attention.empty")}</b></div>}
      {!known && (asks.error || board.error || next.error) && <button type="button" className="btn" onClick={() => { asks.refresh(); board.refresh(); next.refresh(); }}>{t("common.retry")}</button>}
    </main>
  </>;
}
