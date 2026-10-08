// What waits for the operator in a project, on a phone: one compact card at the top of a tab and one
// sheet that answers every request. The card shows the request that has waited longest and answers it
// with a tap; the sheet behind its "+N more" is the Questions tab, the same one the orchestrator's chat
// opens, so a decision has one place to be made. Before the redesign the same ask stood on four
// surfaces at once (the banner, the board's column, the chat's line and the sheet), each with its own
// buttons, and the operator could not tell which one answered it.
//
// The card keeps three states the old banner lost: an answer chosen without a connection stays chosen
// and is sent with one tap once the bot is back; an answer that lost to one given elsewhere says who
// gave it and steps to the next request instead of vanishing with a toast; and a count it cannot
// confirm says so rather than reading as "nothing waits".

import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, ApiError, type Ask } from "../api";
import { relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { navigate, projectPagePath } from "../router";
import { answeredBy, askWords } from "../staff/model";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import { BottomSheet } from "../ui/phone";
import { QuestionsPanel } from "../questions/QuestionsPanel";
import { useQuestions } from "../questions/data";
import type { Staff } from "../team/team";
import type { Project } from "../api";
import { useFinishSetup } from "../main/cards";
import { staffKey } from "./data";
import { useGoalBudget, budgetCompact } from "./ProjectBudget";

const enc = encodeURIComponent;

type Answer = { selected?: string[]; text?: string; allow?: boolean };
type Held = { askId: string; answer: Answer; label: string };

/** Who is asking, in a few words: the member by name, or the orchestrator. */
function askerShort(ask: Ask, names: Map<string, string>): string {
  const name = ask.staff_id ? names.get(ask.staff_id) : undefined;
  if (ask.kind === "permission") return name ? t("needs.asker.permission", { name }) : t("needs.asker.orchestrator.permission");
  return name ? t("needs.asker", { name }) : t("needs.asker.orchestrator");
}

/** The options a card can offer as buttons: two at most, and only when each is short enough to sit
 *  beside the other at 412 px. A longer set is answered in the sheet, where every option has a row. */
function inlineOptions(ask: Ask): string[] {
  const options = Array.isArray(ask.detail?.options) ? ask.detail.options.filter((o): o is string => typeof o === "string") : [];
  if (ask.detail?.multi || options.length === 0 || options.length > 2 || options.some((o) => o.length > 22)) return [];
  return options;
}

/** The decision sheet: every open request of the project, answered at the operator's pace and sent
 *  together. It is the Questions tab of the orchestrator's chat, drawn in a sheet of its own so the
 *  card can open it from any tab. */
export function DecisionSheet({ projectId, toast, onClose }: { projectId: string; toast: (text: string) => void; onClose: () => void }) {
  const scope = useMemo(() => ({ projectId }), [projectId]);
  const { questions } = useQuestions(scope);
  const n = questions?.length ?? 0;
  return (
    <BottomSheet full className="ph-decisions" onClose={onClose} label={t("panel.tab.questions")}
      title={<span className="ph-sheet-title">{t("panel.tab.questions")}{n > 0 && <span className="ph-badge warn">{n}</span>}</span>}>
      <QuestionsPanel scope={scope} toast={toast} />
    </BottomSheet>
  );
}

export type NeedsAsks = { ask: Ask | null; openAsks: Ask[]; unverified: boolean };

/** The one card. `asks` is the project's open requests (useOperatorAsks), handed in so the card and
 *  the tab bar's badge read one query. */
export function NeedsYouCard({ projectId, asks, toast }: { projectId: string; asks: NeedsAsks; toast: (text: string) => void }) {
  const offline = useOffline();
  const { data: team } = useQuery<{ staff: Staff[] }>(staffKey(projectId), { staleMs: 5000 });
  const names = new Map((team?.staff ?? []).map((m) => [m.id, m.name]));
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const [lost, setLost] = useState<{ ask: Ask; who: string } | null>(null);
  const [held, setHeld] = useState<Held | null>(null);
  const [writing, setWriting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [sheet, setSheet] = useState(false);
  const open = [...asks.openAsks].sort((a, b) => a.created_at.localeCompare(b.created_at)).filter((a) => !dismissed.has(a.id));
  const ask = lost?.ask ?? open[0] ?? null;
  const rest = open.filter((a) => a.id !== ask?.id).length;
  // A held answer belongs to one request; once that request is gone (answered elsewhere, withdrawn)
  // there is nothing left to send it to.
  useEffect(() => {
    if (held && !asks.openAsks.some((a) => a.id === held.askId)) setHeld(null);
  }, [held, asks.openAsks]);

  if (!ask) return sheet ? <DecisionSheet projectId={projectId} toast={toast} onClose={() => setSheet(false)} /> : null;
  const permission = ask.kind === "permission" || ask.kind === "folder";
  const options = permission ? [] : inlineOptions(ask);
  const heldHere = held?.askId === ask.id ? held : null;

  async function send(answer: Answer, label: string) {
    if (!ask || busy) return;
    if (offline) {
      // Kept on this device and sent by hand once the bot is back: sending it by itself later could
      // answer a request that changed in between.
      setHeld({ askId: ask.id, answer, label });
      return;
    }
    setBusy(true);
    try {
      await api.post(`/api/asks/${enc(ask.id)}/answer`, answer);
      toast(t("focus.ask.sent"));
      setHeld(null);
      setWriting(false);
      setText("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const who = answeredBy(e.message);
        setLost({ ask, who: who ? t(`perm.by.${who}`) : "" });
      } else toast(errorText(e));
    } finally {
      setBusy(false);
      invalidate(`/api/asks?project=${enc(projectId)}`);
      invalidate(`/api/projects/${enc(projectId)}/board`);
      invalidate(`/api/projects/${enc(projectId)}/staff`);
    }
  }
  const submitOwn = (e: FormEvent) => {
    e.preventDefault();
    const words = text.trim();
    if (ask.kind === "permission") void send({ allow: false, text: words }, words || t("phone.ask.deny"));
    else if (words) void send({ text: words }, words);
  };
  const next = () => {
    setDismissed((d) => new Set([...d, ask.id]));
    setLost(null);
  };
  const title = permission ? (ask.title || ask.heading || "") : "";
  const words = askWords(ask);

  if (lost) {
    return (
      <section className="needs-card answered" data-ask={ask.short_id} aria-label={t("phone.needs")}>
        <div className="needs-head">
          <Icon name="check" size={16} />
          <b className="needs-word">{lost.who ? t("needs.answered.by", { who: lost.who }) : t("needs.answered")}</b>
        </div>
        <div className="needs-text clamp-2">{title || words}</div>
        <div className="needs-row">
          {rest > 0 && <button type="button" className="ph-btn grow" onClick={next}>{t("needs.next")}<Icon name="forward" size={16} /></button>}
          <button type="button" className="ph-link" onClick={next}>{t("questions.dismiss")}</button>
        </div>
      </section>
    );
  }

  // Without a connection the list cannot be confirmed either; a tap then holds the answer here instead
  // of being refused, which is the one thing an operator on a train can still do.
  const blocked = busy || (asks.unverified && !offline);
  return (
    <>
      <section className={`needs-card ${permission ? "perm" : "ask"}`} data-ask={ask.short_id} aria-label={t("phone.needs")}>
        <div className="needs-head">
          <Icon name={permission ? "shield" : "ask"} size={16} />
          <b className="needs-word">{t("phone.needs")}</b>
          <span className="needs-who truncate">· {askerShort(ask, names)} · {relTime(ask.created_at)}</span>
          {rest > 0 && (
            <button type="button" className="needs-more" onClick={() => setSheet(true)} aria-label={plural("needs.more.label", rest)} data-more={rest}>
              {t("phone.needs.more", { n: rest })}<Icon name="forward" size={16} />
            </button>
          )}
        </div>
        {title && <div className="needs-text clamp-2">{title}</div>}
        {permission ? <code className="needs-code">{words}</code> : <div className="needs-text clamp-2">{words}</div>}
        {asks.unverified && !offline && <div className="needs-note warn" role="status">{t("focus.attention.stale")}</div>}
        {heldHere ? (
          <>
            <div className="needs-row">
              <span className="needs-held"><Icon name="check" size={16} />{heldHere.label}</span>
              <button type="button" className="ph-link" onClick={() => setHeld(null)}>{t("questions.clear")}</button>
            </div>
            {offline && <div className="needs-note bad" role="status"><Icon name="offline" size={16} />{t("needs.offline.held")}</div>}
            <button type="button" className="ph-btn wide" disabled={offline || busy} onClick={() => void send(heldHere.answer, heldHere.label)}>
              {t(offline ? "needs.offline.submit" : "needs.submit")}
            </button>
          </>
        ) : writing ? (
          <form className="needs-own" onSubmit={submitOwn}>
            <input className="field" autoFocus value={text} maxLength={4000} enterKeyHint="send"
              placeholder={t(ask.kind === "permission" ? "phone.ask.why" : "focus.ask.placeholder")}
              aria-label={t(ask.kind === "permission" ? "phone.ask.why" : "focus.ask.placeholder")}
              onChange={(e) => setText(e.target.value)} />
            <button className="ph-btn primary" type="submit" disabled={blocked || (ask.kind !== "permission" && !text.trim())}>{t("focus.ask.send")}</button>
            <button className="ph-ib" type="button" onClick={() => setWriting(false)} aria-label={t("common.cancel")} title={t("common.cancel")}><Icon name="close" size={22} /></button>
          </form>
        ) : permission ? (
          <>
            {/* Out of the run's context nothing here is white: Allow is as heavy as Deny, and the
                broad grants wait in the sheet, where the request is read in full. */}
            <div className="needs-row">
              <button type="button" className="ph-btn grow" disabled={blocked} data-answer="deny" onClick={() => void send({ allow: false }, t(ask.kind === "folder" ? "focus.ask.no" : "phone.ask.deny"))}>{t(ask.kind === "folder" ? "focus.ask.no" : "phone.ask.deny")}</button>
              <button type="button" className="ph-btn grow" disabled={blocked} data-answer="allow" onClick={() => void send({ allow: true }, t(ask.kind === "folder" ? "focus.ask.yes" : "needs.allow.once"))}>{t(ask.kind === "folder" ? "focus.ask.yes" : "needs.allow.once")}</button>
            </div>
            {ask.kind === "permission" && (
              <div className="needs-links">
                <button type="button" className="ph-link" disabled={blocked} onClick={() => setWriting(true)}>{t("phone.ask.denyWhy")}</button>
                <button type="button" className="ph-link" onClick={() => setSheet(true)}>{t("needs.more.options")}</button>
              </div>
            )}
          </>
        ) : (
          <div className="needs-row">
            {options.length > 0
              ? options.map((option, i) => (
                <button key={option} type="button" className={`ph-btn grow ${i === 0 ? "primary" : ""}`} disabled={blocked} data-option={option} onClick={() => void send({ selected: [option] }, option)}>
                  <span className="truncate">{option}</span>
                </button>
              ))
              : <button type="button" className="ph-btn grow primary" onClick={() => setSheet(true)}>{t("needs.answer")}</button>}
            {/* Words of the operator's own: a pen beside the options, the way the design draws it. */}
            <button type="button" className="ph-ib needs-pen" disabled={blocked} onClick={() => setWriting(true)} aria-label={t("phone.ask.write")} title={t("phone.ask.write")}><Icon name="pen" size={22} /></button>
          </div>
        )}
      </section>
      {sheet && <DecisionSheet projectId={projectId} toast={toast} onClose={() => setSheet(false)} />}
    </>
  );
}

/** A project the main orchestrator is still setting up, on its Orchestrator tab: what is done, what is
 *  left with the way to it, and the button that ends the setup by hand. The desktop keeps its one line. */
export function SetupCard({ project, goal, staff, toast }: { project: Project; goal: boolean; staff: number | null; toast: (text: string) => void }) {
  const { data: budget } = useGoalBudget(project.id);
  const money = budgetCompact(budget);
  const { busy, finish } = useFinishSetup(project.id, project.name, toast);
  const steps = [
    { key: "brief", done: goal, label: t(goal ? "needs.setup.brief.done" : "needs.setup.brief"), href: projectPagePath(project.id, "brief") },
    { key: "budget", done: !!money, label: money ? t("needs.setup.budget.set", { amount: money }) : t("needs.setup.budget"), href: null },
    { key: "folders", done: project.folders.length > 0, label: t(project.folders.length > 0 ? "needs.setup.folders.done" : "needs.setup.folders"), href: projectPagePath(project.id, "folders") },
    { key: "team", done: (staff ?? 0) > 0, label: t((staff ?? 0) > 0 ? "needs.setup.team.done" : "needs.setup.team"), href: projectPagePath(project.id, "team") },
  ];
  const left = steps.filter((s) => !s.done).length;
  return (
    <section className="ph-setup" data-setup={project.id} aria-label={t("needs.setup.title", { name: project.name })}>
      <h3>{t("needs.setup.title", { name: project.name })}</h3>
      <p>{left ? plural("needs.setup.left", left) : t("needs.setup.ready")}</p>
      <ul>
        {steps.map((step) => (
          <li key={step.key} className={step.done ? "done" : ""} data-step={step.key}>
            {step.href && !step.done
              ? <button type="button" onClick={() => navigate(step.href!)}><span className="ph-setup-mark" aria-hidden>{step.done && <Icon name="check" size={16} />}</span>{step.label}<Icon name="forward" size={16} /></button>
              : <span className="ph-setup-step"><span className="ph-setup-mark" aria-hidden>{step.done && <Icon name="check" size={16} />}</span>{step.label}</span>}
          </li>
        ))}
      </ul>
      <button type="button" className="ph-btn primary wide" disabled={busy} onClick={() => void finish()}>{t("main.setup.finish")}</button>
    </section>
  );
}
