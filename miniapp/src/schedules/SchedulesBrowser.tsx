// The schedules and the open one: the whole of the Schedules screen, and the contents of the sheet over a
// chat. One implementation for both, so the sheet is never the poorer copy of the page.
//
// On the page the list is the page's 880 column under its header and the open schedule is a 420 px pane
// beside it, as a card's pane is beside the board; below 1024 px the pane is a full sheet over the list.
// In the sheet over a chat the list and the schedule take turns, the schedule with a way back.

import { useEffect, useMemo, useRef, useState } from "react";
import { t } from "../i18n";
import { Icon } from "../icons";
import { useQuery } from "../store";
import { Page, PageHeader, screenTitle } from "../shell";
import { useMedia } from "../ui/index";
import { Skeleton } from "../ui/components";
import { Button, IconButton, Segmented, Sheet } from "../ui/primitives";
import { useSessionTitles } from "../screens/Sessions";
import { useScheduleActions } from "./actions";
import { filterItems, groupItems, type Item, itemsFrom, type ProposalList, type ScheduleRow } from "./model";
import { ScheduleDetail } from "./ScheduleDetail";
import { ScheduleList } from "./ScheduleList";
import { NewScheduleSheet } from "./ScheduleForm";
import "./schedules.css";

export function SchedulesBrowser({ sessionId, selected, onSelect, toast, onOpenSession, variant }: {
  /** The conversation the sheet was opened from; the list starts on its schedules, with a switch to all. */
  sessionId?: string | null;
  selected: string | null;
  /** ``auto`` when the browser itself chose, not the reader: the page replaces its address then, or Back would land on a list that opens the same row again. */
  onSelect: (key: string | null, auto?: boolean) => void;
  toast: (text: string) => void;
  onOpenSession: (id: string) => void;
  variant: "page" | "sheet";
}) {
  const schedules = useQuery<ScheduleRow[]>("/api/schedules", { pollMs: 30000, staleMs: 5000 });
  const proposals = useQuery<ProposalList>("/api/recurring/proposals", { pollMs: 30000, staleMs: 5000 });
  const titles = useSessionTitles();
  const actions = useScheduleActions(toast, proposals.data);
  const wide = useMedia("(min-width: 1024px)");
  const phone = !wide;
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
  // Arriving on the page beside a pane, something is open to read: the first row opens, once. A pane the
  // reader closes stays closed; opened again by itself it could not be dismissed.
  const arrived = useRef(false);
  useEffect(() => {
    if (variant !== "page" || phone || arrived.current || !shown.length) return;
    arrived.current = true;
    if (selected) return;
    const groups = groupItems(shown);
    const first = groups.approval[0] ?? groups.active[0] ?? groups.paused[0] ?? groups.done[0];
    if (first) onSelect(first.key, true);
  }, [variant, phone, selected, shown, onSelect]);

  const loading = !schedules.data && !schedules.error;
  const search = (
    <label className="sched-search">
      <Icon name="search" size={16} />
      <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("sched.search")} aria-label={t("sched.search")} />
    </label>
  );
  const body = (
    <>
      {loading && <Skeleton rows={4} />}
      {schedules.error && !schedules.data && (
        <div className="empty"><b>{t("sched.error")}</b><div>{schedules.error}</div><button className="btn primary" onClick={schedules.refresh}>{t("common.retry")}</button></div>
      )}
      {proposals.error && <div className="result-warning" role="status">{t("sched.proposal.readfailed")}</div>}
      {schedules.data && all.length === 0 && (
        <div className="empty sched-empty">
          <Icon name="clock" size={20} />
          <b>{t("sched.empty")}</b>
          <div>{t("sched.empty.sub")}</div>
          <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>{t("sched.new")}</Button>
        </div>
      )}
      {schedules.data && all.length > 0 && shown.length === 0 && (
        <div className="empty sched-empty">
          <Icon name="search" size={20} />
          <b>{t(scope === "chat" && !query ? "sched.scope.chat.empty" : "sched.search.none")}</b>
          {scope === "chat" && <Button onClick={() => setScope("all")}>{t("sched.scope.showall")}</Button>}
        </div>
      )}
      <ScheduleList items={shown} selected={selected} onSelect={onSelect} titles={titles} actions={actions} />
    </>
  );
  const detail = (layout: "pane" | "sheet") => open && (
    <ScheduleDetail
      key={open.key}
      item={open}
      actions={actions}
      titles={titles}
      toast={toast}
      onOpenSession={onOpenSession}
      onRemoved={() => onSelect(null)}
      onClose={layout === "pane" ? () => onSelect(null) : undefined}
      onBack={variant === "sheet" ? () => onSelect(null) : undefined}
      layout={layout}
    />
  );
  const creator = creating && <NewScheduleSheet onClose={() => setCreating(false)} toast={toast} approvalDays={actions.approvalDays} />;

  if (variant === "sheet") {
    return (
      <div className="schedbrowser">
        {open ? detail("sheet") : (
          <>
            <div className="sched-tools">
              {search}
              <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>{t("sched.new")}</Button>
            </div>
            {sessionId && (
              <Segmented<"chat" | "all"> className="sched-scope" label={t("sched.scope")} size="md" value={scope} onChange={setScope}
                options={[{ id: "chat", label: t("sched.scope.chat") }, { id: "all", label: t("sched.scope.all") }]} />
            )}
            <div className="sched-list-body">{body}</div>
          </>
        )}
        {creator}
      </div>
    );
  }

  return (
    <div className="sched-page">
      <section className="sched-area" aria-label={t("nav.schedules")}>
        <PageHeader
          title={screenTitle("schedules")}
          actions={phone
            ? <IconButton icon="plus" label={t("sched.new")} onClick={() => setCreating(true)} />
            : <>{search}<Button variant="primary" icon="plus" className="sched-new" aria-label={t("sched.new")} title={t("sched.new")} onClick={() => setCreating(true)}><span className="sched-new-label">{t("sched.new")}</span></Button></>}
        />
        <div className="sched-scroll">
          <Page width="list" className="sched-body">
            {phone && search}
            {body}
          </Page>
        </div>
      </section>
      {open && wide && detail("pane")}
      {open && phone && (
        <Sheet ariaLabel={open.source === "schedule" ? open.schedule.name : open.proposal.name} size="full" className="sched-sheet" onClose={() => onSelect(null)}>
          {detail("sheet")}
        </Sheet>
      )}
      {!open && wide && selected && schedules.data && (
        <aside className="sched-pane sched-none" aria-label={t("sched.pane")}>
          <Icon name="calendar" size={20} />
          <span>{t("sched.gone")}</span>
        </aside>
      )}
      {creator}
    </div>
  );
}
