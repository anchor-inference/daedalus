// The "Import GitHub issues" sheet: name the repository (the project folder's remote by default), read
// its open issues against the board, tick the ones to bring in and apply. Nothing is written to GitHub;
// every ticked issue goes through the host's per-issue preview digest, so an issue that changed after
// the listing was read is reported rather than imported as it no longer is.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Sheet } from "../ui/dialogs";
import { Skeleton } from "../ui/components";
import { t } from "../i18n";
import { errorText } from "../ui";
import { useOffline } from "../store";
import {
  counts,
  keepPicked,
  normalizeRepository,
  selectable,
  selectionItems,
  toggleAll,
  validRepository,
  type ImportResult,
  type IssueEntry,
  type IssueListing,
  type IssueSource,
} from "./issueimport";

export function IssueImport({ projectId, onClose, onDone, toast }: { projectId: string; onClose: () => void; onDone: () => void; toast: (text: string) => void }) {
  const offline = useOffline();
  const [source, setSource] = useState<IssueSource | null>(null);
  const [repository, setRepository] = useState("");
  const [label, setLabel] = useState("");
  const [listing, setListing] = useState<IssueListing | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [skipped, setSkipped] = useState<ImportResult["skipped"]>([]);
  // One identity per distinct request, so a retry after a lost answer replays the host's receipts
  // instead of importing the same issues twice.
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);

  useEffect(() => {
    let current = true;
    api.get<IssueSource>(`/api/issues/sync/source?project_id=${encodeURIComponent(projectId)}`).then((found) => {
      if (!current) return;
      setSource(found);
      if (found.repository) {
        setRepository(found.repository);
        if (found.configured) void read(found.repository, "");
      }
    }).catch((e) => { if (current) { setSource({ project_id: projectId, repository: null, configured: true }); setError(errorText(e)); } });
    return () => { current = false; };
  }, [projectId]);

  async function read(repo = repository, filter = label) {
    const name = normalizeRepository(repo);
    if (!validRepository(name)) { setError(t("issues.repo.invalid")); return; }
    setRepository(name);
    setLoading(true);
    setError("");
    setSkipped([]);
    try {
      const next = await api.post<IssueListing>("/api/issues/sync/list", { project_id: projectId, repository: name, label: filter.trim() || null });
      setListing(next);
      setPicked((previous) => keepPicked(next, previous));
    } catch (e) {
      setListing(null);
      setError(errorText(e));
    } finally { setLoading(false); }
  }

  async function apply() {
    if (!listing || offline) return;
    const items = selectionItems(listing, picked);
    if (!items.length) return;
    const body = { project_id: projectId, repository: listing.repository, items, expected_collection_revision: listing.collection_revision };
    const fingerprint = JSON.stringify(body);
    if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      const result = await api.post<ImportResult>("/api/issues/sync/import", { ...body, client_operation_id: operation.current.id });
      operation.current = null;
      const made = result.applied.filter((entry) => entry.action === "import").length;
      const updated = result.applied.length - made;
      toast(t("issues.done", { made, updated, skipped: result.skipped.length }));
      onDone();
      if (result.skipped.length === 0) { onClose(); return; }
      // The listing is read again so what was applied shows as on the board, and the reasons for
      // the rest stay beside their rows.
      setPicked(new Set());
      await read(listing.repository, listing.label ?? "");
      setSkipped(result.skipped);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) operation.current = null;
      toast(errorText(e));
    } finally { setBusy(false); }
  }

  const ticked = listing ? selectionItems(listing, picked).length : 0;
  const tally = listing ? counts(listing) : null;
  const choosable = listing ? listing.issues.filter(selectable).length : 0;
  return (
    <Sheet title={t("issues.title")} onClose={onClose} className="pboard-sheet issue-import">
      {!source && <Skeleton rows={2} />}
      {source && <>
        {!source.configured && <div className="result-warning" role="status">{t("issues.token")}</div>}
        <label className="field" htmlFor="issues-repo">{t("issues.repo")}</label>
        <div className="issue-import-repo">
          <input id="issues-repo" className="field" value={repository} placeholder="owner/repo" spellCheck={false} autoCapitalize="off"
            onChange={(e) => setRepository(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void read(); }} />
          <button className="btn" disabled={loading || !repository.trim()} onClick={() => void read()}>{t("issues.read")}</button>
        </div>
        <div className="sub">{source.repository ? t("issues.repo.guessed") : t("issues.repo.none")}</div>
        <details open={!!label}>
          <summary>{t("issues.label")}{label.trim() ? ` · ${label.trim()}` : ""}</summary>
          <label className="field" htmlFor="issues-label">{t("issues.label.name")}</label>
          <input id="issues-label" className="field" value={label} maxLength={50} placeholder={t("issues.label.placeholder")}
            onChange={(e) => setLabel(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void read(); }} />
        </details>
      </>}
      {error && <div className="result-warning" role="alert">{error}</div>}
      {loading && <Skeleton rows={3} />}
      {listing && !loading && (
        <section className="issue-import-list" aria-label={t("issues.list")}>
          <div className="issue-import-head">
            <span className="sub grow">{t("issues.summary", { total: listing.issues.length, importable: tally!.import, updatable: tally!.update })}</span>
            {choosable > 1 && <button className="linkbtn" onClick={() => setPicked(toggleAll(listing, picked))}>{t("issues.all")}</button>}
          </div>
          {listing.issues.length === 0 && <div className="pboard-none">{listing.label ? t("issues.none.label") : t("issues.none")}</div>}
          {listing.issues.map((entry) => <IssueRow key={entry.number} entry={entry} checked={picked.has(entry.number)} onToggle={(on) => setPicked((current) => {
            const next = new Set(current);
            if (on) next.add(entry.number); else next.delete(entry.number);
            return next;
          })} skippedReason={skipped.find((skip) => skip.issue_number === entry.number)?.reason} />)}
          {listing.issues.length >= listing.limit && <div className="sub">{t("issues.more", { n: listing.limit })}</div>}
        </section>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || offline || !listing || ticked === 0} onClick={() => void apply()}>{ticked ? t("issues.apply", { n: ticked }) : t("issues.apply.none")}</button>
      </div>
    </Sheet>
  );
}

function IssueRow({ entry, checked, onToggle, skippedReason }: { entry: IssueEntry; checked: boolean; onToggle: (on: boolean) => void; skippedReason?: string }) {
  const can = selectable(entry);
  return (
    <label className={`toggle-row issue-row ${can ? "" : "is-off"}`} data-issue={entry.number} data-action={entry.action}>
      <input type="checkbox" checked={can && checked} disabled={!can} onChange={(e) => onToggle(e.target.checked)} />
      <span className="issue-row-text">
        <span className="issue-row-title"><a href={entry.url} target="_blank" rel="noreferrer" className="issue-row-number" onClick={(e) => e.stopPropagation()}>#{entry.number}</a> {entry.title}</span>
        <span className="sub issue-row-action">{t(`issues.action.${entry.action}`, { title: entry.task_title ?? "" })}</span>
        {skippedReason && <span className="sub attn">{skippedReason}</span>}
      </span>
    </label>
  );
}
