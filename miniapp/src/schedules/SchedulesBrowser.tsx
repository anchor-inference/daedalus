// The list beside the open schedule: the whole of the Schedules screen, and the whole of the schedules
// dialog over a chat. One implementation for both, so the dialog is never the poorer copy of the page.
// On a phone the two panes take turns: the list, then the schedule with a way back.

import { useEffect, useMemo, useState } from "react";
import { t } from "../i18n";
import { Icon } from "../icons";
import { useQuery } from "../store";
import { useMedia } from "../ui/index";
import { Skeleton } from "../ui/components";
import { useSessionTitles } from "../screens/Sessions";
import { useScheduleActions } from "./actions";
import { filterItems, groupItems, type Item, itemsFrom, type ProposalList, type ScheduleRow } from "./model";
import { ScheduleDetail } from "./ScheduleDetail";
import { ScheduleList } from "./ScheduleList";
import { NewScheduleSheet } from "./ScheduleForm";
import "./schedules.css";

export function SchedulesBrowser({ sessionId, selected, onSelect, toast, onOpenSession, variant }: {
  /** The conversation the dialog was opened from; the list starts on its schedules, with a switch to all. */
  sessionId?: string | null;
  selected: string | null;
  /** ``auto`` when the browser itself chose, not the reader: the page replaces its address then, or Back would land on a list that opens the same row again. */
  onSelect: (key: string | null, auto?: boolean) => void;
  toast: (text: string) => void;
  onOpenSession: (id: string) => void;
  variant: "page" | "dialog";
}) {
  const schedules = useQuery<ScheduleRow[]>("/api/schedules", { pollMs: 30000, staleMs: 5000 });
  const proposals = useQuery<ProposalList>("/api/recurring/proposals", { pollMs: 30000, staleMs: 5000 });
  const titles = useSessionTitles();
  const actions = useScheduleActions(toast, proposals.data);
  const phone = !useMedia("(min-width: 1024px)");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"chat" | "all">(sessionId ? "chat" : "all");
  const [creating, setCreating] = useState(false);

  const all = useMemo(() => itemsFrom(schedules.data, proposals.data?.entries), [schedules.data, proposals.data]);
  const shown = useMemo(() => filterItems(all, { query, sessionId: scope === "chat" ? sessionId : null, titles }), [all, query, scope, sessionId, titles]);
  // An accepted proposal becomes a schedule under a new id; the open pane follows it there.
  const open: Item | null = all.find((item) => item.key === selected) ?? null;
  useEffect(() => {
    if (!selected || open || !schedules.data || !proposals.data) return;
    const became = proposals.data.entries.find((p) => p.id === selected)?.accepted_schedule_id;
    if (became && all.some((item) => item.key === became)) onSelect(became, true);
  }, [selected, open, schedules.data, proposals.data, all, onSelect]);
  // On a wide screen the pane is never empty while there is something to show: the first row opens.
  useEffect(() => {
    if (phone || selected || !shown.length) return;
    const groups = groupItems(shown);
    const first = groups.approval[0] ?? groups.active[0] ?? groups.paused[0] ?? groups.done[0];
    if (first) onSelect(first.key, true);
  }, [phone, selected, shown, onSelect]);

  const loading = !schedules.data && !schedules.error;
  const listPane = (
    <div className="schedbrowser-list">
      <div className="schedbrowser-tools">
        <label className="schedbrowser-search">
          <Icon name="search" size={14} />
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("sched.search")} aria-label={t("sched.search")} />
        </label>
        <button type="button" className="iconbtn small primary" onClick={() => setCreating(true)} title={t("sched.new")} aria-label={t("sched.new")}><Icon name="plus" size={16} /></button>
      </div>
      {sessionId && (
        <div className="segmented schedbrowser-scope" role="radiogroup" aria-label={t("sched.scope")}>
          <button role="radio" aria-checked={scope === "chat"} className={scope === "chat" ? "on" : ""} onClick={() => setScope("chat")}>{t("sched.scope.chat")}</button>
          <button role="radio" aria-checked={scope === "all"} className={scope === "all" ? "on" : ""} onClick={() => setScope("all")}>{t("sched.scope.all")}</button>
        </div>
      )}
      <div className="schedbrowser-scroll">
        {loading && <Skeleton rows={4} />}
        {schedules.error && !schedules.data && (
          <div className="empty"><b>{t("sched.error")}</b><div>{schedules.error}</div><button className="btn primary" onClick={schedules.refresh}>{t("common.retry")}</button></div>
        )}
        {proposals.error && <div className="result-warning" role="status">{t("sched.proposal.readfailed")}</div>}
        {schedules.data && all.length === 0 && (
          <div className="empty schedbrowser-empty">
            <b>{t("sched.empty")}</b>
            <div>{t("sched.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}>{t("sched.new")}</button>
          </div>
        )}
        {schedules.data && all.length > 0 && shown.length === 0 && (
          <div className="empty schedbrowser-empty">
            <b>{t(scope === "chat" && !query ? "sched.scope.chat.empty" : "sched.search.none")}</b>
            {scope === "chat" && <button className="btn" onClick={() => setScope("all")}>{t("sched.scope.showall")}</button>}
          </div>
        )}
        <ScheduleList items={shown} selected={selected} onSelect={onSelect} titles={titles} actions={actions} />
      </div>
    </div>
  );
  const detailPane = open ? (
    <ScheduleDetail
      key={open.key}
      item={open}
      actions={actions}
      titles={titles}
      toast={toast}
      onOpenSession={onOpenSession}
      onRemoved={() => onSelect(null)}
      onBack={phone ? () => onSelect(null) : undefined}
    />
  ) : (
    <div className="schedbrowser-none">
      <Icon name="calendar" size={20} />
      <span>{t(selected && schedules.data ? "sched.gone" : "sched.pick")}</span>
    </div>
  );

  return (
    <div className={`schedbrowser ${variant === "page" ? "on-page" : "in-dialog"} ${phone ? (selected ? "show-detail" : "show-list") : ""}`}>
      {(!phone || !selected) && listPane}
      {(!phone || selected) && <div className="schedbrowser-detail">{detailPane}</div>}
      {creating && <NewScheduleSheet onClose={() => setCreating(false)} toast={toast} approvalDays={actions.approvalDays} />}
    </div>
  );
}
