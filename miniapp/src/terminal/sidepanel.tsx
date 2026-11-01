// The Terminals workspace's side panel: about the active pane, beside the grid. Three tabs —
//
// - Files: what changed in the pane's folder, each file opening its diff in the app's diff viewer.
//   A staff member's terminal lists its worktree's changes since its session's base (the member's own
//   work), any other the folder its shell is in now against the last commit.
// - Actions: the pane's feed, filterable by kind and by failure. A shell's commands as its
//   integration marked them; a staff member's tool calls from its CLI's own transcript.
// - Panes: every terminal with its state, to focus one or put it into a place, and the layouts saved
//   on the host, to restore a whole grid on any device.
//
// The screen owns what it remembers (open, width, tab) and mounts this only while it is open; the
// workspace draws it in its `slots.side`, on a desktop only — the phone shows one pane at a time and
// has no room beside it.

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { api, type TerminalCommand, type TerminalFileDiff, type TerminalFiles, type TerminalLayout, type TerminalView as TerminalRow } from "../api";
import { EnvPill } from "../envpill";
import { useEvent } from "../events";
import { clock, duration, relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { DiffView } from "../previewparts";
import { useTurns } from "../staff/FeedView";
import { invalidate, useQuery, type Query } from "../store";
import { errorText } from "../ui";
import { MenuItem, OverflowMenu } from "../ui/dialogs";
import type { TerminalState } from "./instance";
import { ownerLine, running } from "./preview";
import {
  assign, clampSide, commandActions, filterActions, kindCounts, reopenable, restoreLayout, SIDE_TABS, SIDE_W_DEFAULT, turnActions,
  type ActionRow, type KindFilter, type Missing, type SidePrefs, type SideTab,
} from "./sidestate";
import { paneState } from "./status";
import { paneTitle, placeKey, StateDot } from "./workspace";
import { activeId, type Workspace } from "./workspacestate";
import "./sidepanel.css";

const enc = encodeURIComponent;
const FILES_POLL_MS = 30000;
const COMMANDS_POLL_MS = 5000;
const COMMANDS_LAST = 200;
const LAYOUTS_KEY = "/api/terminals/layouts";
const WIDTH_STEP = 16;

export type SidePanelProps = {
  ws: Workspace;
  update: (change: (ws: Workspace) => Workspace) => void;
  rows: TerminalRow[];
  states: Record<string, TerminalState>;
  prefs: SidePrefs;
  onPrefs: (change: (prefs: SidePrefs) => SidePrefs) => void;
  onOpen: (id: string) => void;
  onFocusRequest: (id: string) => void;
  toast: (text: string) => void;
};

function useViewport(): number {
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}

/** The active pane's feed in one shape, from whichever of its two sources it has. */
function useActions(id: string | null, row: TerminalRow | null): { rows: ActionRow[]; loaded: boolean; error: string; source: "staff" | "shell" } {
  const staffId = row?.owner.kind === "staff" && row.owner.id ? row.owner.id : null;
  const turns = useTurns(staffId ?? "", !!staffId);
  const key = id && !staffId ? `/api/terminals/${enc(id)}/commands?last=${COMMANDS_LAST}&output=0` : null;
  const commands = useQuery<{ commands: TerminalCommand[] }>(key, { pollMs: COMMANDS_POLL_MS, staleMs: 2000 });
  const { refresh } = commands;
  useEvent(["terminal.command"], (event) => {
    if (key && event.terminal_id === id) void refresh();
  }, [key, id, refresh]);
  return useMemo(() => {
    if (staffId) return { rows: turnActions(turns.turns), loaded: turns.loaded, error: turns.error, source: "staff" as const };
    const list = Array.isArray(commands.data?.commands) ? commands.data!.commands : [];
    return { rows: commandActions(list), loaded: commands.data !== undefined || !!commands.error, error: commands.data ? "" : commands.error ?? "", source: "shell" as const };
  }, [staffId, turns.turns, turns.loaded, turns.error, commands.data, commands.error]);
}

export function TerminalSidePanel({ ws, update, rows, states, prefs, onPrefs, onOpen, onFocusRequest, toast }: SidePanelProps) {
  const viewport = useViewport();
  const width = clampSide(prefs.width, viewport);
  const id = activeId(ws);
  const row = id ? rows.find((r) => r.id === id) ?? null : null;
  const filesKey = id ? `/api/terminals/${enc(id)}/files` : null;
  // The folder is read with git on every poll, and on the host each read is a line of the audit: it
  // is asked for often only while its tab is shown, and once otherwise, for the count on the tab.
  const files = useQuery<TerminalFiles>(filesKey, { pollMs: prefs.tab === "files" ? FILES_POLL_MS : 0, staleMs: prefs.tab === "files" ? 5000 : 10 * 60000 });
  const actions = useActions(id, row);
  const [missing, setMissing] = useState<{ name: string; items: Missing[] } | null>(null);

  const setTab = (tab: SideTab) => onPrefs((p) => ({ ...p, tab }));
  const counts: Record<SideTab, number | null> = {
    files: files.data ? files.data.files.length : null,
    // A program that reports no commands has no feed to count, rather than a feed of none.
    actions: actions.loaded && !(actions.error && actions.rows.length === 0) ? actions.rows.length : null,
    panes: rows.length,
  };
  const failed = actions.rows.filter((r) => r.failed).length;

  // ── the left edge: drag to resize, double-click for the default, arrows a step at a time ──────
  const drag = useRef<{ x: number; width: number } | null>(null);
  const [resizing, setResizing] = useState(false);
  const startResize = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, width };
    setResizing(true);
  };
  const moveResize = (event: ReactPointerEvent<HTMLElement>) => {
    if (!drag.current) return;
    const next = clampSide(drag.current.width + (drag.current.x - event.clientX), viewport);
    onPrefs((p) => (p.width === next ? p : { ...p, width: next }));
  };
  const endResize = () => {
    drag.current = null;
    setResizing(false);
  };
  useEffect(() => {
    if (!resizing) return;
    document.body.classList.add("tside-resizing");
    return () => document.body.classList.remove("tside-resizing");
  }, [resizing]);
  const resizeKeys = (event: ReactKeyboardEvent<HTMLElement>) => {
    const step = event.key === "ArrowLeft" ? WIDTH_STEP : event.key === "ArrowRight" ? -WIDTH_STEP : 0;
    if (!step) return;
    event.preventDefault();
    onPrefs((p) => ({ ...p, width: clampSide(width + step, viewport) }));
  };

  return (
    <div className="tside" style={{ width }} data-tab={prefs.tab}>
      <div
        className={`tside-grip ${resizing ? "on" : ""}`}
        role="separator"
        aria-orientation="vertical"
        aria-label={t("term.side.resize")}
        title={t("term.side.resize")}
        aria-valuenow={width}
        tabIndex={0}
        onPointerDown={startResize}
        onPointerMove={moveResize}
        onPointerUp={endResize}
        onPointerCancel={endResize}
        onDoubleClick={() => onPrefs((p) => ({ ...p, width: clampSide(SIDE_W_DEFAULT, viewport) }))}
        onKeyDown={resizeKeys}
      />
      <div className="tside-head">
        <div className="tside-tabs" role="tablist" aria-label={t("term.ws.panel")}>
          {SIDE_TABS.map((tab) => (
            <button key={tab} type="button" role="tab" aria-selected={prefs.tab === tab} className={`tside-tab ${prefs.tab === tab ? "on" : ""}`} data-tab={tab} onClick={() => setTab(tab)}>
              <span>{t(`term.side.tab.${tab}`)}</span>
              {counts[tab] !== null && <span className={`tside-count num ${tab === "actions" && failed ? "bad" : ""}`}>{counts[tab]}</span>}
            </button>
          ))}
        </div>
        <button type="button" className="iconbtn small flat tside-close" onClick={() => onPrefs((p) => ({ ...p, open: false }))} aria-label={t("term.side.close")} title={t("term.side.close")}>
          <Icon name="close" size={14} />
        </button>
      </div>
      {prefs.tab !== "panes" && (
        <div className="tside-pane" data-pane={id ?? ""}>
          {id ? (
            <>
              <StateDot status={paneState(row, states[id])} />
              <span className="tside-pane-title truncate">{paneTitle(id, row, states[id])}</span>
              {row && <span className="tside-pane-cwd truncate" title={row.cwd}>{row.cwd}</span>}
            </>
          ) : (
            <span className="tside-none">{t("term.side.noPane")}</span>
          )}
        </div>
      )}
      <div className="tside-body">
        {prefs.tab === "files" && (id ? <FilesTab id={id} query={files} staffId={row?.owner.kind === "staff" ? row.owner.id : null} /> : null)}
        {prefs.tab === "actions" && (id ? <ActionsTab id={id} actions={actions} /> : null)}
        {prefs.tab === "panes" && (
          <PanesTab ws={ws} update={update} rows={rows} states={states} onOpen={onOpen} onFocusRequest={onFocusRequest} toast={toast} missing={missing} setMissing={setMissing} />
        )}
      </div>
    </div>
  );
}

// ── Files ─────────────────────────────────────────────────────────────────────────────────

function splitPath(path: string): { dir: string; name: string } {
  const at = path.lastIndexOf("/");
  return at < 0 ? { dir: "", name: path } : { dir: path.slice(0, at + 1), name: path.slice(at + 1) };
}

function FilesTab({ id, query, staffId }: { id: string; query: Query<TerminalFiles>; staffId: string | null }) {
  const [open, setOpen] = useState<string | null>(null);
  const { data, error, refresh } = query;
  useEffect(() => setOpen(null), [id]);
  // A command that finished, or a staff member whose turn moved on, may have written files.
  useEvent(["terminal.command", "staff.status"], (event) => {
    if (event.terminal_id === id || (staffId && event.staff_id === staffId)) void refresh();
  }, [id, staffId, refresh]);

  if (open) return <FileDiff id={id} path={open} onBack={() => setOpen(null)} />;
  if (!data) return <div className="tside-note">{error ? errorText(error) : t("common.loading")}</div>;
  const reason = data.reason ? t(`term.side.files.${data.reason}`) : "";
  return (
    <div className="tside-files">
      <div className="tside-line">
        {data.branch && <span className="tside-branch truncate" title={data.branch}><Icon name="fork" size={12} />{data.branch}</span>}
        <span className="tside-root truncate" title={data.root}>{data.root}</span>
        <span className="grow" />
        <button type="button" className="iconbtn small flat" onClick={() => void refresh()} aria-label={t("term.side.refresh")} title={t("term.side.refresh")}>
          <Icon name="reload" size={14} />
        </button>
      </div>
      {data.source === "staff" && data.files.length > 0 && <div className="tside-sub">{t("term.side.files.staff", { base: data.base })}</div>}
      {reason ? (
        <div className="tside-note">{reason}{data.detail ? <span className="tside-detail">{data.detail}</span> : null}</div>
      ) : data.files.length === 0 ? (
        <div className="tside-note">{t("term.side.files.none")}</div>
      ) : (
        <ul className="tside-list" aria-label={t("term.side.tab.files")}>
          {data.files.map((f) => {
            const { dir, name } = splitPath(f.path);
            return (
              <li key={f.path}>
                <button type="button" className="tside-file" data-path={f.path} data-status={f.status} onClick={() => setOpen(f.path)} title={t("term.side.files.open", { path: f.path })}>
                  <span className="tside-status" data-status={f.status}>{f.status === "?" ? "U" : f.status}</span>
                  <span className="tside-file-path truncate"><span className="tside-dir">{dir}</span>{name}</span>
                  {f.added !== null && <span className="tside-add num">+{f.added}</span>}
                  {f.removed !== null && <span className="tside-del num">−{f.removed}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {data.truncated && <div className="tside-sub">{t("term.side.files.more")}</div>}
    </div>
  );
}

function FileDiff({ id, path, onBack }: { id: string; path: string; onBack: () => void }) {
  const [answer, setAnswer] = useState<TerminalFileDiff | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let stop = false;
    setAnswer(null);
    setError("");
    api.terminalFileDiff(id, path).then((a) => { if (!stop) setAnswer(a); }, (e) => { if (!stop) setError(errorText(e)); });
    return () => { stop = true; };
  }, [id, path]);
  return (
    <div className="tside-diff">
      <div className="tside-line">
        <button type="button" className="iconbtn small flat tside-back" onClick={onBack} aria-label={t("term.side.files.back")} title={t("term.side.files.back")}>
          <Icon name="back" size={14} />
        </button>
        <span className="tside-diff-path truncate" title={path}>{path}</span>
        {answer?.untracked && <span className="tside-tag">{t("term.side.files.new")}</span>}
      </div>
      <div className="tside-diff-body">
        {error ? <div className="tside-note">{error}</div>
          : !answer ? <div className="tside-note">{t("common.loading")}</div>
          : answer.reason ? <div className="tside-note">{t(`term.side.files.${answer.reason}`)}</div>
          : answer.diff.trim() ? <DiffView text={answer.diff} />
          : <div className="tside-note">{t("term.side.files.same")}</div>}
        {answer?.truncated && <div className="tside-sub">{t("term.side.files.cut")}</div>}
      </div>
    </div>
  );
}

// ── Actions ───────────────────────────────────────────────────────────────────────────────

function ActionsTab({ id, actions }: { id: string; actions: ReturnType<typeof useActions> }) {
  const [kind, setKind] = useState<KindFilter>("all");
  const [failedOnly, setFailedOnly] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { setKind("all"); setFailedOnly(false); setOpen(null); }, [id]);
  const { rows, loaded, error, source } = actions;
  const pills = kindCounts(rows);
  const failed = rows.filter((r) => r.failed).length;
  const shown = filterActions(rows, kind, failedOnly);
  const newest = rows.find((r) => r.n !== undefined)?.n ?? 0;

  if (!loaded) return <div className="tside-note">{t("common.loading")}</div>;
  if (error && rows.length === 0) return <div className="tside-note">{source === "shell" ? t("term.side.actions.unsupported") : errorText(error)}</div>;
  return (
    <div className="tside-actions">
      <div className="tside-line">
        <span className="tside-summary">{plural("term.side.actions.count", rows.length)}{failed > 0 && <> · <span className="tside-failed">{plural("term.side.actions.failed", failed)}</span></>}</span>
        <span className="tside-source">{t(`term.side.actions.from.${source}`)}</span>
      </div>
      {rows.length > 0 && (
        <div className="tside-pills" role="group" aria-label={t("term.side.actions.filter")}>
          <button type="button" className={`tside-pill ${kind === "all" ? "on" : ""}`} aria-pressed={kind === "all"} data-kind="all" onClick={() => setKind("all")}>
            {t("term.side.kind.all")} <span className="num">{rows.length}</span>
          </button>
          {pills.map((p) => (
            <button key={p.kind} type="button" className={`tside-pill ${kind === p.kind ? "on" : ""}`} aria-pressed={kind === p.kind} data-kind={p.kind} onClick={() => setKind(kind === p.kind ? "all" : p.kind)}>
              {t(`term.side.kind.${p.kind}`)} <span className="num">{p.n}</span>
            </button>
          ))}
          {failed > 0 && (
            <button type="button" className={`tside-pill failed ${failedOnly ? "on" : ""}`} aria-pressed={failedOnly} data-kind="failed" onClick={() => setFailedOnly((v) => !v)}>
              {t("term.side.actions.failedOnly")} <span className="num">{failed}</span>
            </button>
          )}
        </div>
      )}
      {rows.length === 0 ? (
        <div className="tside-note">{t(`term.side.actions.none.${source}`)}</div>
      ) : shown.length === 0 ? (
        <div className="tside-note">{t("term.side.actions.filtered")}</div>
      ) : (
        <ul className="tside-list" aria-label={t("term.side.tab.actions")}>
          {shown.map((r) => (
            <ActionItem key={r.key} id={id} row={r} newest={newest} open={open === r.key} onToggle={() => setOpen(open === r.key ? null : r.key)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function took(ms: number | null): string {
  if (ms === null) return "";
  return ms < 1000 ? t("term.side.ms", { n: ms }) : duration(ms);
}

function ActionItem({ id, row, newest, open, onToggle }: { id: string; row: ActionRow; newest: number; open: boolean; onToggle: () => void }) {
  const [output, setOutput] = useState<string | null>(null);
  const [failure, setFailure] = useState("");
  useEffect(() => {
    if (!open || row.n === undefined || output !== null) return;
    let stop = false;
    // A command's output is asked for with the commands after it: the route reads the last N.
    const last = Math.min(500, Math.max(1, newest - row.n + 1));
    api.terminalCommands(id, last, true).then((a) => {
      if (stop) return;
      const found = a.commands.find((c) => c.n === row.n);
      setOutput(found?.output ?? "");
    }, (e) => { if (!stop) setFailure(errorText(e)); });
    return () => { stop = true; };
  }, [open, id, row.n, newest, output]);
  const state = row.running ? "running" : row.failed ? "failed" : "ok";
  return (
    <li className={`tside-act ${open ? "open" : ""}`} data-state={state} data-kind={row.kind}>
      <button type="button" className="tside-act-row" onClick={onToggle} aria-expanded={open}>
        <span className="tside-time num">{row.at ? clock(row.at) : ""}</span>
        <span className="tside-kind" data-kind={row.kind}>{t(`term.side.kind.${row.kind}`)}</span>
        <span className="tside-act-title truncate" title={row.title}>{row.title}</span>
        <span className="tside-act-end num">
          {row.running ? "…" : row.failed ? <>✗{row.exit !== null ? ` ${row.exit}` : ""}</> : took(row.durationMs)}
        </span>
      </button>
      {open && (
        <div className="tside-act-more">
          <div className="tside-act-full">{row.title}</div>
          {row.detail && <div className="tside-act-detail truncate" title={row.detail}>{row.detail}{row.failed && row.durationMs !== null ? ` · ${took(row.durationMs)}` : ""}</div>}
          {row.n !== undefined && (failure ? <div className="tside-note">{failure}</div>
            : output === null ? <div className="tside-note">{t("common.loading")}</div>
            : output.trim() ? <pre className="tside-out">{output}</pre>
            : <div className="tside-note">{t("term.side.actions.noOutput")}</div>)}
        </div>
      )}
    </li>
  );
}

// ── Panes and layouts ─────────────────────────────────────────────────────────────────────

type PanesProps = Pick<SidePanelProps, "ws" | "update" | "rows" | "states" | "onOpen" | "onFocusRequest" | "toast"> & {
  missing: { name: string; items: Missing[] } | null;
  setMissing: (missing: { name: string; items: Missing[] } | null) => void;
};

function PanesTab({ ws, update, rows, states, onOpen, onFocusRequest, toast, missing, setMissing }: PanesProps) {
  const order = useMemo(() => {
    const at = (r: TerminalRow) => { const i = ws.panes.indexOf(r.id); return i >= 0 && i < ws.layout ? i : SLOTS_AFTER; };
    return [...rows].sort((a, b) => at(a) - at(b) || Number(running(b)) - Number(running(a)) || (b.created_at > a.created_at ? 1 : b.created_at < a.created_at ? -1 : 0));
  }, [rows, ws.panes, ws.layout]);
  const shown = Array.from({ length: ws.layout }, (_, i) => i);
  const put = (slot: number, tid: string) => {
    update((w) => assign(w, slot, tid));
    onFocusRequest(tid);
  };
  return (
    <div className="tside-panes">
      {missing && missing.items.length > 0 && <MissingNote missing={missing} update={update} toast={toast} onDone={() => setMissing(null)} />}
      <Layouts ws={ws} update={update} rows={rows} toast={toast} onFocusRequest={onFocusRequest} onMissing={setMissing} />
      <div className="tside-label"><span>{t("term.side.panes.all")}</span><span className="num">{rows.length}</span></div>
      {rows.length === 0 ? <div className="tside-note">{t("term.screen.empty")}</div> : (
        <ul className="tside-list" aria-label={t("term.side.panes.all")}>
          {order.map((r) => {
            const at = ws.panes.indexOf(r.id);
            const placed = at >= 0 && at < ws.layout ? at : null;
            const status = paneState(r, states[r.id]);
            return (
              <li key={r.id} className={`tside-term ${placed !== null ? "placed" : ""} ${placed === ws.active ? "on" : ""} ${running(r) ? "" : "finished"}`} data-terminal={r.id} data-state={status}>
                <button type="button" className="tside-term-open" onClick={() => onOpen(r.id)} title={t("term.side.panes.focus")}>
                  <StateDot status={status} />
                  <span className="tside-term-text">
                    <span className="tside-term-title truncate">{r.title || t("term.untitled")}</span>
                    <span className="tside-term-sub truncate">{t(`term.ws.state.${status}`)} · {ownerLine(r, null)} · <span className="tside-path">{r.cwd}</span></span>
                  </span>
                  {r.env === "host" && <EnvPill env="host" tiny />}
                </button>
                {ws.layout > 1 && running(r) ? (
                  <span className="tside-slots" role="group" aria-label={t("term.side.panes.put")}>
                    {shown.map((slot) => (
                      <button key={slot} type="button" className={`tside-slot ${placed === slot ? "on" : ""}`} aria-pressed={placed === slot} data-slot={slot} onClick={() => put(slot, r.id)} title={t("term.side.panes.putN", { n: slot + 1 })} aria-label={t("term.side.panes.putN", { n: slot + 1 })}>
                        {slot + 1}
                      </button>
                    ))}
                  </span>
                ) : placed !== null ? <kbd className="tside-key">{placeKey(placed)}</kbd> : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

const SLOTS_AFTER = 99;

function Layouts({ ws, update, rows, toast, onFocusRequest, onMissing }: Pick<SidePanelProps, "ws" | "update" | "rows" | "toast" | "onFocusRequest"> & { onMissing: (missing: { name: string; items: Missing[] }) => void }) {
  const { data, error } = useQuery<{ layouts: TerminalLayout[] }>(LAYOUTS_KEY, { staleMs: 10000 });
  const layouts = data?.layouts ?? [];
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const titles = useMemo(() => new Map(rows.map((r) => [r.id, r.title || t("term.untitled")])), [rows]);
  const clean = name.trim();
  const replacing = layouts.some((l) => l.name === clean);

  const save = async () => {
    if (!clean || busy) return;
    setBusy(true);
    try {
      const { layout, panes, col, row, active } = ws;
      await api.saveTerminalLayout(clean, { layout, panes, col, row, active });
      invalidate(LAYOUTS_KEY);
      toast(t(replacing ? "term.side.layouts.replaced" : "term.side.layouts.saved", { name: clean }));
      setName("");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const restore = (layout: TerminalLayout) => {
    const live = new Set(rows.filter(running).map((r) => r.id));
    const { ws: next, missing } = restoreLayout(layout, live);
    update(() => next);
    const focus = next.panes[next.active];
    if (focus) onFocusRequest(focus);
    onMissing({ name: layout.name, items: missing });
    toast(missing.length ? plural("term.side.layouts.missing", missing.length, { name: layout.name }) : t("term.side.layouts.restored", { name: layout.name }));
  };

  const rename = async (layout: TerminalLayout, text: string | null) => {
    setRenaming(null);
    if (!text || text === layout.name) return;
    try {
      await api.renameTerminalLayout(layout.id, text);
      invalidate(LAYOUTS_KEY);
    } catch (e) {
      toast(errorText(e));
    }
  };

  const remove = async (layout: TerminalLayout) => {
    try {
      await api.deleteTerminalLayout(layout.id);
      invalidate(LAYOUTS_KEY);
      toast(t("term.side.layouts.deleted", { name: layout.name }));
    } catch (e) {
      toast(errorText(e));
    }
  };

  return (
    <section className="tside-layouts" aria-label={t("term.side.layouts")}>
      <div className="tside-label"><span>{t("term.side.layouts")}</span>{layouts.length > 0 && <span className="num">{layouts.length}</span>}</div>
      <form className="tside-save" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <input className="tside-input" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} placeholder={t("term.side.layouts.name")} aria-label={t("term.side.layouts.name")} onKeyDown={(e) => e.stopPropagation()} />
        <button type="submit" className="btn small" disabled={!clean || busy}>{t(replacing ? "term.side.layouts.replace" : "term.side.layouts.save")}</button>
      </form>
      {error && !data && <div className="tside-note">{errorText(error)}</div>}
      {data && layouts.length === 0 && <div className="tside-sub">{t("term.side.layouts.none")}</div>}
      {layouts.length > 0 && (
        <ul className="tside-list">
          {layouts.map((layout) => {
            const menu: MenuItem[] = [
              { label: t("term.side.layouts.rename"), icon: "pen", onSelect: () => setRenaming(layout.id) },
              { label: t("term.side.layouts.delete"), icon: "trash", danger: true, onSelect: () => void remove(layout) },
            ];
            const ids = layout.workspace.panes.slice(0, layout.workspace.layout);
            return (
              <li key={layout.id} className="tside-layout" data-layout-id={layout.id}>
                <div className="tside-layout-head">
                  {renaming === layout.id ? (
                    <NameField initial={layout.name} onDone={(text) => void rename(layout, text)} />
                  ) : (
                    <span className="tside-layout-name truncate" title={layout.name}>{layout.name}</span>
                  )}
                  <span className="tside-layout-when">{relTime(layout.updated_at)}</span>
                  <button type="button" className="btn small tside-restore" onClick={() => restore(layout)}>{t("term.side.layouts.restore")}</button>
                  <OverflowMenu small className="flat" icon="vdots" label={t("term.side.layouts.menu", { name: layout.name })} items={menu} />
                </div>
                <div className="tside-chips">
                  <span className="tside-chip shape">{t("term.ws.layout.n", { n: layout.workspace.layout })}</span>
                  {ids.map((tid, i) => tid ? (
                    <span key={`${tid}-${i}`} className={`tside-chip ${titles.has(tid) ? "" : "gone"}`} title={layout.terminals[tid]?.cwd ?? tid}>{titles.get(tid) ?? layout.terminals[tid]?.title ?? tid}</span>
                  ) : (
                    <span key={`empty-${i}`} className="tside-chip empty">{t("term.ws.empty")}</span>
                  ))}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function NameField({ initial, onDone }: { initial: string; onDone: (text: string | null) => void }) {
  const [text, setText] = useState(initial);
  const done = useRef(false);
  const finish = useCallback((value: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(value);
  }, [onDone]);
  return (
    <input
      className="tside-input"
      autoFocus
      value={text}
      maxLength={80}
      aria-label={t("term.side.layouts.rename")}
      onChange={(e) => setText(e.target.value)}
      onFocus={(e) => e.currentTarget.select()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") finish(text.trim());
        if (e.key === "Escape") finish(null);
      }}
      onBlur={() => finish(text.trim())}
    />
  );
}

/** After a restore: the places left empty because their terminal had ended or gone, and a way to
 *  open a plain shell again in the same folder, as the same owner, into the same place. */
function MissingNote({ missing, update, toast, onDone }: { missing: { name: string; items: Missing[] }; update: SidePanelProps["update"]; toast: (text: string) => void; onDone: () => void }) {
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const reopen = async (item: Missing) => {
    const info = item.info!;
    try {
      const fresh = await api.createTerminal({
        env: info.env,
        owner_kind: info.owner_kind,
        ...(info.owner_id ? { owner_id: info.owner_id } : {}),
        ...(info.project_id ? { project_id: info.project_id } : {}),
        cwd: info.cwd,
        title: info.title || undefined,
        sandbox: info.sandbox,
      });
      invalidate("/api/terminals");
      update((w) => assign(w, item.slot, fresh.id));
      setOpened((s) => new Set(s).add(item.id));
    } catch (e) {
      toast(errorText(e));
    }
  };
  return (
    <div className="tside-missing" role="status">
      <div className="tside-missing-head">
        <Icon name="alert" size={14} />
        <span className="grow">{plural("term.side.layouts.missingNote", missing.items.length, { name: missing.name })}</span>
        <button type="button" className="iconbtn small flat" onClick={onDone} aria-label={t("term.side.dismiss")} title={t("term.side.dismiss")}><Icon name="close" size={12} /></button>
      </div>
      <ul className="tside-list">
        {missing.items.map((item) => (
          <li key={item.id} className="tside-missing-row" data-terminal={item.id}>
            <kbd className="tside-key">{placeKey(item.slot)}</kbd>
            <span className="tside-term-text">
              <span className="tside-term-title truncate">{item.info?.title || item.id}</span>
              {item.info && <span className="tside-term-sub truncate"><span className="tside-path">{item.info.cwd}</span></span>}
            </span>
            {opened.has(item.id) ? <span className="tside-tag">{t("term.side.layouts.reopened")}</span>
              : reopenable(item.info) ? <button type="button" className="btn small" onClick={() => void reopen(item)}>{t("term.side.layouts.reopen")}</button>
              : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
