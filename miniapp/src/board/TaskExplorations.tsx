import { t } from "../i18n";
import { useQuery } from "../store";
import type { ProjectTask } from "./board";

export type ExplorationAttempt = {
  slot: number; state: string; staff_name: string; harness: string; model: string;
  task_id: string | null; forked: boolean; usd: number; unpriced: boolean;
};
export type Exploration = {
  id: string; task_id: string; state: string; reason: string; winner_slot: number | null;
  decision_reason: string; budget_usd: string; deadline_at: string; created_at: string;
  attempts: ExplorationAttempt[];
};

/** What an attempt cost; spend nobody priced is said, never shown as nothing. */
export function attemptCost(attempt: ExplorationAttempt): string {
  const usd = `$${attempt.usd.toFixed(2)}`;
  return attempt.unpriced ? t("explore.unpriced", { usd }) : usd;
}

/** The competing attempts a card was raced by, or is one of: who, how far, what each cost, and the
 *  one the orchestrator kept. Read-only; the orchestrator decides with its Explore tool. */
export function TaskExplorations({ task }: { task: ProjectTask }) {
  const query = useQuery<{ explorations: Exploration[] }>(`/api/board/${encodeURIComponent(task.id)}/explorations`);
  const found = query.data?.explorations ?? [];
  if (!found.length) return null;
  return <section className="explorations" aria-label={t("explore.title")}>
    <h4 className="sub">{t("explore.title")}</h4>
    {found.map((exploration) => <div key={exploration.id} className="exploration">
      <p className="sub">
        {t(`explore.state.${exploration.state}`, { slot: String(exploration.winner_slot ?? "") })}
        {exploration.state === "running" && <> · {t("explore.budget", { usd: exploration.budget_usd })}</>}
        {exploration.decision_reason && <> · {exploration.decision_reason}</>}
      </p>
      <ul className="exploration-attempts">
        {exploration.attempts.map((attempt) => <li key={attempt.slot} className={attempt.state === "won" ? "won" : undefined}>
          <span className="mono">{attempt.slot}.</span> {attempt.staff_name || t("explore.unnamed")}
          <span className="sub"> · {attempt.harness}{attempt.model ? ` ${attempt.model}` : ""}
            {attempt.forked && <> · {t("explore.forked")}</>}
            {" · "}{t(`explore.attempt.${attempt.state}`)} · {attemptCost(attempt)}
            {attempt.task_id && attempt.task_id !== task.id && <> · <span className="mono">{attempt.task_id}</span></>}
          </span>
        </li>)}
      </ul>
    </div>)}
  </section>;
}
