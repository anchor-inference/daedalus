// What agents do together over a link, drawn the same wherever it shows: a card beside a message's words, the answers
// to one question put to several agents side by side, a conversation's summary with what the operator can make of
// it, a guest's subscriptions, and the actions a link's turn asked for. Everything the other side wrote is plain
// text here, as everywhere in an agent link: a stranger's summary, slot title or address is never a link, markup or
// a picture. Only the host checked its shape; nothing checked its truth.

import { useState } from "react";
import { api, LinkAction, LinkCard, LinkCardRow, LinkPoll, LinkPromise, LinkSubscription } from "./api";
import { absTime, clock } from "./format";
import { Icon } from "./icons";
import { t } from "./i18n";
import { invalidate } from "./store";
import { errorText } from "./ui";
import "./linkflows.css";

const encoded = encodeURIComponent;

/** A promise as this side reads it. A card inside a door's message names the sender and the receiver; `ours` says
 *  whether this side sent that message. */
export function promiseSide(promise: Pick<LinkPromise, "by">, ours: boolean): "us" | "them" {
  if (promise.by === "us" || promise.by === "them") return promise.by;
  return (promise.by === "sender") === ours ? "us" : "them";
}

/** A span of time as one short line: the day and both clocks when it ends the same day. */
export function spanText(start: string, end: string): string {
  const a = new Date(start);
  const b = new Date(end);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return `${start} – ${end}`;
  return a.toDateString() === b.toDateString() ? `${absTime(start)} – ${clock(end)}` : `${absTime(start)} – ${absTime(end)}`;
}

export function LinkCardView({ card, ours = true }: { card: LinkCard; ours?: boolean }) {
  if (card.type === "summary") {
    const mine = card.promises.filter((p) => promiseSide(p, ours) === "us");
    const theirs = card.promises.filter((p) => promiseSide(p, ours) === "them");
    const list = (items: LinkPromise[], head: string, side: string) => items.length > 0 && (
      <div className="lf-promises" data-side={side}>
        <span className="lf-label">{head}</span>
        <ul>{items.map((p, i) => <li key={i}>{p.what}{p.due && <span className="sub"> · {t("flow.due", { due: p.due })}</span>}</li>)}</ul>
      </div>
    );
    return (
      <div className="lf-card" data-card="summary">
        <div className="lf-card-head"><Icon name="list" size={13} /><b className="truncate">{card.title || t("flow.summary")}</b></div>
        {list(mine, t("flow.promised.us"), "us")}
        {list(theirs, t("flow.promised.them"), "them")}
        {!card.promises.length && <div className="sub">{t("flow.promised.none")}</div>}
        {card.links.length > 0 && <div className="lf-row"><span className="lf-label">{t("flow.links")}</span><span className="lf-plain">{card.links.join("  ")}</span></div>}
        {card.files.length > 0 && <div className="lf-row"><span className="lf-label">{t("flow.files")}</span><span className="lf-plain">{card.files.join(", ")}</span></div>}
      </div>
    );
  }
  if (card.type === "slots") {
    return (
      <div className="lf-card" data-card="slots">
        <div className="lf-card-head"><Icon name="calendar" size={13} /><b className="truncate">{card.title || t("flow.slots")}</b><span className="sub">{t("flow.minutes", { n: card.duration_minutes })}</span></div>
        {card.slots.length ? <ul className="lf-slots">{card.slots.map((slot, i) => <li key={i}>{spanText(slot.start, slot.end)}</li>)}</ul> : <div className="sub">{t("flow.slots.none")}</div>}
        {card.busy && card.busy.length > 0 && <div className="lf-row"><span className="lf-label">{t("flow.busy")}</span><span className="lf-plain">{card.busy.map((b) => `${spanText(b.start, b.end)} ${b.title}`).join("; ")}</span></div>}
      </div>
    );
  }
  if (card.type === "meeting") {
    return (
      <div className="lf-card" data-card="meeting" data-state={card.state}>
        <div className="lf-card-head"><Icon name="calendar" size={13} /><b className="truncate">{card.title}</b><span className="lf-chip" data-state={card.state}>{t(`flow.meeting.${card.state}`)}</span></div>
        <div className="sub">{spanText(card.start, card.end)}</div>
      </div>
    );
  }
  return (
    <div className="lf-card" data-card="service" data-state={card.state}>
      <div className="lf-card-head"><Icon name="link" size={13} /><b className="truncate">{card.title}</b><span className="lf-chip" data-state={card.state}>{t(`flow.service.${card.state}`)}</span></div>
      {card.state === "open" && card.url && <div className="lf-plain mono">{card.url}</div>}
      {card.state === "open" && card.expires_at && <div className="sub">{t("flow.until", { at: absTime(card.expires_at) })}</div>}
    </div>
  );
}

// ── the answers to one question ───────────────────────────────────────────────────────────────

/** How far a question to several has got, in one short line. */
export function pollLine(poll: Pick<LinkPoll, "state" | "entries" | "deadline_at">): string {
  const answered = poll.entries.filter((e) => e.state === "answered").length;
  if (poll.state === "done") return answered < poll.entries.length ? t("flow.poll.late", { n: answered, of: poll.entries.length }) : t("flow.poll.done");
  return t("flow.poll.open", { n: answered, of: poll.entries.length, at: clock(poll.deadline_at) });
}

/** One question put to several agents, as a table that fills as the answers come: who, where it stands, what they said.
 *  The answers are strangers' words and stay plain text, cut to a few lines; the full one is on hover. */
export function PollCard({ poll, onDismiss }: { poll: LinkPoll; onDismiss?: () => void }) {
  return (
    <article className="lf-poll" data-poll={poll.id} data-poll-state={poll.state}>
      <div className="lf-card-head">
        <Icon name="bots" size={14} />
        <b className="grow truncate" title={poll.question}>{poll.question}</b>
        {onDismiss && <button type="button" className="iconbtn small" aria-label={t("flow.poll.dismiss")} title={t("flow.poll.dismiss")} onClick={onDismiss} data-poll-dismiss=""><Icon name="close" size={13} /></button>}
      </div>
      <div className="sub lf-poll-line">{pollLine(poll)}</div>
      <table className="lf-table">
        <thead><tr><th>{t("flow.poll.agent")}</th><th>{t("flow.poll.answer")}</th></tr></thead>
        <tbody>
          {poll.entries.map((entry) => (
            <tr key={entry.talk} data-entry-state={entry.state}>
              <td className="lf-host"><span className="truncate">{entry.host}</span><span className="lf-chip" data-state={entry.state}>{t(`flow.poll.state.${entry.state}`)}</span></td>
              <td><div className="lf-answer" title={entry.answer}>{entry.answer || "—"}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}

/** The questions to several that the operator has not put away, over the composer: where the answers gather. */
export function TalkPolls({ sessionId, polls }: { sessionId: string; polls: readonly LinkPoll[] }) {
  const shown = polls.filter((poll) => !poll.dismissed);
  if (!shown.length) return null;
  const dismiss = async (poll: LinkPoll) => {
    await api.post(`/api/agent-polls/${encoded(poll.id)}/dismiss`).catch(() => undefined);
    invalidate(`/api/sessions/${encoded(sessionId)}/agent-talks`);
  };
  return <div className="lf-polls" data-polls="">{shown.slice(0, 2).map((poll) => <PollCard key={poll.id} poll={poll} onDismiss={() => void dismiss(poll)} />)}</div>;
}

// ── summaries ────────────────────────────────────────────────────────────────────────────────

/** A conversation's summaries, newest first, each with what the operator can make of it, and the button that makes
 *  one. `path` is where Summary posts: the conversation's or the link's. */
export function Summaries({ rows, path, refresh, disabled, toast }: { rows: readonly LinkCardRow[]; path: string; refresh: () => void; disabled?: boolean; toast: (text: string) => void }) {
  const [busy, setBusy] = useState("");
  const summaries = rows.filter((row) => row.type === "summary").slice().reverse();
  async function act(key: string, work: () => Promise<unknown>, done?: string) {
    if (busy) return;
    setBusy(key);
    try {
      await work();
      if (done) toast(done);
      refresh();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="lf-summaries" data-summaries="">
      <button type="button" className="btn small" disabled={disabled || !!busy} data-summary-make="" onClick={() => void act("make", () => api.post(`${path}/summary`))}>
        <Icon name="list" size={13} />{busy === "make" ? t("flow.summary.making") : t("flow.summary.make")}
      </button>
      {summaries.map((row) => (
        <div key={row.id} className="lf-summary" data-summary={row.id} data-direction={row.direction}>
          <div className="sub lf-summary-who">{t(row.direction === "mine" ? "flow.summary.ours" : "flow.summary.theirs")} · {absTime(row.at)}</div>
          <LinkCardView card={row.card} />
          <div className="lf-actions">
            <button type="button" className="btn small" disabled={!!busy} data-summary-note="" onClick={() => void act(`n${row.id}`, () => api.post(`/api/link-cards/${row.id}/note`), t("flow.summary.noted"))}>{t("flow.summary.note")}</button>
            <button type="button" className="btn small" disabled={!!busy} data-summary-board="" onClick={() => void act(`b${row.id}`, () => api.post(`/api/link-cards/${row.id}/board`), t("flow.summary.boarded"))}>{t("flow.summary.board")}</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── what a link's turns set up ────────────────────────────────────────────────────────────────

export function actionDetail(action: LinkAction): string {
  if (action.kind === "patch") return [action.branch, action.stat].filter(Boolean).join(" · ");
  if (action.kind === "meeting") return [action.title, action.start && action.end ? spanText(action.start, action.end) : ""].filter(Boolean).join(" · ");
  return action.name ?? action.service ?? "";
}

/** Patches, services shown and meetings a link's turns asked for: what each is, where it stands, and what came of it.
 *  A service still shown can be taken back here. */
export function LinkActions({ actions, refresh, toast }: { actions: readonly LinkAction[]; refresh: () => void; toast: (text: string) => void }) {
  if (!actions.length) return null;
  const revoke = async (action: LinkAction) => {
    try {
      await api.post(`/api/link-actions/${encoded(action.id)}/revoke`);
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  };
  return (
    <div className="lf-list" data-link-actions="">
      {actions.map((action) => (
        <div key={action.id} className="lf-item" data-action-kind={action.kind} data-state={action.state}>
          <div className="lf-card-head"><b>{t(`flow.action.${action.kind}`)}</b><span className="lf-chip" data-state={action.state}>{t(`flow.action.state.${action.state}`)}</span></div>
          <div className="sub truncate" title={actionDetail(action)}>{actionDetail(action)}</div>
          {action.result && <div className="sub">{action.result}</div>}
          {action.kind === "service" && action.state === "done" && <button type="button" className="btn small" onClick={() => void revoke(action)}>{t("flow.action.revoke")}</button>}
        </div>
      ))}
    </div>
  );
}

export function subscriptionWhen(sub: Pick<LinkSubscription, "next_at" | "every_minutes" | "times_left" | "state">): string {
  if (sub.state !== "active") return "";
  const next = t("flow.sub.next", { at: absTime(sub.next_at) });
  return sub.every_minutes ? `${next} · ${t("flow.sub.every", { n: sub.every_minutes, left: sub.times_left })}` : next;
}

/** A guest's "tell me when" on this link: what it asked, when it is next checked, and End. */
export function LinkSubscriptions({ subs, refresh, toast }: { subs: readonly LinkSubscription[]; refresh: () => void; toast: (text: string) => void }) {
  if (!subs.length) return null;
  const end = async (sub: LinkSubscription) => {
    try {
      await api.post(`/api/link-subscriptions/${encoded(sub.id)}/end`);
      refresh();
    } catch (e) {
      toast(errorText(e));
    }
  };
  return (
    <div className="lf-list" data-link-subs="">
      <span className="lf-label">{t("flow.sub.title")}</span>
      {subs.map((sub) => (
        <div key={sub.id} className="lf-item" data-state={sub.state}>
          <div className="lf-plain">{sub.request}</div>
          <div className="sub">{sub.state === "active" ? subscriptionWhen(sub) : t("flow.sub.ended", { n: sub.fired })}</div>
          {sub.state === "active" && <button type="button" className="btn small" data-sub-end="" onClick={() => void end(sub)}>{t("flow.sub.end")}</button>}
        </div>
      ))}
    </div>
  );
}
