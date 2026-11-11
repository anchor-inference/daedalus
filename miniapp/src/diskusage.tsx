// A workspace's disk use: how large it is against its limit, what the largest folders are, and the
// one deliberate way to clear throwaway ones. A session once filled the host's disk with hundreds of
// repository copies nobody could see; this is where they show up, and removal is a ticked list in a
// confirmation, never a button on a row.

import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { relTime } from "./format";
import { plural, t } from "./i18n";
import { invalidate, prime, useOffline, useQuery } from "./store";
import { errorText } from "./ui";
import { Sheet } from "./ui/dialogs";
import {
  canTick, defaultSelection, diskBytes, fillPercent, limitChoices, limitText, parseLimitGb, reasonKey, selectedBytes, sizeText, tone,
  withLimit, withoutTracked,
  type CleanupResult, type DiskDetail, type DiskEntry, type LimitResult, type WorkspaceDetail,
} from "./diskmodel";

export const diskKey = (kind: "session" | "project", id: string) => `/api/disk/${kind}/${encodeURIComponent(id)}`;

/** True once the enclosing <details> is open (or at once when there is none): the measurement walks
 *  a disk, so a section the operator never opens never asks for it. */
function useOpenAncestor(): [React.RefObject<HTMLDivElement | null>, boolean] {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const details = ref.current?.closest("details") ?? null;
    if (!details) { setOpen(true); return; }
    setOpen(details.open);
    const sync = () => setOpen(details.open);
    details.addEventListener("toggle", sync);
    return () => details.removeEventListener("toggle", sync);
  }, []);
  return [ref, open];
}

export function DiskUsage({ kind, id, toast }: { kind: "session" | "project"; id: string; toast: (text: string) => void }) {
  const [ref, open] = useOpenAncestor();
  const key = diskKey(kind, id);
  const query = useQuery<DiskDetail>(open ? key : null, { staleMs: 30000 });
  const data = query.data;
  return <div ref={ref} className="disk-usage" data-disk-usage>
    {open && query.error && !data && <p className="sub attn">{errorText(query.error)}</p>}
    {open && !data && !query.error && <p className="sub">{t("common.loading")}</p>}
    {data && <>
      <div className={`sub disk-free${data.low_disk ? " attn" : ""}`} data-disk-free>
        {data.low_disk ? t("disk.low", { free: diskBytes(data.free_bytes), total: diskBytes(data.total_bytes) }) : t("disk.free", { free: diskBytes(data.free_bytes), total: diskBytes(data.total_bytes) })}
      </div>
      {!data.workspaces.length && <p className="sub">{t("disk.none")}</p>}
      {data.workspaces.map((workspace) => (
        <WorkspaceUsage key={workspace.name} workspace={workspace} defaultLimit={data.default_limit_bytes} toast={toast}
          onCleaned={() => invalidate(key)} onLimit={(result) => prime(key, withLimit(data, result))} />
      ))}
      {data.outside.map((folder) => <p key={folder.label} className="sub" data-disk-outside>{t("disk.outside", { label: folder.label })}</p>)}
    </>}
  </div>;
}

function WorkspaceUsage({ workspace, defaultLimit, toast, onCleaned, onLimit }: {
  workspace: WorkspaceDetail; defaultLimit: number; toast: (text: string) => void; onCleaned: () => void; onLimit: (result: LimitResult) => void;
}) {
  const [cleaning, setCleaning] = useState(false);
  const [changing, setChanging] = useState(false);
  const limit = workspace.limit_bytes;
  return <div className="disk-workspace" data-disk-workspace={workspace.name}>
    <div className="disk-head">
      <span className="grow truncate" title={workspace.label}>{workspace.label}</span>
      <b data-disk-total>{sizeText(workspace.bytes, workspace.truncated)}</b>
      <span className="sub" data-disk-limit>{limit > 0 ? t("disk.ofLimit", { limit: diskBytes(limit) }) : t("disk.noLimit")}</span>
    </div>
    {limit > 0 && <div className={`bar ${tone(workspace.over)}`} style={{ "--v": fillPercent(workspace.bytes, limit) } as React.CSSProperties}><i /></div>}
    <div className="disk-limitline">
      {workspace.over > 0
        ? <span className="sub attn grow" data-disk-over>{t(workspace.over === 2 ? "disk.over2" : "disk.over1")}</span>
        : <span className="sub faint grow" data-disk-source>{t(workspace.own_limit ? "disk.limit.own" : "disk.limit.inherited")}</span>}
      <button className="btn ghost small" aria-expanded={changing} data-disk-limit-open onClick={() => setChanging(!changing)}>{t("disk.limit.change")}</button>
    </div>
    {changing && <LimitEditor workspace={workspace} defaultLimit={defaultLimit} toast={toast} onSaved={onLimit} onClose={() => setChanging(false)} />}
    <div className="disk-entries">
      {workspace.entries.map((entry) => <EntryRow key={entry.path} entry={entry} />)}
    </div>
    <button className="btn ghost small" onClick={() => setCleaning(true)} disabled={!workspace.entries.length}>{t("disk.cleanup")}</button>
    {cleaning && <CleanupSheet workspace={workspace} toast={toast} onClose={() => setCleaning(false)} onCleaned={onCleaned} />}
  </div>;
}

/** The ways to change one workspace's limit. Every choice is saved at once: there is no form to fill in
 *  and confirm, since a limit only decides when someone is told, and is changed back as easily. */
function LimitEditor({ workspace, defaultLimit, toast, onSaved, onClose }: {
  workspace: WorkspaceDetail; defaultLimit: number; toast: (text: string) => void; onSaved: (result: LimitResult) => void; onClose: () => void;
}) {
  const [text, setText] = useState(() => (workspace.limit_bytes > 0 ? String(Math.round(workspace.limit_bytes / 1e6) / 1000) : ""));
  const [busy, setBusy] = useState(false);
  const offline = useOffline();
  const typed = parseLimitGb(text);
  const unlimited = workspace.own_limit && workspace.limit_bytes === 0;
  async function save(limitGb: number | null) {
    if (busy || offline) return;
    setBusy(true);
    try {
      onSaved(await api.put<LimitResult>(`/api/disk/limit/${encodeURIComponent(workspace.name)}`, { limit_gb: limitGb }));
      onClose();
    } catch (error) { toast(errorText(error)); }
    finally { setBusy(false); }
  }
  const idle = busy || offline;
  return <div className="disk-limit-edit" data-disk-limit-edit>
    <div className="disk-limit-choices">
      {limitChoices(workspace.limit_bytes).map((choice) => (
        <button key={choice.id} className="chip select" disabled={idle} data-disk-limit-choice={choice.id}
          title={diskBytes(choice.gb * 1e9)} onClick={() => void save(choice.gb)}>{choice.label}</button>
      ))}
      <button className="chip select" disabled={idle} aria-pressed={unlimited} data-disk-limit-choice="none" onClick={() => (unlimited ? onClose() : void save(0))}>{t("disk.limit.none")}</button>
      <button className="chip select" disabled={idle} aria-pressed={!workspace.own_limit} data-disk-limit-choice="default"
        onClick={() => (workspace.own_limit ? void save(null) : onClose())}>{t("disk.limit.default", { limit: limitText(defaultLimit) })}</button>
    </div>
    <div className="disk-limit-custom">
      <span className="settings-num">
        <input className="field" type="number" inputMode="decimal" min={0} step="any" value={text} aria-label={t("disk.limit.custom")} aria-invalid={text.trim() !== "" && typed === null}
          data-disk-limit-input onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && typed !== null) void save(typed); }} />
        <span className="settings-unit">{t("disk.limit.unit")}</span>
      </span>
      <button className="btn small" disabled={idle || typed === null} data-disk-limit-set onClick={() => typed !== null && void save(typed)}>{t("common.save")}</button>
    </div>
  </div>;
}

function EntryRow({ entry }: { entry: DiskEntry }) {
  const [open, setOpen] = useState(false);
  return <div className="disk-entry" data-disk-entry={entry.path}>
    <div className="disk-row">
      <button className="linkbtn disk-name mono truncate" aria-expanded={open} disabled={!entry.children.length} onClick={() => setOpen(!open)} title={entry.path}>{entry.path}</button>
      <EntryBadges entry={entry} />
      <span className="disk-size">{diskBytes(entry.bytes)}</span>
    </div>
    {entry.newest && <div className="sub disk-meta">{t("disk.newest", { when: relTime(entry.newest) })} · {plural("disk.files", entry.files)}</div>}
    {open && entry.children.map((child) => (
      <div key={child.path} className="disk-row disk-child" data-disk-child={child.path}>
        <span className="mono truncate grow" title={child.path}>{child.path.split("/").pop()}</span>
        <EntryBadges entry={child} />
        <span className="disk-size">{diskBytes(child.bytes)}</span>
      </div>
    ))}
  </div>;
}

function EntryBadges({ entry }: { entry: DiskEntry }) {
  return <>
    {entry.throwaway && <span className="badge">{t("disk.throwaway")}</span>}
    {entry.tracked && <span className="badge attn">{t("disk.tracked")}</span>}
    {entry.protected && <span className="badge">{t("disk.protected")}</span>}
  </>;
}

function CleanupSheet({ workspace, toast, onClose, onCleaned }: { workspace: WorkspaceDetail; toast: (text: string) => void; onClose: () => void; onCleaned: () => void }) {
  const [selected, setSelected] = useState(() => defaultSelection(workspace.entries));
  const [includeTracked, setIncludeTracked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CleanupResult | null>(null);
  const offline = useOffline();
  const anyTracked = workspace.entries.some((entry) => entry.tracked);
  function toggle(path: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(path); else next.delete(path);
    setSelected(next);
  }
  async function run() {
    if (busy || offline || !selected.size) return;
    setBusy(true);
    try {
      const done = await api.post<CleanupResult>("/api/disk/cleanup", { workspace: workspace.name, paths: [...selected], include_tracked: includeTracked });
      setResult(done);
      onCleaned();
    } catch (error) { toast(errorText(error)); }
    finally { setBusy(false); }
  }
  return <Sheet title={t("disk.cleanup.title", { label: workspace.label })} onClose={onClose} size="narrow">
    {result ? <div data-disk-result>
      <p data-disk-freed><b>{t("disk.freed", { size: diskBytes(result.freed_bytes) })}</b></p>
      {result.refused.length > 0 && <div className="disk-refused">
        <div className="sub attn">{t("disk.refused.title")}</div>
        {result.refused.map((item) => <div key={item.path} className="sub" data-disk-refused={item.path}><span className="mono">{item.path}</span> — {t(reasonKey(item.reason))}</div>)}
      </div>}
      <div className="sheet-foot"><button className="btn primary" onClick={onClose}>{t("common.close")}</button></div>
    </div> : <>
      <p className="sub">{t("disk.cleanup.body")}</p>
      <div className="disk-pick">
        {workspace.entries.map((entry) => {
          const ok = canTick(entry, includeTracked);
          return <label key={entry.path} className="toggle-row disk-pick-row" data-disk-pick={entry.path}>
            <input type="checkbox" checked={selected.has(entry.path)} disabled={!ok || busy} onChange={(event) => toggle(entry.path, event.target.checked)} />
            <span className="grow mono truncate">{entry.path}</span>
            <EntryBadges entry={entry} />
            <span className="disk-size">{diskBytes(entry.bytes)}</span>
          </label>;
        })}
      </div>
      {anyTracked && <label className="toggle-row" data-disk-tracked>
        <input type="checkbox" checked={includeTracked} disabled={busy}
          onChange={(event) => { setIncludeTracked(event.target.checked); if (!event.target.checked) setSelected(withoutTracked(selected, workspace.entries)); }} />
        <span>{t("disk.includeTracked")}</span>
      </label>}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
        <button className="btn danger" data-disk-confirm onClick={() => void run()} disabled={busy || offline || !selected.size}>
          {t("disk.cleanup.confirm", { size: diskBytes(selectedBytes(selected, workspace.entries)) })}
        </button>
      </div>
    </>}
  </Sheet>;
}
