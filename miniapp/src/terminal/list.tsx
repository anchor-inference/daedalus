// The Terminals page's list column: every terminal the host knows, found by name, owner or folder,
// narrowed by environment or owner, grouped by project, and the clean-up commands that go with a
// list that grows by itself (end the idle, forget the finished, pick several to end). The panes beside
// it show terminals; this is how one is chosen, and the only place a terminal nobody looks at is seen.
//
// The memory meter at its foot is the whole machine's answer to "may I open another?": the terminals'
// share of memory, with the projection for the configured cap one press away.

import { useMemo, useRef, useState } from "react";
import type { Project, TerminalLoad, TerminalView } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { LoadBar, size } from "../loadbar";
import { loadFigures } from "../machineload";
import { invalidate } from "../store";
import { OverflowMenu, Popover, type MenuItem } from "../ui/dialogs";
import { endTerminals, removeTerminals } from "./actions";
import { FILTERS, groupTerminals, idleUnwatched, matches, ownerLine, ownerShort, rowCommand, rowTag, running, type TerminalFilter } from "./preview";
import { useTerminalCommands } from "./rowmenu";
import { paneState, type PaneState } from "./status";
import type { TerminalState } from "./instance";
import { activeId, type Workspace } from "./workspacestate";
import { StateDot } from "./workspace";

export function ListColumn({ rows, states, ws, projects, load, onOpen, toast }: { rows: TerminalView[]; states: Record<string, TerminalState>; ws: Workspace; projects: Project[]; load: TerminalLoad | null; onOpen: (id: string) => void; toast: (text: string) => void }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<TerminalFilter>("all");
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects]);
  const needle = query.trim().toLowerCase();
  const found = rows.filter((r) => matches(r, filter) && (!needle || [r.title, r.cwd, r.owner.label ?? "", ownerLine(r, r.project_id ? names.get(r.project_id) ?? null : null), rowCommand(r)].some((s) => s.toLowerCase().includes(needle))));
  const groups = groupTerminals(found, projects);
  const current = activeId(ws);

  const refresh = () => invalidate("/api/terminals");
  // Several at once, each after one question that lists them all.
  const endMany = async (targets: TerminalView[], title?: string) => {
    const ended = await endTerminals(targets, toast, title);
    refresh();
    return ended;
  };
  const idle = rows.filter(idleUnwatched);
  const finished = rows.filter((r) => !running(r));
  const endIdle = () => {
    if (!idle.length) toast(t("term.bulk.idle.none"));
    else void endMany(idle, plural("term.bulk.idle.title", idle.length));
  };
  const removeFinished = async () => {
    await removeTerminals(finished.map((r) => r.id), toast);
    refresh();
  };
  const toggle = (id: string) => setSelected((all) => {
    const next = new Set(all);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  const chosen = rows.filter((r) => selected.has(r.id) && running(r));
  const endSelected = async () => {
    const ended = await endMany(chosen);
    if (ended !== null) {
      setSelected(new Set());
      setSelecting(false);
    }
  };
  const stopSelecting = () => {
    setSelecting(false);
    setSelected(new Set());
  };

  const actions: MenuItem[] = [
    { label: t("term.select"), icon: "check", disabled: !rows.some(running), onSelect: () => { setSelecting(true); setSelected(new Set()); } },
    { label: t("term.bulk.idle"), icon: "stop", aside: <span className="num">{idle.length}</span>, disabled: idle.length === 0, onSelect: endIdle },
    { label: t("term.bulk.finished"), icon: "close", aside: <span className="num">{finished.length}</span>, disabled: finished.length === 0, onSelect: () => void removeFinished() },
  ];

  return (
    <nav className="tp-list" aria-label={t("term.ws.list")}>
      <div className="tp-list-head">
        <label className="tp-list-find">
          <Icon name="search" size={16} />
          <input className="tp-list-search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("term.ws.search")} aria-label={t("term.ws.search")} />
        </label>
        <OverflowMenu className="tp-list-actions" icon="more" label={t("term.list.actions")} items={actions} />
      </div>
      <div className="tp-filters" role="group" aria-label={t("term.filter.label")}>
        {FILTERS.map((f) => (
          <button key={f} type="button" className="tp-filter" aria-pressed={filter === f} data-filter={f} onClick={() => setFilter(f)}>
            {t(`term.filter.${f}`)}
            <span className="tp-filter-n num">{rows.filter((r) => matches(r, f)).length}</span>
          </button>
        ))}
      </div>
      {selecting && (
        <div className="tp-select" role="toolbar" aria-label={t("term.select")}>
          <span className="tp-select-n num">{t("term.select.n", { n: chosen.length })}</span>
          <button type="button" className="btn small danger" data-action="end-selected" disabled={chosen.length === 0} onClick={() => void endSelected()}>
            <Icon name="stop" size={14} />
            <span>{t("term.select.end")}</span>
          </button>
          <button type="button" className="btn small ghost" data-action="select-done" onClick={stopSelecting}>{t("term.select.done")}</button>
        </div>
      )}
      <div className="tp-list-body">
        {/* With no terminal at all the page says so once, in the place of the panes. */}
        {found.length === 0 && rows.length > 0 && <div className="tp-list-none">{t("term.screen.none")}</div>}
        {groups.map((group) => {
          const live = group.rows.filter(running);
          const name = group.name ?? t("term.screen.noproject");
          return (
            <section key={group.key || "none"} className="tp-list-group" data-group={group.key || "none"}>
              <div className="tp-label">
                <span className="tp-label-name truncate">{name}</span>
                <span className="tp-label-n num">{group.rows.length}</span>
                {live.length > 0 && !selecting && (
                  <button type="button" className="tp-label-end" data-action="end-group" aria-label={t("term.bulk.group.named", { name })} title={t("term.bulk.group.named", { name })}
                    onClick={() => void endMany(live, plural("term.bulk.group.title", live.length, { name }))}>
                    <Icon name="stop" size={14} />
                  </button>
                )}
              </div>
              {group.rows.map((r) => (
                <ListRow key={r.id} row={r} status={paneState(r, states[r.id])} current={current === r.id} toast={toast} onOpen={() => onOpen(r.id)}
                  selection={selecting && running(r) ? { on: selected.has(r.id), toggle: () => toggle(r.id) } : null} />
              ))}
            </section>
          );
        })}
      </div>
      {load && <MemoryMeter load={load} />}
    </nav>
  );
}

function ListRow({ row, status, current, onOpen, toast, selection }: { row: TerminalView; status: PaneState; current: boolean; onOpen: () => void; toast: (text: string) => void; selection: { on: boolean; toggle: () => void } | null }) {
  const { items, layer } = useTerminalCommands(row, toast);
  const tag = rowTag(row);
  const title = row.title || t("term.untitled");
  return (
    <div className={`tp-row ${current ? "on" : ""} ${running(row) ? "" : "finished"} ${selection?.on ? "selected" : ""}`} data-terminal={row.id} data-state={status} data-env={row.env} data-stale={tag.level === "stale" ? "1" : undefined}>
      {selection && (
        <label className="tp-row-check" title={t("term.select.one", { title })}>
          <input type="checkbox" checked={selection.on} onChange={selection.toggle} aria-label={t("term.select.one", { title })} />
        </label>
      )}
      <button type="button" className="tp-row-open" aria-pressed={current} onClick={selection ? selection.toggle : onOpen} aria-label={t("term.open", { title })}>
        {!selection && <StateDot status={status} />}
        <span className="tp-row-text">
          <span className="tp-row-line">
            <span className="tp-row-title truncate">{title}</span>
            <span className="tp-row-tag" data-level={tag.level}>{tag.text}</span>
          </span>
          <span className="tp-row-cmd truncate">{rowCommand(row)}</span>
          <span className="tp-row-owner truncate">{ownerShort(row)}</span>
        </span>
      </button>
      <OverflowMenu small className="quiet tp-row-menu" contextSelector=".tp-row" label={t("term.row.menu", { title })} items={items} />
      {layer}
    </div>
  );
}

/** The terminals' share of the machine's memory as a line and a meter; a press opens the projection for the cap. */
function MemoryMeter({ load }: { load: TerminalLoad }) {
  const f = loadFigures(load);
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  if (!f.known) return null;
  const share = Math.min(100, (100 * f.terminalsNow) / f.total);
  const value = t("term.mem.value", { used: size(f.terminalsNow), total: size(f.total) });
  return (
    <div className="tp-mem" data-level={f.level}>
      <button ref={button} type="button" className="tp-mem-button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="tp-mem-head"><span className="tp-mem-label">{t("term.mem.label")}</span><span className="tp-mem-value num">{value}</span></span>
        <span className="tp-mem-track" role="meter" aria-valuemin={0} aria-valuemax={Math.round(f.total / 2 ** 30)} aria-valuenow={Math.round(f.terminalsNow / 2 ** 30)} aria-label={t("term.mem.aria")} aria-valuetext={value}>
          <span className="tp-mem-fill" style={{ width: `${f.terminalsNow > 0 ? Math.max(2, share) : 0}%` }} />
        </span>
      </button>
      {open && (
        <Popover anchor={button.current} onClose={() => setOpen(false)} label={t("term.mem.label")} side="top" width={360} className="tp-mem-pop">
          <LoadBar load={load} compact />
        </Popover>
      )}
    </div>
  );
}
