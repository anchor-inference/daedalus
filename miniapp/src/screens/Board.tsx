import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, Project, SessionList, SessionSummary } from "../api";
import { Skeleton, copyText } from "../ui/components";
import { Chip, ChipBar, EmptyState, TopBar } from "../ui/phone";
import { Button, Combobox, Field, IconButton, Menu, MenuRow, Popover, Row, Segmented, Select, Sheet, type MenuItem } from "../ui/primitives";
import { colourOf, initials } from "../drawer";
import { useMedia } from "../shell";
import { StaffAvatar } from "../team/parts";
import { relTime } from "../format";
import { Icon, type IconName } from "../icons";
import { useEvent, useStreamUp } from "../events";
import { navigate, pathFor, projectPagePath } from "../router";
import { PageHeader, screenTitle } from "../ui/index";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import { plural, t } from "../i18n";
import { closeItems } from "../board/operatorClose";
import { BoardColumns, type BoardColumn, type ChatInfo } from "../board/BoardColumns";
import { TaskPane, TaskSheet } from "../board/TaskPane";
import { useTaskWrites, type TaskWrites } from "../board/useTaskWrites";
import { assigneeName, byUrgency, columnLabel, FINISHED, isFinished, movesFor, OPEN_COLUMNS, type BoardTask, type Status } from "../board/tasks";
import { byProject, filtered, scopeFrom, scopeKey, scopeQuery, searched, storedGrouping, type Grouping, type Scope } from "./boardview";

/** The grouping the operator chose, kept per browser and per form: a wide window starts from the
 *  status columns and a phone from its projects, as each did before there was a choice, and a webview
 *  without site data simply starts from that again. */
function useGrouping(key: string, fallback: Grouping): [Grouping, (value: Grouping) => void] {
  const [grouping, setGrouping] = useState<Grouping>(() => storedGrouping(() => localStorage.getItem(key), fallback));
  const choose = (value: Grouping) => {
    setGrouping(value);
    try { localStorage.setItem(key, value); } catch { /* the choice lasts this load */ }
  };
  return [grouping, choose];
}

/** The chats the board can name and narrow to: the operator's own, not a helper of one. */
function useChats(): { list: SessionSummary[]; info: ChatInfo } {
  const { data } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  return useMemo(() => {
    const sessions = data?.sessions ?? [];
    const byId = new Map(sessions.map((s) => [s.id, s]));
    return {
      list: sessions.filter((s) => !s.metadata?.subagent_of && !s.archived).sort((a, b) => Date.parse(b.last_message_at) - Date.parse(a.last_message_at)),
      info: {
        title: (id) => byId.get(id)?.title,
        running: (id) => byId.get(id)?.status === "running",
        waiting: (id) => { const s = byId.get(id); return Boolean(s && (s.needs_attention || s.status === "waiting")); },
      },
    };
  }, [data]);
}

export function BoardScreen({ toast, onOpen, selected, project, projects = [], query }: {
  toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null; project?: Project | null; projects?: Project[]; query?: URLSearchParams;
}) {
  const phone = useMedia("(max-width: 1023px)");
  const scope = scopeFrom(query ?? new URLSearchParams(), project?.id ?? null);
  const [showDone, setShowDone] = useState(false);
  const [needs, setNeeds] = useState(false);
  const [search, setSearch] = useState<string | null>(null);
  const [grouping, setGrouping] = useGrouping("board.grouping", "status");
  const [phoneGrouping, setPhoneGrouping] = useGrouping("board.grouping.phone", "project");
  const live = useStreamUp();
  const key = scopeKey(scope);
  const { data: tasks, error, loading, refresh } = useQuery<BoardTask[]>(key, { pollMs: live ? 60000 : 20000, staleMs: 5000 });
  // A card changed anywhere (an agent's tool, another window, a member's report) is read again at once;
  // the poll is only the fallback for a window without the event stream.
  useEvent(["task."], () => invalidate("/api/board"), []);
  const chats = useChats();
  const [creating, setCreating] = useState<null | { status?: Status }>(null);
  const reload = () => { refresh(); invalidate("/api/board"); };
  const writes = useTaskWrites(reload);
  const agentProjects = projects.filter((p) => !p.settings.orchestrator?.enabled && p.system !== "dispatcher" && !p.settings.archived && !p.settings.ephemeral);
  const go = (next: Scope, task: string | null = null) => navigate(pathFor("board", task, scopeQuery(next)), { replace: true });
  const openTask = (id: string) => navigate(pathFor("board", id, scopeQuery(scope)));
  const closeTask = () => navigate(pathFor("board", null, scopeQuery(scope)), { replace: true });

  const every = tasks ?? [];
  const open = selected ? every.find((task) => task.id === selected) ?? null : null;
  // A link to a finished task widens the filter so the task can be shown; a link to a card outside the
  // scope in view widens the scope, so the card opens rather than a blank pane.
  useEffect(() => {
    if (!selected || !tasks) return;
    if (!open && scope.kind !== "all") go({ kind: "all" }, selected);
    else if (open && isFinished(open) && !showDone) setShowDone(true);
  }, [selected, tasks, open, showDone, scope.kind]); // eslint-disable-line react-hooks/exhaustive-deps

  const openCards = every.filter((task) => !isFinished(task));
  const finishedCount = every.length - openCards.length;
  const needsCount = filtered(openCards, true).length;
  const shown = searched(filtered(showDone ? every : openCards, needs), search ?? "");
  const pane = open && {
    task: open, chats: chats.info, writes, onClose: closeTask, onChanged: reload, onOpenSession: onOpen, onOpenTask: openTask, others: every, toast,
    onRemoved: closeTask,
  };

  if (phone) return <PhoneBoardView tasks={tasks} error={error} loading={loading} refresh={refresh} showDone={showDone} setShowDone={setShowDone} project={project ?? null} grouping={phoneGrouping} setGrouping={setPhoneGrouping}
    chats={chats.info} writes={writes} onNew={() => setCreating({})} onOpenTask={openTask} onOpenSession={onOpen} toast={toast} reload={reload}
    sheets={<>
      {creating && <NewTaskSheet scope={scope} chats={chats.list} onClose={() => setCreating(null)} onCreated={() => { setCreating(null); reload(); }} toast={toast} />}
      {pane && <TaskSheet {...pane} />}
    </>} />;

  const columns: BoardColumn[] = grouping === "project"
    ? byProject(shown).map((group) => ({ key: group.key, name: group.projectId ? group.name : t("pbph.noproject"), tasks: group.items }))
    : [
      ...OPEN_COLUMNS.map((status) => ({
        key: status, name: columnLabel(status), drop: status, tasks: shown.filter((task) => task.status === status).sort(byUrgency),
        onAdd: status === "todo" ? () => setCreating({}) : undefined,
      })),
      ...(showDone ? [{ key: "finished", name: t("board.finished"), drop: "dropped" as Status, tasks: shown.filter(isFinished).sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)) }] : []),
    ];
  return (
    <div className="board-page">
      <section className="board-area" aria-label={t("board.region")}>
        <PageHeader
          title={screenTitle("board")}
          filters={<>
            <Segmented label={t("board.scope")} size="md" className="board-scope" value={scope.kind}
              options={[
                { id: "all", label: t("board.scope.all") },
                { id: "project", label: t("board.scope.project"), disabled: agentProjects.length === 0 && scope.kind !== "project" },
                { id: "chat", label: t("board.scope.chat"), disabled: chats.list.length === 0 && scope.kind !== "chat" },
              ]}
              onChange={(kind) => go(kind === "all" ? { kind } : kind === "project" ? { kind, id: project?.id ?? openTaskProject(every, agentProjects) ?? agentProjects[0].id } : { kind, id: chats.list[0].id })} />
            <ScopePick scope={scope} projects={agentProjects} chats={chats.list} onPick={go} />
          </>}
          actions={<>
            <Menu label={t("board.group")} trigger={<button type="button" className="board-group-btn">{t(`board.group.${grouping}`)}<Icon name="chevron" size={14} /></button>}
              items={(["status", "project"] as Grouping[]).map((value) => ({ label: t(`board.group.${value}`), checked: grouping === value, onSelect: () => setGrouping(value) }))} />
            {search === null
              ? <IconButton icon="search" label={t("board.search")} onClick={() => setSearch("")} />
              : <SearchField value={search} onChange={setSearch} onClose={() => setSearch(null)} />}
            <Button variant="primary" icon="plus" className="board-new-btn" aria-label={t("board.new")} title={t("board.new")} onClick={() => setCreating({})}><span className="board-new-label">{t("board.new")}</span></Button>
          </>}
        />
        <div className="board-filters" role="group" aria-label={t("pboard.filter")}>
          <button type="button" className="board-filter" aria-pressed={!showDone} data-filter="open" onClick={() => setShowDone(false)}>{t("board.filter.open")}</button>
          <button type="button" className="board-filter" aria-pressed={showDone} data-filter="done" onClick={() => setShowDone(true)}>{t("board.filter.done")} <span className="n">{finishedCount}</span></button>
          <button type="button" className="board-filter needs" aria-pressed={needs} data-filter="needs" onClick={() => setNeeds(!needs)}>
            <span className="kanban-dot attention" aria-hidden="true" />{t("pboard.col.needs")} <span className="n">{needsCount}</span>
          </button>
          <span className="grow" />
          {tasks && <span className="board-summary">{plural("board.open.count", openCards.length)}</span>}
        </div>
        <div className="board-scroll">
          {loading && !tasks && !error && <Skeleton rows={4} />}
          {error && !tasks && <div className="empty"><b>{t("board.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
          {tasks && tasks.length === 0 && (
            <div className="empty">
              <b>{t(scope.kind === "chat" ? "board.empty.chat" : "board.empty")}</b>
              <div>{t("board.empty.sub")}</div>
              <button className="btn primary" onClick={() => setCreating({})}><Icon name="plus" size={16} /> {t("board.new")}</button>
            </div>
          )}
          {tasks && tasks.length > 0 && shown.length === 0 && (
            <div className="empty">
              <b>{t(search ? "board.none.search" : needs ? "pboard.none.needs" : "board.none.open")}</b>
              <button className="btn" onClick={() => { setNeeds(false); setSearch(null); setShowDone(true); }}>{t("pbph.all")}</button>
            </div>
          )}
          {tasks && shown.length > 0 && (
            <div data-grouping={grouping}>
              <BoardColumns columns={columns} scope={grouping === "project" ? "none" : scope.kind} selected={open?.id ?? null} onOpen={(task) => openTask(task.id)} chats={chats.info}
                canMove={(task) => (writes.blocked(task) ? [] : movesFor(task))} onMove={(task, status) => void writes.move(task, status, "card")} refusal={writes.refusal} />
            </div>
          )}
        </div>
      </section>
      {pane && <TaskPane key={pane.task.id} {...pane} />}
      {creating && <NewTaskSheet scope={scope} chats={chats.list} onClose={() => setCreating(null)} onCreated={() => { setCreating(null); reload(); }} toast={toast} />}
    </div>
  );
}

/** The project of the card most recently touched, for a first press of "Project" with no lens. */
function openTaskProject(tasks: BoardTask[], projects: Project[]): string | null {
  const ids = new Set(projects.map((p) => p.id));
  const recent = [...tasks].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).find((task) => task.project_id && ids.has(task.project_id));
  return recent?.project_id ?? null;
}

type Pick = { kind: "all" } | { kind: "project"; id: string; name: string } | { kind: "chat"; id: string; name: string; project: string };

/** Which board is in view, named, and the list to change it from: every project and chat, a project,
 *  or one chat. Orchestrated projects are not offered: their boards are the orchestration mode's. */
function ScopePick({ scope, projects, chats, onPick }: { scope: Scope; projects: Project[]; chats: SessionSummary[]; onPick: (scope: Scope) => void }) {
  const anchor = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const current = scope.kind === "project" ? projects.find((p) => p.id === scope.id)?.name : scope.kind === "chat" ? chats.find((s) => s.id === scope.id)?.title : null;
  const name = scope.kind === "all" ? t("board.scope.everything") : current ?? t(scope.kind === "chat" ? "board.of.gone" : "board.scope.project");
  const icon: IconName = scope.kind === "all" ? "board" : scope.kind === "project" ? "folder" : "chat";
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const hit = (text: string) => words.every((w) => text.toLowerCase().includes(w));
  const groups = [
    { label: t("board.scope.all"), items: [{ kind: "all" } as Pick].filter(() => hit(t("board.scope.everything"))) },
    { label: t("board.scope.projects"), items: projects.filter((p) => hit(p.name)).map((p): Pick => ({ kind: "project", id: p.id, name: p.name })) },
    { label: t("board.scope.chats"), items: chats.filter((s) => hit(s.title)).slice(0, 40).map((s): Pick => ({ kind: "chat", id: s.id, name: s.title, project: s.project })) },
  ].filter((group) => group.items.length > 0);
  const label = (item: Pick) => (item.kind === "all" ? t("board.scope.everything") : item.name);
  return (
    <>
      <button ref={anchor} type="button" className="board-scope-pick" aria-haspopup="dialog" aria-expanded={open} aria-label={t("board.scope.shown", { name })} title={t("board.scope.choose")} onClick={() => setOpen(!open)}>
        <Icon name={icon} size={16} />
        <span className="truncate">{name}</span>
        <Icon name="chevron" size={14} />
      </button>
      {open && (
        <Popover anchor={anchor.current} onClose={() => { setOpen(false); setQuery(""); }} label={t("board.scope.choose")} width={340} side="bottom" className="board-scope-pop">
          <Combobox inline autoFocus label={t("board.scope.find")} placeholder={t("board.scope.find")} groups={groups} items={[]} query={query} onQuery={setQuery} toLabel={(item) => `${item.kind}:${item.kind === "all" ? "" : item.id}:${label(item)}`}
            empty={t("board.scope.nothing")}
            render={(item) => (
              <span className="board-scope-row">
                <Icon name={item.kind === "all" ? "board" : item.kind === "project" ? "folder" : "chat"} size={16} />
                <span className="truncate">{label(item)}</span>
                {item.kind === "chat" && item.project && <span className="sub truncate">{item.project}</span>}
              </span>
            )}
            onChoose={(item) => { setOpen(false); setQuery(""); onPick(item.kind === "all" ? { kind: "all" } : { kind: item.kind, id: item.id }); }} />
        </Popover>
      )}
    </>
  );
}

function SearchField({ value, onChange, onClose }: { value: string; onChange: (value: string) => void; onClose: () => void }) {
  return (
    <label className="board-search">
      <Icon name="search" size={16} />
      <input autoFocus type="search" value={value} aria-label={t("board.search")} placeholder={t("board.search")} onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); } }}
        onBlur={() => { if (!value.trim()) onClose(); }} />
    </label>
  );
}

type PhoneBoardProps = {
  tasks: BoardTask[] | null | undefined; error: string | null | undefined; loading: boolean; refresh: () => void; showDone: boolean; setShowDone: (v: boolean) => void; project: Project | null;
  grouping: Grouping; setGrouping: (value: Grouping) => void; chats: ChatInfo; writes: TaskWrites; reload: () => void;
  onNew: () => void; onOpenTask: (id: string) => void; onOpenSession: (id: string) => void; toast: (text: string) => void; sheets: ReactNode;
};

/**
 * The board of every project on a phone: chips for the columns over one list, grouped by project with
 * the way into each project's own board at its head, and the tasks of no project last. A task's
 * commands (open, move, its session, copy, archive) are its long-press sheet; what the list shows
 * (open or with finished, the project lens) is behind the options glyph, so the bar keeps two glyphs.
 */
function PhoneBoardView({ tasks, error, loading, refresh, showDone, setShowDone, project, grouping, setGrouping, chats, writes, reload, onNew, onOpenTask, onOpenSession, toast, sheets }: PhoneBoardProps) {
  const [filter, setFilter] = useState<Status | "finished" | "needs" | null>(null);
  const [options, setOptions] = useState(false);
  const all = tasks ?? [];
  const needsCount = filtered(all.filter((task) => !isFinished(task)), true).length;
  const inColumn = (task: BoardTask) => filter === null ? true : filter === "needs" ? Boolean(task.needs_you) : filter === "finished" ? isFinished(task) : task.status === filter;
  const shown = all.filter((task) => (showDone || !isFinished(task)) && inColumn(task)).sort(byUrgency);
  const ordered = byProject(shown).map((group) => ({ id: group.projectId, name: group.name, items: group.items }));
  const byStatus = [...OPEN_COLUMNS, ...FINISHED].map((status) => ({ status, items: shown.filter((task) => task.status === status) })).filter((group) => group.items.length > 0);
  const columns = [...OPEN_COLUMNS, ...(showDone ? ["finished" as const] : [])].map((column) => ({ column, count: all.filter((task) => column === "finished" ? isFinished(task) : task.status === column).length })).filter((c) => c.count > 0);
  const openCount = all.filter((task) => !isFinished(task)).length;
  const row = (task: BoardTask) => {
    const done = task.checklist.filter((c) => c.done).length;
    const pct = task.checklist.length ? Math.round((100 * done) / task.checklist.length) : 0;
    const chat = task.origin_session_id ?? task.session_id;
    const owner = chat ? chats.title(chat) : undefined;
    const blocked = writes.blocked(task);
    const open = () => onOpenTask(task.id);
    const actions: MenuItem[] = [
      { label: t("common.open"), icon: "expand", onSelect: open },
      ...movesFor(task).map((status) => ({ label: t("pbph.moveto", { column: columnLabel(status) }), icon: "forward" as const, disabled: blocked, onSelect: () => void writes.move(task, status, "card").then((ok) => { if (!ok && writes.refusal) toast(writes.refusal.text); }) })),
      ...(chat ? [{ label: owner ? t("board.open.owner", { name: owner }) : t("board.open.session"), icon: "bots" as const, onSelect: () => onOpenSession(chat) }] : []),
      ...(task.project_id ? [{ label: t("board.on.project", { name: task.project_name ?? task.project_id }), icon: "board" as const, onSelect: () => navigate(projectPagePath(task.project_id!, "board", { task: task.id })) }] : []),
      { label: t("board.copyid"), icon: "copy", ticks: true, onSelect: async () => { const ok = await copyText(task.id); if (!ok) toast(task.id); return ok; } },
      ...closeItems(task, blocked, { onDone: reload, toast }),
    ];
    return (
      <Row key={task.id} className={`ph-task p${Math.min(task.priority, 4)} ${task.status}`} data={{ task: task.id }} title={task.title} label={task.title}
        meta={<>
          {task.assignee?.kind === "staff" && <StaffAvatar name={task.assignee.name} color={task.assignee.color} size="small" />}
          {task.needs_you && <span className="ph-pill warn" data-needs={task.needs_you}>{t(`board.needs.${task.needs_you}`)}</span>}
          <span className={`ph-ell ${task.status === "doing" ? "running" : task.status === "blocked" ? "waiting" : ""}`}>{[columnLabel(task.status), owner ?? assigneeName(task), relTime(task.updated_at)].filter(Boolean).join(" · ")}</span>
        </>}
        trail={<>
          {task.priority <= 2 && <span className={`ph-pill ${task.priority === 1 ? "bad" : ""}`}>P{task.priority}</span>}
          {task.checklist.length > 0 && <span className="ph-progress"><span className={`ph-bar ${pct === 100 ? "ok" : ""}`} style={{ ["--v" as string]: pct }}><i /></span><span className="num">{done}/{task.checklist.length}</span></span>}
        </>}
        actions={actions} preview={{ title: task.title, meta: [columnLabel(task.status), task.project_name, t("pbph.id", { id: task.id })].filter(Boolean).join(" · ") }}
        onOpen={open} />
    );
  };
  return (
    <div className="ph-page ph-board ph-gboard">
      <TopBar center title={screenTitle("board")} sub={undefined}
        actions={<>
          <IconButton icon="sliders" label={t("pbph.options")} onClick={() => setOptions(true)} className="ph-ib" />
          <IconButton icon="plus" label={t("board.new")} onClick={onNew} className="ph-ib" />
        </>} />
      {tasks && tasks.length > 0 && (
        <ChipBar label={t("pboard.filter")}>
          <Chip on={filter === null} count={showDone ? all.length : openCount} onClick={() => setFilter(null)}>{t("pbph.all")}</Chip>
          {needsCount > 0 && <Chip on={filter === "needs"} tone="warn" count={needsCount} onClick={() => setFilter(filter === "needs" ? null : "needs")}>{t("pboard.col.needs")}</Chip>}
          {columns.map(({ column, count }) => (
            <Chip key={column} on={filter === column} tone={column === "blocked" ? "warn" : undefined} count={count} onClick={() => setFilter(filter === column ? null : column)}>{column === "finished" ? t("board.finished") : columnLabel(column)}</Chip>
          ))}
        </ChipBar>
      )}
      <div className="ph-page-body list">
        {project && <div className="ph-page-pad"><span className="ph-pill">{t("board.lens", { name: project.name })}</span></div>}
        {loading && !tasks && !error && [0, 1, 2, 3].map((i) => <div key={i} className="ph-board-sk-row"><span className="ph-sk" style={{ height: 14, width: `${60 - i * 8}%` }} /><span className="ph-sk" style={{ height: 11, width: "40%" }} /></div>)}
        {error && !tasks && <EmptyState icon="alert" tone="bad" title={t("board.error")} body={error} action={<button type="button" className="ph-btn primary" onClick={refresh}>{t("common.retry")}</button>} />}
        {tasks && tasks.length === 0 && <EmptyState icon="board" title={t("board.empty")} body={t("board.empty.sub")} action={<button type="button" className="ph-btn accent" onClick={onNew}><Icon name="plus" size={18} />{t("board.new")}</button>} />}
        {tasks && tasks.length > 0 && shown.length === 0 && <EmptyState icon="board" title={t("pbph.filtered")} action={<button type="button" className="ph-btn" onClick={() => setFilter(null)}>{t("pbph.all")}</button>} />}
        {grouping === "status" && byStatus.map((group) => (
          <section key={group.status} className="ph-gboard-group" data-status={group.status}>
            <div className="ph-gboard-head">
              <b className="truncate">{columnLabel(group.status)}</b>
              <span className="ph-gboard-n">{group.items.length}</span>
            </div>
            {group.items.map(row)}
          </section>
        ))}
        {grouping === "project" && ordered.map((group) => (
          <section key={group.id ?? "none"} className="ph-gboard-group" data-project={group.id ?? ""}>
            {group.id ? (
              <button type="button" className="ph-gboard-head" onClick={() => navigate(projectPagePath(group.id!, "board"))} aria-label={t("board.on.project", { name: group.name })}>
                <span className="ph-avatar" style={{ ["--c" as string]: colourOf(group.id) }} aria-hidden>{initials(group.name)}</span>
                <b className="truncate">{group.name}</b>
                <span className="ph-gboard-n">{group.items.length}</span>
                <Icon name="forward" size={18} />
              </button>
            ) : (
              <div className="ph-gboard-head">
                <span className="ph-avatar" aria-hidden><Icon name="bots" size={18} /></span>
                <b className="truncate">{t("pbph.noproject")}</b>
                <span className="ph-gboard-n">{group.items.length}</span>
              </div>
            )}
            {group.items.map(row)}
          </section>
        ))}
      </div>
      {options && (
        <Sheet title={t("pbph.options")} onClose={() => setOptions(false)} className="ph-sheet ph-board-options">
          <div className="ph-gl">{t("pbph.show")}</div>
          <div className="ph-board-opt"><Segmented label={t("pbph.show")} value={showDone ? "done" : "open"} onChange={(v) => setShowDone(v === "done")}
            options={[{ id: "open", label: t("board.filter.open") }, { id: "done", label: t("board.filter.done") }]} className="ph-seg" /></div>
          <div className="ph-gl">{t("board.group")}</div>
          <div className="ph-board-opt"><Segmented label={t("board.group")} value={grouping} onChange={(v) => setGrouping(v === "status" ? "status" : "project")}
            options={[{ id: "project", label: t("board.group.project") }, { id: "status", label: t("board.group.status") }]} className="ph-seg" /></div>
          {project && !project.system && !project.settings.ephemeral && <MenuRow icon="board" label={t("board.lens.project")} onSelect={() => { setOptions(false); navigate(projectPagePath(project.id, "board")); }} />}
          <MenuRow icon="reload" label={t("pboard.launch.refresh")} onSelect={() => { refresh(); setOptions(false); }} />
        </Sheet>
      )}
      {sheets}
    </div>
  );
}

/**
 * A new card. Its words are the pane's two blocks, Description and Acceptance, then its checklist; who
 * does it, when it is due and how urgent it is; and the chat whose board it goes on. A chat in a
 * project posts to that project's board, since the board of every chat refuses a project's chat.
 */
function NewTaskSheet({ scope, chats, onClose, onCreated, toast }: { scope: Scope; chats: SessionSummary[]; onClose: () => void; onCreated: () => void; toast: (t: string) => void }) {
  const offline = useOffline();
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const collectionRevision = useRef<{ key: string; value: number } | null>(null);
  const [form, setForm] = useState({ title: "", description: "", acceptance: "", checklist: "", priority: 3, due: "", you: false, session_id: scope.kind === "chat" ? scope.id : "" });
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const chat = chats.find((s) => s.id === form.session_id);
  const projectId = chat?.project_id || (scope.kind === "project" ? scope.id : "");
  const options = [{ id: "", label: t("board.board.any") }, ...chats.filter((s) => scope.kind !== "project" || s.project_id === scope.id).slice(0, 60).map((s) => ({ id: s.id, label: s.title }))];
  async function create() {
    if (offline) { setRefused(t("result.block.unconfirmed")); return; }
    setBusy(true);
    try {
      const revisionsPath = projectId ? `/api/control/revisions?project=${encodeURIComponent(projectId)}` : "/api/control/revisions";
      if (collectionRevision.current?.key !== revisionsPath) {
        const revisions = await api.get<{ collection_revision: number }>(revisionsPath);
        if (!Number.isInteger(revisions.collection_revision)) { setRefused(t("result.block.unconfirmed")); return; }
        collectionRevision.current = { key: revisionsPath, value: revisions.collection_revision };
      }
      const body = {
        title: form.title.trim(),
        acceptance: form.acceptance.trim(),
        priority: form.priority,
        checklist: form.checklist.split("\n").map((l) => l.trim()).filter(Boolean),
        ...(form.description.trim() ? { brief: { objective: form.description.trim() } } : {}),
        ...(form.due ? { due_at: form.due } : {}),
        ...(form.you ? { assignee_operator: true } : {}),
        session_id: form.session_id || null,
        expected_collection_revision: collectionRevision.current.value,
      };
      const fingerprint = JSON.stringify(body);
      if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
      await api.post(projectId ? `/api/projects/${encodeURIComponent(projectId)}/board` : "/api/board", { ...body, client_operation_id: operation.current.id });
      operation.current = null;
      collectionRevision.current = null;
      onCreated();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { operation.current = null; collectionRevision.current = null; }
      setRefused(errorText(e));
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("board.new")} onClose={onClose} size="md" className="board-new"
      footer={<>
        <Button variant="quiet" onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" busy={busy} disabled={offline || !form.title.trim()} onClick={() => void create()}>{t("common.create")}</Button>
      </>}>
      <Field label={t("board.title")} autoFocus value={form.title} maxLength={200} onChange={(e) => setForm({ ...form, title: e.target.value })} />
      <Field multiline label={t("board.description")} rows={3} maxLength={4000} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      <Field multiline label={t("board.acceptance.label")} rows={2} maxLength={2000} value={form.acceptance} onChange={(e) => setForm({ ...form, acceptance: e.target.value })} />
      <Field multiline label={t("board.checklist.label")} rows={3} value={form.checklist} onChange={(e) => setForm({ ...form, checklist: e.target.value })} />
      <div className="board-new-row">
        <span className="field-label">{t("board.priority")}</span>
        <Segmented label={t("board.priority")} value={String(form.priority)} onChange={(v) => setForm({ ...form, priority: Number(v) })}
          options={[1, 2, 3, 4, 5].map((p) => ({ id: String(p), label: `P${p}` }))} />
      </div>
      <div className="board-new-row">
        <span className="field-label">{t("board.kv.assignee")}</span>
        <Segmented label={t("board.kv.assignee")} value={form.you ? "you" : "agent"} onChange={(v) => setForm({ ...form, you: v === "you" })}
          options={[{ id: "agent", label: t("board.who.agent.full") }, { id: "you", label: t("board.who.you.full") }]} />
      </div>
      <Field label={t("board.kv.due")} type="date" value={form.due} onChange={(e) => setForm({ ...form, due: e.target.value })} />
      <div className="board-new-row">
        <span className="field-label">{t("board.kv.chat")}</span>
        <Select label={t("board.kv.chat")} size="md" value={form.session_id} options={options} onChange={(v) => setForm({ ...form, session_id: v })} />
      </div>
      <div className="sub">{t("board.board.hint")}</div>
      {refused && <div className="task-refusal" role="alert">{refused}</div>}
    </Sheet>
  );
}
