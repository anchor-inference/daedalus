// The Terminals screen on a phone: one line of capacity under the bar (the whole picture is a tap
// away, in a sheet), the filter chips, and the cards by project. A card opens on a tap; its commands
// are behind a long press; a staff member's card that waits for permission answers it in a sheet.
// The desktop keeps Terminals.tsx; both read the same listing and open terminals the same way.
// A project's Terminals tab draws its cards with the same TerminalCard (terminal/phonecard.tsx).

import { useMemo, useState, type ReactNode } from "react";
import type { Project, SessionList, TerminalEnv, TerminalEnvName, TerminalList, TerminalLoad } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { size } from "../loadbar";
import { loadFigures } from "../machineload";
import { navigate, pathFor } from "../router";
import { peek, useQuery } from "../store";
import { FILTERS, groupTerminals, headerCounts, matches, type TerminalFilter } from "../terminal/preview";
import { TerminalCard } from "../terminal/phonecard";
import { Banner, BottomSheet, Chip, ChipBar, EmptyState, IconButton, RowSkeleton, SectionHeader, TopBar } from "../ui/phone";
import { containerFolder, LIST_KEY, openFreeTerminal } from "./Terminals";

const LOAD_POLL_MS = 10000;

export function PhoneTerminals({ toast, project, projects }: { toast: (text: string) => void; project: string; projects: Project[] }) {
  const [filter, setFilter] = useState<TerminalFilter>("all");
  const [sheet, setSheet] = useState<"new" | "capacity" | null>(null);
  const [pollMs, setPollMs] = useState(3000);
  const list = useQuery<TerminalList>(LIST_KEY, { pollMs, staleMs: 1000 });
  const load = useQuery<TerminalLoad>("/api/terminals/load", { pollMs: LOAD_POLL_MS, staleMs: 3000 });
  const envs = list.data?.envs ?? [];
  const wanted = Math.max(1000, envs.find((e) => e.available)?.preview_poll_ms ?? 3000);
  if (wanted !== pollMs) setPollMs(wanted);
  const rows = useMemo(() => list.data?.terminals ?? [], [list.data]);
  const counts = headerCounts(rows);
  const shown = rows.filter((r) => matches(r, filter));
  const groups = groupTerminals(shown, projects);
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects]);
  const lens = projects.find((p) => p.id === project) ?? null;
  const anyEnv = envs.some((e) => e.available);
  const container = envs.find((e) => e.env === "container");
  const host = envs.find((e) => e.env === "host");
  // No service answering at all is its own page: nothing here can work until it is installed.
  const missing = !!list.data && envs.length > 0 && !anyEnv;

  let body: ReactNode;
  if (list.error && !list.data) {
    body = <EmptyState icon="alert" tone="bad" title={t("term.screen.error")} body={list.error} action={<button type="button" className="ph-btn primary" onClick={() => void list.refresh()}>{t("common.retry")}</button>} />;
  } else if (!list.data) {
    body = <RowSkeleton rows={4} lead="none" />;
  } else if (missing && rows.length === 0) {
    body = <EmptyState icon="grid" title={t("term.unavailable.container")} body={t("ph.term.missing")} action={<button type="button" className="ph-btn primary" onClick={() => navigate(pathFor("settings", "components"))}>{t("ph.term.components")}</button>} />;
  } else if (rows.length === 0) {
    body = <EmptyState icon="terminal" title={t("term.screen.empty")} body={t("term.screen.empty.sub")} action={<button type="button" className="ph-btn accent" onClick={() => setSheet("new")}><Icon name="plus" size={18} />{t("term.new")}</button>} />;
  } else {
    body = (
      <>
        {!missing && host && !host.available && container?.available && (
          <div className="ph-page-pad"><Banner tone="warn" icon="lock" sub={t("ph.term.hostMissing")}>{t("term.unavailable.host")}</Banner></div>
        )}
        {shown.length === 0 && <EmptyState icon="terminal" title={t("term.screen.none")} action={<button type="button" className="ph-btn" onClick={() => setFilter("all")}>{t("ph.inbox.showall")}</button>} />}
        {groups.map((group) => (
          <section key={group.key || "none"} data-group={group.key || "none"}>
            <SectionHeader count={group.rows.length}>{group.name ?? t("term.screen.noproject")}</SectionHeader>
            <div className="ph-tcards">
              {group.rows.map((row) => <TerminalCard key={row.id} row={row} projectName={row.project_id ? names.get(row.project_id) ?? null : null} toast={toast} />)}
            </div>
          </section>
        ))}
      </>
    );
  }

  return (
    <div className="ph-page ph-terms">
      <TopBar center title={t("nav.terminals")} sub={list.data && rows.length > 0 ? t("term.screen.counts", { open: counts.open, host: counts.host, staff: counts.staff }) : undefined}
        actions={<IconButton icon="plus" label={t("term.new")} disabled={!!list.data && !anyEnv} onClick={() => setSheet("new")} />} />
      {load.data && rows.length > 0 && <CapacityLine load={load.data} onOpen={() => setSheet("capacity")} />}
      {rows.length > 0 && (
        <ChipBar label={t("term.filter.label")}>
          {FILTERS.map((f) => {
            const n = rows.filter((r) => matches(r, f)).length;
            return <Chip key={f} on={filter === f} count={n > 0 && f !== "finished" ? n : undefined} onClick={() => setFilter(f)}>{t(`term.filter.${f}`)}</Chip>;
          })}
        </ChipBar>
      )}
      <div className={`ph-page-body ${rows.length > 0 ? "list" : ""}`}>{body}</div>
      {sheet === "new" && <NewTerminalSheet envs={envs} lens={lens} projects={projects} toast={toast} onClose={() => setSheet(null)} />}
      {sheet === "capacity" && load.data && <CapacitySheet load={load.data} onClose={() => setSheet(null)} />}
    </div>
  );
}

/** The track's three parts, as shares of the machine's memory: everything else, the terminals now,
 *  and what the limit would add. */
function shares(load: TerminalLoad) {
  const f = loadFigures(load);
  const pct = (n: number) => (f.total ? Math.max(0, Math.min(100, (100 * n) / f.total)) : 0);
  const other = pct(Math.max(0, f.machineNow - f.terminalsNow));
  const now = Math.min(100 - other, pct(f.terminalsNow));
  const extra = Math.min(100 - other - now, pct(f.machineAtCap - f.machineNow));
  return { f, other, now, extra };
}

function Track({ load, big }: { load: TerminalLoad; big?: boolean }) {
  const { f, other, now, extra } = shares(load);
  return (
    <span className={`ph-cbar ${big ? "big" : ""}`} data-level={f.level} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(f.memPercentAtCap)} aria-label={t("load.aria", { percent: Math.round(f.memPercentAtCap), n: f.cap })}>
      <i className="o" style={{ width: `${other}%` }} />
      <i className="n" style={{ width: `${now}%`, minWidth: now > 0 ? 3 : 0 }} />
      <i className="k" style={{ width: `${extra}%` }} />
    </span>
  );
}

function CapacityLine({ load, onOpen }: { load: TerminalLoad; onOpen: () => void }) {
  const f = loadFigures(load);
  if (!f.known) return null;
  return (
    <button type="button" className="ph-cap" data-level={f.level} onClick={onOpen} aria-haspopup="dialog">
      <span className="ph-cap-l">
        <Icon name="chart" size={14} />
        <span className="ph-ell">{t("ph.term.cap.line", { percent: Math.round(f.memPercentNow), line: plural("load.atcap", f.cap, { used: size(f.terminalsAtCap), total: size(f.total) }) })}</span>
        <Icon name="forward" size={14} />
      </span>
      <Track load={load} />
    </button>
  );
}

/** The whole picture of what terminals cost the machine, and the way to change the limit. */
function CapacitySheet({ load, onClose }: { load: TerminalLoad; onClose: () => void }) {
  const f = loadFigures(load);
  const other = Math.max(0, f.machineNow - f.terminalsNow);
  return (
    <BottomSheet onClose={onClose} className="ph-capacity" label={t("ph.term.cap.title")}
      title={<span className="ph-sheet-kt-w"><span className="ph-sheet-kt-t">{t("ph.term.cap.title")}</span><span className="ph-sheet-kt-m">{t("load.percent", { percent: Math.round(f.memPercentNow) })}</span></span>}>
      <div className="ph-sheet-pad">
        <div className="ph-cap-big">{plural("load.atcap", f.cap, { used: size(f.terminalsAtCap), total: size(f.total) })}</div>
        <Track load={load} big />
        <div className="ph-legend">
          <div><span className="sw o" />{t("ph.term.cap.other")}<span className="v">{size(other)}</span></div>
          <div><span className="sw n" />{plural("ph.term.cap.now", f.running)}<span className="v">{size(f.terminalsNow)}</span></div>
          {f.extra > 0 && <div><span className="sw k" />{t("ph.term.cap.kept", { cap: f.cap })}<span className="v">{size(f.machineAtCap - f.machineNow)}</span></div>}
          <div><span className="sw f" />{t("ph.term.cap.free")}<span className="v">{size(Math.max(0, f.total - f.machineNow))}</span></div>
        </div>
        <div className="ph-msep" />
        <div className="ph-kv" data-level={f.cpuLevel}><span>{t("ph.term.cap.cpu")}</span><span>{t("ph.term.cap.cpu.v", { now: Math.round(f.cpuNow), atcap: Math.round(f.cpuAtCap), cap: f.cap })}</span></div>
        <div className="ph-kv"><span>{t("ph.term.cap.limit")}</span><span>{plural("ph.term.cap.limit.v", f.cap)}</span></div>
        {f.level !== "ok" && <Banner tone={f.level === "bad" ? "bad" : "warn"} icon="alert">{f.supported !== null ? plural("load.over", f.supported) : t("load.over.memory")}</Banner>}
        <button type="button" className="ph-btn" onClick={() => { onClose(); navigate(pathFor("settings", "environments")); }}>{t("ph.term.cap.change")}</button>
      </div>
    </BottomSheet>
  );
}

/**
 * A new terminal: where it runs (the container, or the host with what that means said beside it) and
 * the folder it starts in, typed, picked from the projects' folders, or browsed for on the desktop app.
 */
function NewTerminalSheet({ envs, lens, projects, toast, onClose }: { envs: TerminalEnv[]; lens: Project | null; projects: Project[]; toast: (text: string) => void; onClose: () => void }) {
  const env = (name: TerminalEnvName) => envs.find((e) => e.env === name);
  const sessions = peek<SessionList>("/api/sessions")?.sessions ?? [];
  const here = containerFolder(lens, sessions);
  const [where, setWhere] = useState<TerminalEnvName>(env("container")?.available ? "container" : "host");
  const start = (name: TerminalEnvName) => (name === "container" ? here?.path ?? "" : env("host")?.home ?? "");
  const [path, setPath] = useState(() => start(where));
  const [busy, setBusy] = useState(false);
  const pick = window.daedalus?.pickFolder;
  const suggestions = projects.flatMap((p) => p.folders.filter((f) => f.env === where).map((f) => ({ path: f.path, project: p })));
  const chosenProject = suggestions.find((s) => s.path === path.trim())?.project.id ?? (where === "container" && path.trim() === here?.path ? here?.projectId ?? null : null);
  const choose = (name: TerminalEnvName) => { setWhere(name); setPath(start(name)); };
  const go = async () => {
    setBusy(true);
    await openFreeTerminal(where, path.trim() || undefined, chosenProject, toast);
    setBusy(false);
    onClose();
  };
  const option = (name: TerminalEnvName, title: string, sub: string) => {
    const e = env(name);
    return (
      <button type="button" role="radio" aria-checked={where === name} className={`ph-opt ${where === name ? "on" : ""} ${name}`} data-choice={name} disabled={!e?.available} onClick={() => choose(name)}>
        <span className="ph-radio" aria-hidden />
        <span className="ph-opt-main"><span className="ph-opt-t">{title}</span><span className="ph-opt-m">{e?.available ? sub : t(`term.unavailable.${name}`)}</span></span>
        <Icon name={name === "host" ? "lock" : "terminal"} size={18} />
      </button>
    );
  };
  return (
    <BottomSheet title={t("term.new")} onClose={onClose} className="ph-newterm"
      footer={<button type="button" className="ph-btn primary block" disabled={busy || !env(where)?.available} onClick={() => void go()}>{t("term.new.open")}</button>}>
      <div className="ph-sheet-pad">
        <div role="radiogroup" aria-label={t("term.new.where")} className="ph-opts">
          {option("container", t("term.new.in.container"), t("ph.term.new.container"))}
          {option("host", t("term.new.on.host"), t("ph.term.new.host"))}
        </div>
        <label className="ph-label" htmlFor="ph-term-path">{t("ph.term.folder")}</label>
        <div className="ph-foot-row">
          <input id="ph-term-path" className="ph-field mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder={where === "container" ? t("term.new.container.home") : t("term.new.host.home")} list="ph-term-suggestions" autoCapitalize="off" spellCheck={false} />
          {pick && <button type="button" className="ph-btn" onClick={() => void pick().then((picked) => picked && setPath(picked))}>{t("term.new.browse")}</button>}
        </div>
        <datalist id="ph-term-suggestions">{suggestions.map((s) => <option key={`${s.project.id}:${s.path}`} value={s.path}>{s.project.name}</option>)}</datalist>
        {suggestions.length > 0 && (
          <div className="ph-tags">
            {suggestions.slice(0, 6).map((s) => (
              <button key={`${s.project.id}:${s.path}`} type="button" className={`ph-chip ${path === s.path ? "on" : ""}`} aria-pressed={path === s.path} title={s.path} onClick={() => setPath(s.path)}>
                {/* A project with two folders here names the folder too, or its two chips read the same. */}
                <span className="ph-chip-l">{suggestions.filter((o) => o.project.id === s.project.id).length > 1 ? `${s.project.name} · ${s.path.split("/").filter(Boolean).pop() ?? s.path}` : s.project.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </BottomSheet>
  );
}
