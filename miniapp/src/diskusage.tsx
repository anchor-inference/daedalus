// A workspace's disk use: how large it is against its limit, what the largest folders are, and the
// one deliberate way to clear throwaway ones. A session once filled the host's disk with hundreds of
// repository copies nobody could see; this is where they show up, and removal is a ticked list in a
// confirmation, never a button on a row.

import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { bytes, relTime } from "./format";
import { plural, t } from "./i18n";
import { invalidate, useOffline, useQuery } from "./store";
import { errorText } from "./ui";
import { Sheet } from "./ui/dialogs";
import {
  canTick, defaultSelection, fillPercent, reasonKey, selectedBytes, sizeText, tone, withoutTracked,
  type CleanupResult, type DiskDetail, type DiskEntry, type WorkspaceDetail,
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
        {data.low_disk ? t("disk.low", { free: bytes(data.free_bytes), total: bytes(data.total_bytes) }) : t("disk.free", { free: bytes(data.free_bytes), total: bytes(data.total_bytes) })}
      </div>
      {!data.workspaces.length && <p className="sub">{t("disk.none")}</p>}
      {data.workspaces.map((workspace) => (
        <WorkspaceUsage key={workspace.name} workspace={workspace} limit={data.limit_bytes} toast={toast}
          onCleaned={() => invalidate(key)} />
      ))}
      {data.outside.map((folder) => <p key={folder.label} className="sub" data-disk-outside>{t("disk.outside", { label: folder.label })}</p>)}
    </>}
  </div>;
}

function WorkspaceUsage({ workspace, limit, toast, onCleaned }: { workspace: WorkspaceDetail; limit: number; toast: (text: string) => void; onCleaned: () => void }) {
  const [cleaning, setCleaning] = useState(false);
  return <div className="disk-workspace" data-disk-workspace={workspace.name}>
    <div className="disk-head">
      <span className="grow truncate" title={workspace.label}>{workspace.label}</span>
      <b data-disk-total>{sizeText(workspace.bytes, workspace.truncated)}</b>
      {limit > 0 && <span className="sub">{t("disk.ofLimit", { limit: bytes(limit) })}</span>}
    </div>
    {limit > 0 && <div className={`bar ${tone(workspace.over)}`} style={{ "--v": fillPercent(workspace.bytes, limit) } as React.CSSProperties} />}
    {workspace.over > 0 && <div className="sub attn">{t(workspace.over === 2 ? "disk.over2" : "disk.over1")}</div>}
    <div className="disk-entries">
      {workspace.entries.map((entry) => <EntryRow key={entry.path} entry={entry} />)}
    </div>
    <button className="btn ghost small" onClick={() => setCleaning(true)} disabled={!workspace.entries.length}>{t("disk.cleanup")}</button>
    {cleaning && <CleanupSheet workspace={workspace} toast={toast} onClose={() => setCleaning(false)} onCleaned={onCleaned} />}
  </div>;
}

function EntryRow({ entry }: { entry: DiskEntry }) {
  const [open, setOpen] = useState(false);
  return <div className="disk-entry" data-disk-entry={entry.path}>
    <div className="disk-row">
      <button className="linkbtn disk-name mono truncate" aria-expanded={open} disabled={!entry.children.length} onClick={() => setOpen(!open)} title={entry.path}>{entry.path}</button>
      <EntryBadges entry={entry} />
      <span className="disk-size">{bytes(entry.bytes)}</span>
    </div>
    {entry.newest && <div className="sub disk-meta">{t("disk.newest", { when: relTime(entry.newest) })} · {plural("disk.files", entry.files)}</div>}
    {open && entry.children.map((child) => (
      <div key={child.path} className="disk-row disk-child" data-disk-child={child.path}>
        <span className="mono truncate grow" title={child.path}>{child.path.split("/").pop()}</span>
        <EntryBadges entry={child} />
        <span className="disk-size">{bytes(child.bytes)}</span>
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
      <p data-disk-freed><b>{t("disk.freed", { size: bytes(result.freed_bytes) })}</b></p>
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
            <span className="disk-size">{bytes(entry.bytes)}</span>
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
          {t("disk.cleanup.confirm", { size: bytes(selectedBytes(selected, workspace.entries)) })}
        </button>
      </div>
    </>}
  </Sheet>;
}
