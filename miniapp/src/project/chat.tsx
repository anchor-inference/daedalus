// The orchestrator's chat, where it differs from any other: the project's events it was woken with are
// cards, not a system note to unfold; and what it did to the project is named in the project's words
// ("Task created · Photos"), inside the turn's worked group like any other agent's steps. The
// questions it put to the operator are not drawn here: they wait in the panel's Questions tab, and
// the chat carries one line that opens it.

import { createContext, useContext, useEffect, useSyncExternalStore } from "react";
import type { MessageView, OperatorSteps, ReplyRef } from "../api";
import { ArtifactCard } from "../artifact";
import { bytes } from "../format";
import { plural, t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { keptBase } from "../keptfiles";
import { downloadHref, type PreviewSource } from "../preview";
import type { ToolItem } from "../turns";
import { parseEvents } from "../turns";
import { useFocusState } from "./data";
import { isOrchestratorStep, stepDetail, stepKey } from "./focus";
import { excerptOf, hasFate, lastAnswerSeq, messageFate, type FateLine } from "./goalmodel";

/** What a turn in focus mode needs beyond its own session: the project, whether this is its
 *  orchestrator, and — in the orchestrator's chat — the way to answer something in it. */
export type FocusChat = { projectId: string; orchestrator: boolean; toast: (text: string) => void; reply?: (target: ReplyRef) => void };

export const FocusChatContext = createContext<FocusChat | null>(null);

export function useFocusChat(): FocusChat | null {
  return useContext(FocusChatContext);
}

/** A batch of the project's events, one line each, coloured by what the line says happened. In the
 *  orchestrator's chat each line can be answered: a member's report is what a correction is most often
 *  about, and a correction read as being about the report beside it is how one went to the wrong member. */
export function EventCard({ text, seq }: { text: string; seq?: number | null }) {
  const reply = useFocusChat()?.reply;
  const batch = parseEvents(text);
  if (!batch) return null;
  return (
    <section className="event-card" aria-label={t("focus.events.label")}>
      <div className="event-head">
        <Icon name="bolt" size={14} />
        <span>{plural("focus.events.head", batch.count, { time: batch.since })}</span>
      </div>
      <ul className="event-lines">
        {batch.lines.map((line, i) => (
          <li key={i} className={`event-line ${line.tone} ${reply && seq ? "answerable" : ""}`}>
            <span className="event-time num">{line.time}</span>
            <span className="event-text">{line.text}</span>
            {reply && seq ? (
              <button type="button" className="iconbtn small event-reply" onClick={() => reply({ seq, excerpt: excerptOf(`${line.time} ${line.text}`) })} aria-label={t("reply.action")} title={t("reply.action")}>
                <Icon name="reply" size={14} />
              </button>
            ) : null}
          </li>
        ))}
        {batch.more > 0 && <li className="event-line info more">{t("focus.events.more", { n: batch.more })}</li>}
      </ul>
    </section>
  );
}

const STEP_ICONS: Record<string, IconName> = {
  Folders: "folder", Tasks: "board", Journal: "journal", Brief: "pen", Team: "bots", Hire: "bots", StaffEdit: "bots", Dismiss: "bots",
  Assign: "forward", Tell: "forward", ReadStaff: "file", Answer: "check", Interrupt: "stop", Pause: "stop", Release: "stop",
  Peek: "eye", WakeMe: "clock", Watch: "eye", Unwatch: "eye", AskOperator: "question", WithdrawQuestions: "undo", ProjectReport: "journal",
};

/**
 * How one of the orchestrator's own tool calls reads in its turn's worked group: the step in the
 * project's words and what it acted on. It replaces the tool's name and arguments there rather than
 * adding a second line elsewhere: the steps were once drawn both inside the group (as raw calls) and
 * again under it (as these lines), and the operator read every action twice.
 */
export function stepDescription(item: ToolItem): { verb: string; detail: string; icon: IconName } | null {
  if (!isOrchestratorStep(item.name)) return null;
  return { verb: t(`focus.step.${stepKey(item.name, item.args)}`), detail: stepDetail(item.name, item.args), icon: STEP_ICONS[item.name] ?? "conductor" };
}

// ── answering something in the chat ──────────────────────────────────────────────────────────

// What the operator chose to answer, per conversation, until the message goes out or the chip is
// removed. Outside React state on purpose: the button that sets it sits in a turn deep in the list,
// the chip over the composer, and neither should re-render the conversation between them.
const replies = new Map<string, ReplyRef>();
const replyListeners = new Set<() => void>();

function subscribeReplies(listener: () => void): () => void {
  replyListeners.add(listener);
  return () => {
    replyListeners.delete(listener);
  };
}

export function setReply(sessionId: string, target: ReplyRef | null): void {
  if (target) replies.set(sessionId, target);
  else replies.delete(sessionId);
  for (const listener of [...replyListeners]) listener();
}

/** What the next message answers, if anything: read when the message is sent. */
export function currentReply(sessionId: string): ReplyRef | null {
  return replies.get(sessionId) ?? null;
}

export function useReply(sessionId: string): ReplyRef | null {
  return useSyncExternalStore(subscribeReplies, () => replies.get(sessionId) ?? null);
}

/** The quote over the composer: what the next message answers, and the way to take it back. */
export function ReplyChip({ sessionId }: { sessionId: string }) {
  const reply = useReply(sessionId);
  if (!reply) return null;
  return (
    <div className="reply-chip" role="status" aria-label={t("reply.chip")}>
      <Icon name="reply" size={14} />
      <span className="reply-chip-label">{t("reply.chip")}</span>
      <span className="reply-chip-text truncate" title={reply.excerpt}>«{reply.excerpt}»</span>
      <button type="button" className="iconbtn small reply-chip-remove" onClick={() => setReply(sessionId, null)} aria-label={t("reply.remove")} title={t("reply.remove")}>
        <Icon name="close" size={14} />
      </button>
    </div>
  );
}

/** What a sent message answered, at the top of its bubble; it leads to that message. */
export function ReplyQuote({ reply }: { reply: ReplyRef }) {
  return (
    <a className="msg-quote" href={`#m${reply.seq}`} aria-label={t("reply.quote")} title={t("reply.quote")}>
      <Icon name="reply" size={12} />
      <span className="msg-quote-text">{reply.excerpt}</span>
    </a>
  );
}

// ── what became of the operator's message ────────────────────────────────────────────────────

// The seq of the orchestrator's latest answer, per conversation: a message before it has been read.
// Kept beside the turns rather than passed into them, so a new answer re-draws the receipts and not
// every turn of the list.
const answered = new Map<string, number>();
const answeredListeners = new Set<() => void>();

function subscribeAnswered(listener: () => void): () => void {
  answeredListeners.add(listener);
  return () => {
    answeredListeners.delete(listener);
  };
}

/** Keep the latest answer of a conversation's transcript where the receipts read it. */
export function useAnsweredMark(sessionId: string, messages: readonly MessageView[] | null | undefined, on: boolean): void {
  const last = on ? lastAnswerSeq(messages) : null;
  useEffect(() => {
    if (last === null || answered.get(sessionId) === last) return;
    answered.set(sessionId, last);
    for (const listener of [...answeredListeners]) listener();
  }, [sessionId, last]);
}

function useAnswered(sessionId: string): number | null {
  return useSyncExternalStore(subscribeAnswered, () => answered.get(sessionId) ?? null);
}

function FateRow({ line }: { line: FateLine }) {
  if (line.kind === "commitment") {
    return (
      <li className={`fate-line commitment ${line.kept ? "ok" : ""}`}>
        <span className="fate-what">{t("fate.commitment", { text: line.text })}</span>
        <span className={`fate-state ${line.kept ? "ok" : "open"}`}>{t(line.kept ? "fate.kept" : "fate.open")}</span>
      </li>
    );
  }
  const replaced = line.state === "superseded" || line.state === "withdrawn";
  return (
    <li className={`fate-line requirement ${replaced ? "replaced" : ""}`} title={line.text}>
      <span className="fate-what">{t("fate.requirement", { label: line.label, card: line.card || t("goal.card.untitled") })}</span>
      {replaced && <span className="fate-state">{t(`pboard.req.state.${line.state}`)}</span>}
      {!replaced && line.deliveries.length === 0 && <span className="fate-state open">{t("fate.undelivered")}</span>}
      {!replaced && line.deliveries.map((d, i) => (
        <span key={i} className={`fate-state ${d.state === "sent" ? "open" : "ok"}`}>{t("fate.delivered", { name: d.name })} → {t(`fate.state.${d.state}`)}</span>
      ))}
    </li>
  );
}

/**
 * The small print under an operator's message in the orchestrator's chat: what it became (a
 * requirement on a card and whether the member confirmed it, a commitment and whether it was kept),
 * that it was read, and how it arrived when it arrived during a turn or as one ended. A correction
 * the operator made once went nowhere and the chat said nothing; this is where the chat says.
 */
export function MessageFate({ message, sessionId, projectId }: { message: MessageView; sessionId: string; projectId: string }) {
  const state = useFocusState(projectId);
  const last = useAnswered(sessionId);
  const fate = messageFate(message, state?.receipts, last);
  if (!hasFate(fate)) return null;
  return (
    <ul className="msg-fate" aria-label={t("fate.label")}>
      {fate.arrival && <li className="fate-line arrival">{t(`fate.${fate.arrival}`)}</li>}
      {fate.lines.map((line, i) => <FateRow key={i} line={line} />)}
      {fate.read && <li className="fate-line read"><Icon name="check" size={12} />{t("fate.read")}</li>}
    </ul>
  );
}

// ── a member's steps for the operator ────────────────────────────────────────────────────────

/**
 * The steps a member wrote for the operator, as the host carried them: the goal, whether the member
 * walked them on the version that runs, which account is which, the steps in order, what to expect
 * and how to check, and the file they are kept in. Drawn from the fields rather than from the note's
 * markdown, so the table of accounts stays a table on a phone.
 */
export function StepsCard({ steps, seq, onOpen }: { steps: OperatorSteps; seq?: number | null; onOpen: (src: PreviewSource) => void }) {
  const reply = useFocusChat()?.reply;
  const unverified = steps.verified !== "on-running-version";
  const file = steps.file;
  const src: PreviewSource | null = file ? { base: keptBase(file.id), path: file.name } : null;
  const facts = [["expected", steps.expected], ["check", steps.check], ["limits", steps.limits]].filter(([, v]) => !!v);
  return (
    <section className={`steps-card ${unverified ? "unverified" : "verified"}`} aria-label={t("steps.label")} data-seq={seq ?? undefined}>
      <div className="steps-head">
        <Icon name="user" size={14} />
        <span className="steps-kind">{t("steps.label")}</span>
        <span className="steps-from truncate">{steps.task_id ? t("steps.from.task", { name: steps.member, task: steps.task_id }) : t("steps.from", { name: steps.member })}</span>
      </div>
      <div className="steps-goal">{steps.goal}</div>
      <div className={`steps-badge ${unverified ? "warn" : "ok"}`}>
        <Icon name={unverified ? "alert" : "check"} size={12} />
        <span><b>{t(unverified ? "steps.unverified" : "steps.verified")}</b>{steps.verified_how ? ` — ${steps.verified_how}` : ""}</span>
      </div>
      {steps.roles.length > 0 && (
        <table className="steps-roles">
          <thead>
            <tr><th>{t("steps.account")}</th><th>{t("steps.purpose")}</th></tr>
          </thead>
          <tbody>
            {steps.roles.map((role, i) => <tr key={i}><td>{role.account}</td><td>{role.purpose}</td></tr>)}
          </tbody>
        </table>
      )}
      <ol className="steps-list">
        {steps.steps.map((step, i) => <li key={i}>{step}</li>)}
      </ol>
      {facts.length > 0 && (
        <dl className="steps-facts">
          {facts.map(([key, value]) => (
            <div key={key}><dt>{t(`steps.${key}`)}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      )}
      {(src || (reply && seq)) && (
        <div className="steps-foot">
          {src && file && (
            <ArtifactCard
              item={{ callId: file.id, path: `att:${file.id}`, name: file.name, how: "kept", caption: t("steps.file"), size: file.size ? bytes(file.size) : null }}
              src={src}
              downloadUrl={downloadHref(src.base, src.path)}
              onOpen={onOpen}
            />
          )}
          {reply && seq ? (
            <button type="button" className="btn small steps-reply" onClick={() => reply({ seq, excerpt: excerptOf(`${steps.member}: ${steps.goal}`) })}>
              <Icon name="reply" size={14} />{t("reply.action")}
            </button>
          ) : null}
        </div>
      )}
    </section>
  );
}
