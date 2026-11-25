// The list of schedules, grouped: what waits for the operator, what runs, what is paused, what is done.
// A row is the shared 56 px list row: what it is and when it runs in the operator's own clock over where
// it belongs, then its state in a word and how it last went. What waits for an answer carries its
// decision at the end of the row, so it is answered without opening it.

import { absTime, describeSchedule, relTimeLong, untilShort } from "../format";
import { t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { Button } from "../ui/primitives";
import type { ScheduleActions } from "./actions";
import { conversationOf, GROUPS, groupItems, type Item, lastRun, nameOf } from "./model";

const kindIcon = (kind: string): IconName => (kind === "message" ? "bell" : kind === "lazy" ? "bulb" : kind === "wake" ? "clock" : "loop");

export function ScheduleList({ items, selected, onSelect, titles, actions }: {
  items: Item[];
  selected: string | null;
  onSelect: (key: string) => void;
  titles: Record<string, string>;
  actions: ScheduleActions;
}) {
  const groups = groupItems(items);
  return (
    <div className="schedlist" role="list">
      {GROUPS.map((group) => groups[group].length === 0 ? null : (
        <section key={group} className={`schedlist-group ${group}`} aria-label={t(`sched.group.${group}`)}>
          <div className="schedlist-title">{t(`sched.group.${group}`)} <span className="n">{groups[group].length}</span></div>
          {groups[group].map((item) => <Row key={item.key} item={item} on={item.key === selected} onSelect={onSelect} titles={titles} actions={actions} />)}
        </section>
      ))}
    </div>
  );
}

/** What a row says about the schedule's state in one word, and the tone it is said in. */
function stateOf(item: Item): { word: string; tone: "attention" | "plain" | "quiet"; title?: string } {
  if (item.source === "proposal") return { word: t("sched.status.waiting"), tone: "attention" };
  const s = item.schedule;
  if (item.group === "approval") return { word: t("sched.approval.needed"), tone: "attention" };
  if (item.group === "active" && s.next_run_at) return { word: untilShort(s.next_run_at), tone: "plain", title: absTime(s.next_run_at) };
  if (item.group === "paused") return { word: t("sched.paused.word"), tone: "quiet" };
  return { word: t("sched.status.done"), tone: "quiet" };
}

function Row({ item, on, onSelect, titles, actions }: { item: Item; on: boolean; onSelect: (key: string) => void; titles: Record<string, string>; actions: ScheduleActions }) {
  const s = item.source === "schedule" ? item.schedule : null;
  const p = item.source === "proposal" ? item.proposal : null;
  const rule = s ?? p!;
  const chat = conversationOf(item);
  const chatTitle = chat ? titles[chat] : undefined;
  const last = s ? lastRun(s) : null;
  const state = stateOf(item);
  const answers = !!p || (!!s && s.authority_state === "needs_approval" && item.group === "approval");
  return (
    <div
      role="listitem"
      className={`schedrow ${on ? "on" : ""} ${item.group}`}
      data-schedule={item.key}
    >
      {/* The row is one button for the whole of its face, and the decisions sit beside it rather than
          inside it: a button nested in a button is neither valid nor reachable by keyboard. */}
      <button type="button" className="schedrow-open" aria-current={on ? "true" : undefined} onClick={() => onSelect(item.key)}>
        <span className={`schedrow-tile ${item.group}`} aria-hidden="true"><Icon name={kindIcon(rule.kind)} size={16} /></span>
        <span className="schedrow-main">
          <span className="schedrow-name">{nameOf(item)}</span>
          <span className="schedrow-when">{describeSchedule(rule)}{chatTitle ? ` · ${chatTitle}` : p ? ` · ${t("sched.proposal.from.agent")}` : ""}</span>
        </span>
        <span className={`schedrow-state ${state.tone}`} title={state.title}>{state.word}</span>
        {!answers && (
          <span className={`schedrow-last ${last?.status ?? "none"}`} title={last?.at ? absTime(last.at) : undefined}>
            {last && <><span className="dot" aria-hidden="true" />{last.status === "running" ? t("sched.running") : t(last.status === "failed" ? "sched.last.failed" : "sched.last.ok", { t: relTimeLong(last.at) })}</>}
          </span>
        )}
      </button>
      {p && (
        <div className="schedrow-actions">
          <Button variant="quiet" size="sm" disabled={!!actions.busy} onClick={() => void actions.decide(p, "withdraw")}>{t("sched.proposal.withdraw")}</Button>
          <Button size="sm" icon="check" disabled={!!actions.busy || p.legacy_file_review_required} onClick={() => void actions.decide(p, "accept")}>{t("sched.proposal.accept")}</Button>
        </div>
      )}
      {s && answers && !p && (
        <div className="schedrow-actions">
          <Button size="sm" icon="check" disabled={!!actions.busy} onClick={() => void actions.approve(s)}>{t("sched.approval.action")}</Button>
        </div>
      )}
    </div>
  );
}
