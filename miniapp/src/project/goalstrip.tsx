// The goal line over the orchestrator's composer: the project in one line of counts — work in hand,
// decisions needed, what waits for the operator, what nobody confirmed, what was promised — and, a
// tap away, the list behind them. It sits over the composer rather than in the header because a
// phone's header has no room for a second line, and the composer is where the operator is about to
// ask "what is going on?" — the line answers it before the question is typed.

import { useEffect, useId, useState } from "react";
import { acceptanceTone } from "../board/board";
import { relTime } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { navigate, projectPagePath } from "../router";
import { useFocusState } from "./data";
import { cardTitle, goalLine, goalRows, lineNeedsAttention, resultKey, resultRows, type FocusState } from "./goalmodel";

export function GoalStrip({ projectId }: { projectId: string }) {
  const state = useFocusState(projectId);
  const [open, setOpen] = useState(false);
  const listId = useId();
  const parts = goalLine(state?.counts);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  if (!state || (parts.length === 0 && state.goals.length === 0)) return null;
  const waitsForYou = (state.counts.waiting_for_you ?? 0) > 0;
  const blocked = goalRows(state.goals).find((goal) => goal.blocked || goal.next);
  const blockedReason = blocked?.next ? ("key" in blocked.next ? t(blocked.next.key) : blocked.next.text) : "";
  const headline = waitsForYou ? t("goal.headline.you") : blocked ? t(blockedReason ? "goal.headline.reason" : "goal.headline.blocked", { title: blocked.title || t("goal.card.untitled"), reason: blockedReason }) : state.goals[0] ? t("goal.headline.work", { title: state.goals[0].title || t("goal.card.untitled") }) : t("goal.headline.quiet");
  return (
    <div className={`goal-strip ${open ? "open" : ""} ${lineNeedsAttention(state.counts) ? "attn" : ""}`}>
      {open && <GoalList id={listId} state={state} />}
      <div className="goal-primary">
      <button type="button" className="goal-line" aria-expanded={open} aria-controls={open ? listId : undefined} onClick={() => setOpen((o) => !o)} title={t(open ? "goal.hide" : "goal.show")}>
        <Icon name="board" size={14} />
        <span className="goal-headline">{headline}</span>
        <span className="goal-chev" aria-hidden><Icon name="chevron" size={14} /></span>
      </button>
      {waitsForYou && <button type="button" className="btn small warn goal-next-action" onClick={() => navigate(projectPagePath(projectId, "attention"))}>{t("goal.openAttention")}</button>}
      {!waitsForYou && blocked && <button type="button" className="btn small goal-next-action" onClick={() => navigate(projectPagePath(projectId, "board", { task: blocked.id }))}>{t("goal.openTask")}</button>}
      </div>
    </div>
  );
}

/** The list the line opens: the goals, then the results waiting for a decision, then the promises. */
function GoalList({ id, state }: { id: string; state: FocusState }) {
  const goals = goalRows(state.goals);
  const results = resultRows(state.open_results);
  return (
    <div className="goal-list" id={id} role="region" aria-label={t("goal.list")}>
      {goals.length > 0 && (
        <section className="goal-sec">
          <h4 className="goal-sec-head">{t("goal.goals")} <span className="num">{goals.length}</span></h4>
          <ul>
            {goals.map((goal) => {
              const tone = goal.acceptance ? acceptanceTone(goal.acceptance as Parameters<typeof acceptanceTone>[0]) : "";
              return (
                <li key={goal.id} className={`goal-item ${goal.blocked ? "blocked" : ""}`} data-task={goal.id}>
                  <div className="goal-item-head">
                    <span className="goal-title">{goal.title || t("goal.card.untitled")}</span>
                    <span className="goal-status">{t(`board.col.${goal.status}`)}</span>
                  </div>
                  <div className="goal-item-meta">
                    <span className={`goal-owner ${goal.unowned ? "unowned" : ""}`}>
                      → {goal.unowned ? (goal.owner ? t("goal.owner.was", { name: goal.owner }) : t("goal.owner.none")) : goal.owner}
                    </span>
                    {goal.acceptance && <span className={`goal-accept ${tone}`} title={t(`pboard.acceptance.${goal.acceptance}.title`)}>{t(`pboard.acceptance.${goal.acceptance}`)}</span>}
                    {goal.next && <span className="goal-next">→ {t("goal.waits", { text: "key" in goal.next ? t(goal.next.key) : goal.next.text })}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {results.length > 0 && (
        <section className="goal-sec results">
          <h4 className="goal-sec-head">{t("goal.results")} <span className="num">{results.length}</span></h4>
          <ul>
            {results.map((result) => (
              <li key={String(result.id)} className={`goal-item result ${result.cause}`} data-result={String(result.id)}>
                <div className="goal-item-head">
                  <span className="goal-title">
                    {t(resultKey(result.cause), { who: result.staff_name || t("goal.result.member"), card: result.title || t(result.task_id ? "goal.card.untitled" : "goal.result.nocard") })}
                  </span>
                  <span className="goal-age num">{relTime(result.opened_at)}</span>
                </div>
                {(result.summary || result.reminded) && (
                  <div className="goal-item-meta">
                    {result.summary && <span className="goal-summary">«{result.summary}»</span>}
                    {result.reminded && <span className="goal-accept attn">{t("goal.reminded")}</span>}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {state.commitments.length > 0 && (
        <section className="goal-sec commitments">
          <h4 className="goal-sec-head">{t("goal.commitments")} <span className="num">{state.commitments.length}</span></h4>
          <ul>
            {state.commitments.map((c) => (
              <li key={String(c.id)} className="goal-item commitment">
                <div className="goal-item-head">
                  <span className="goal-title">{c.text}</span>
                  <span className="goal-age num">{relTime(c.at)}</span>
                </div>
                {c.task_id && <div className="goal-item-meta"><span className="goal-owner">→ {cardTitle(c.task_id, state.goals) || t("goal.card.untitled")}</span></div>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {goals.length === 0 && results.length === 0 && state.commitments.length === 0 && <div className="goal-empty">{t("goal.empty")}</div>}
    </div>
  );
}
