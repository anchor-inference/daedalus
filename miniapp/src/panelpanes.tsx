// The panel's tools that are not a whole screen elsewhere: the chat's working-tree changes with their
// diff, revert and commit; the files that went in and out of the conversation; the canvases and the
// memory it made; and the context and the spend, whose pages Details used to hold. Each reads its own
// listing when it opens and again when the host says the thing changed, so a tool that is not open
// costs nothing.

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type MemoryRecord, type ProviderUsage, type SessionDetail } from "./api";
import { useEvent, useStreamUp } from "./events";
import { useQuery } from "./store";
import { Icon } from "./icons";
import { plural, t } from "./i18n";
import { bytes, shortDateTime } from "./format";
import { fileIcon } from "./artifact";
import { parseDiff } from "./diff";
import { Counts, FileDiffBody, FileDiffHeader } from "./diffpane";
import { Button, confirmDialog } from "./ui/primitives";
import { errorText } from "./ui";
import { fmtUsd } from "./ui/components";
import { navigate, pathFor } from "./router";
import { CanvasRow } from "./canvas/parts";
import type { CanvasItem } from "./canvas/model";
import { ContextBody, ProviderUsageBody, UsageBody, providerLabel } from "./details";

// ── the chat's changes ───────────────────────────────────────────────────────────────────────

export type ChangedFile = { path: string; status: string; added: number; removed: number; binary: boolean };
export type ChangesListing = {
  repository: boolean;
  branch?: string | null;
  upstream?: string | null;
  ahead?: number;
  behind?: number;
  files: ChangedFile[];
  added: number;
  removed: number;
  truncated?: boolean;
  reason?: string;
  detail?: string;
  /** A sentence while a revert or a commit would be refused now: the chat's run is in progress. */
  busy?: string;
};

export function changesKey(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/changes`;
}

/** The chat's working-tree changes, read when asked for and again when the host says they moved: a
 *  revert or a commit from anywhere (`session.changes`), and the end of a run, which is when the agent's
 *  edits have landed. Null `sessionId` reads nothing (a live link's visitor, a bare chat). */
export function useChatChanges(sessionId: string | null, status: string | undefined): { data: ChangesListing | undefined; refresh: () => Promise<void> } {
  const query = useQuery<ChangesListing>(sessionId ? changesKey(sessionId) : null, { staleMs: 4000 });
  const { refresh } = query;
  useEvent(["session.changes"], (event) => {
    if (sessionId && event.session_id === sessionId) void refresh();
  }, [sessionId, refresh]);
  const was = useRef(status);
  useEffect(() => {
    if (was.current === "running" && status !== "running") void refresh();
    was.current = status;
  }, [status, refresh]);
  const live = useStreamUp();
  const wasLive = useRef(live);
  useEffect(() => {
    if (live && !wasLive.current) void refresh();
    wasLive.current = live;
  }, [live, refresh]);
  return { data: query.data, refresh };
}

/** Which files a revert deletes rather than puts back: the ones git does not have at HEAD. */
export function deletedByRevert(files: ChangedFile[]): ChangedFile[] {
  return files.filter((file) => file.status === "?" || file.status === "A");
}

/** "2 файла в order/": how many files, and the one folder they share when they share one. */
export function changesSummary(files: ChangedFile[]): string {
  const dirs = new Set(files.map((file) => file.path.includes("/") ? file.path.slice(0, file.path.lastIndexOf("/") + 1) : ""));
  const count = plural("panel.changes.files", files.length);
  const [only] = [...dirs];
  return dirs.size === 1 && only ? t("panel.changes.in", { files: count, dir: only }) : count;
}

/** The Changes tool: the files the chat changed in its folder, one of them as a diff, and the two
 *  things one does with them — put them back, or commit them on the branch. */
export function ChangesPane({ sessionId, listing, refresh, toast }: { sessionId: string; listing: ChangesListing | undefined; refresh: () => Promise<void>; toast: (text: string) => void }) {
  const files = listing?.files ?? [];
  const [chosen, setChosen] = useState<string | null>(null);
  const current = files.find((file) => file.path === chosen) ?? files[0] ?? null;
  const [diff, setDiff] = useState<{ path: string; text: string; truncated: boolean } | null>(null);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const path = current?.path ?? null;
  // The diff is read again whenever the listing is: the same name may carry other lines after a run.
  const stamp = `${listing?.added ?? 0}:${listing?.removed ?? 0}:${files.length}`;
  useEffect(() => {
    if (!path) { setDiff(null); return; }
    let gone = false;
    api.get<{ path: string; diff: string; truncated?: boolean }>(`${changesKey(sessionId)}/diff?path=${encodeURIComponent(path)}`)
      .then((answer) => { if (!gone) setDiff({ path, text: answer.diff ?? "", truncated: !!answer.truncated }); })
      .catch((e) => { if (!gone) { setDiff({ path, text: "", truncated: false }); toast(errorText(e)); } });
    return () => { gone = true; };
  }, [sessionId, path, stamp, toast]);
  const parsed = useMemo(() => (diff && diff.path === path ? parseDiff(diff.text)[0] ?? null : null), [diff, path]);
  const busy = listing?.busy || "";
  const revert = async (which: ChangedFile[]) => {
    const gone = deletedByRevert(which);
    const ok = await confirmDialog({
      title: which.length === files.length ? t("panel.changes.revert.all.title", { files: plural("panel.changes.files", which.length) }) : t("panel.changes.revert.one.title", { name: which[0]?.path ?? "" }),
      body: <div className="ch-confirm">
        <p>{t("panel.changes.revert.body")}</p>
        {gone.length > 0 && <>
          <p className="ch-confirm-warn">{t("panel.changes.revert.deleted")}</p>
          <ul className="ch-confirm-list">{gone.map((file) => <li key={file.path} className="mono">{file.path}</li>)}</ul>
        </>}
      </div>,
      action: t("panel.changes.revert"),
      danger: true,
    });
    if (!ok) return;
    setWorking(true);
    try {
      const body = which.length === files.length && !listing?.truncated ? { confirm: true, all: true } : { confirm: true, paths: which.map((file) => file.path) };
      const answer = await api.post<{ reverted?: string[]; deleted?: string[] }>(`${changesKey(sessionId)}/revert`, body);
      toast(t("panel.changes.reverted", { files: plural("panel.changes.files", answer.reverted?.length ?? which.length) }));
      await refresh();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setWorking(false);
    }
  };
  const commit = async () => {
    const text = (message ?? "").trim();
    if (!text) return;
    setWorking(true);
    try {
      const answer = await api.post<{ commit?: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/commit`, { message: text });
      toast(t("panel.changes.committed", { hash: (answer.commit ?? "").slice(0, 7) }));
      setMessage(null);
      await refresh();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setWorking(false);
    }
  };
  if (!listing) return <div className="tool-empty">…</div>;
  if (!listing.repository) return <div className="tool-empty">{t("panel.changes.norepo")}</div>;
  if (files.length === 0) return <div className="tool-empty">{t("panel.changes.none", { branch: listing.branch ?? "" })}</div>;
  return (
    <div className="ch-pane" data-tool-page="changes">
      <div className="ch-files" role="listbox" aria-label={t("panel.tab.changes")}>
        {files.map((file) => (
          <button key={file.path} type="button" role="option" aria-selected={file.path === current?.path} className={`ch-file ${file.path === current?.path ? "on" : ""}`} data-path={file.path} onClick={() => setChosen(file.path)} title={file.path}>
            <Icon name={fileIcon(file.path)} size={16} />
            <span className="ch-path mono">{file.path}</span>
            {file.status === "?" || file.status === "A" ? <span className="ch-new">{t("panel.changes.new")}</span> : null}
            <span className="ch-counts mono"><Counts added={file.added} removed={file.removed} /></span>
          </button>
        ))}
        {listing.truncated && <div className="tool-hint">{t("panel.changes.truncated")}</div>}
      </div>
      <div className="ch-diff">
        {parsed ? <>
          <FileDiffHeader file={parsed}>
            <button type="button" className="iconbtn small ch-revert-one" disabled={!!busy || working} title={busy || t("panel.changes.revert.file")} aria-label={t("panel.changes.revert.file")} onClick={() => current && void revert([current])}><Icon name="undo" size={14} /></button>
          </FileDiffHeader>
          <FileDiffBody key={path ?? ""} file={parsed} mode="unified" cut={diff?.truncated ? <span>{t("panel.changes.cut")}</span> : undefined} />
        </> : <div className="tool-empty">{diff ? t("diff.noLines") : "…"}</div>}
      </div>
      <div className="ch-foot">
        {message === null ? <>
          <span className="ch-foot-text">{[plural("panel.changes.files", files.length), listing.branch ? t("panel.changes.branch", { branch: listing.branch }) : ""].filter(Boolean).join(" · ")}</span>
          <Button disabled={!!busy || working} title={busy || undefined} onClick={() => void revert(files)} className="ch-revert">{t("panel.changes.revert")}</Button>
          <Button disabled={!!busy || working} title={busy || undefined} onClick={() => setMessage("")} className="ch-commit">{t("panel.changes.commit")}</Button>
        </> : <form className="ch-commit-form" onSubmit={(e) => { e.preventDefault(); void commit(); }}>
          <input className="field" autoFocus value={message} placeholder={t("panel.changes.message")} aria-label={t("panel.changes.message")} onChange={(e) => setMessage(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setMessage(null); } }} />
          <Button type="submit" variant="primary" disabled={!message.trim() || working || !!busy}>{t("panel.changes.commit.go")}</Button>
          <Button onClick={() => setMessage(null)}>{t("common.cancel")}</Button>
        </form>}
      </div>
      {busy && <div className="tool-hint ch-busy" role="status">{busy}</div>}
    </div>
  );
}

// ── attachments ──────────────────────────────────────────────────────────────────────────────

export type Attachment = { id: string; source: "operator" | "agent"; name: string; size: number | null; mime: string; message_seq: number | null; created_at: string; url: string };

/** Every file that went into or out of the conversation, oldest first as the host lists them, newest
 *  on top here: what the operator gave, what the agent sent, what it placed in an answer. */
export function AttachmentsPane({ sessionId, onJump }: { sessionId: string; onJump?: (seq: number) => void }) {
  const query = useQuery<{ attachments: Attachment[] }>(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, { staleMs: 10000 });
  const rows = useMemo(() => [...(query.data?.attachments ?? [])].reverse(), [query.data]);
  if (!query.data) return <div className="tool-empty">{query.error ?? "…"}</div>;
  if (rows.length === 0) return <div className="tool-empty">{t("panel.attachments.none")}</div>;
  return (
    <div className="tool-list" data-tool-page="attachments">
      {rows.map((file) => (
        <div key={file.id} className="tool-row" data-attachment={file.id}>
          <Icon name={fileIcon(file.name)} size={16} />
          <a className="tool-row-main" href={file.url} target="_blank" rel="noreferrer" title={file.name}>
            <span className="tool-row-name truncate">{file.name}</span>
            <span className="tool-row-meta">{[t(file.source === "agent" ? "panel.attachments.agent" : "panel.attachments.you"), file.size != null ? bytes(file.size) : "", shortDateTime(file.created_at)].filter(Boolean).join(" · ")}</span>
          </a>
          {onJump && file.message_seq != null && <button type="button" className="iconbtn small" onClick={() => onJump(file.message_seq!)} aria-label={t("panel.attachments.jump")} title={t("panel.attachments.jump")}><Icon name="reply" size={14} /></button>}
        </div>
      ))}
    </div>
  );
}

// ── canvases ─────────────────────────────────────────────────────────────────────────────────

/** The canvases this chat made or was given, each opening on the canvas page, and the way to all of them. */
export function CanvasPane({ sessionId, canvases }: { sessionId: string; canvases: CanvasItem[] | undefined }) {
  const all = pathFor("canvas", null, { session: sessionId });
  return (
    <div className="tool-list" data-tool-page="canvas">
      {canvases === undefined ? <div className="tool-empty">…</div> : canvases.length === 0 ? <div className="tool-empty">{t("panel.canvas.none")}</div> : canvases.map((canvas) => (
        <CanvasRow key={canvas.id} item={canvas} onOpen={() => navigate(pathFor("canvas", canvas.id, { session: sessionId }))} />
      ))}
      <div className="tool-foot"><a className="btn" href={all} onClick={(e) => { e.preventDefault(); navigate(all); }}>{t("panel.canvas.open")}</a></div>
    </div>
  );
}

// ── memory ───────────────────────────────────────────────────────────────────────────────────

/** What this chat taught the memory: the records whose source is this conversation. */
export function MemoryPane({ sessionId }: { sessionId: string }) {
  const query = useQuery<{ records: MemoryRecord[] }>(`/api/memory?source_session=${encodeURIComponent(sessionId)}`, { staleMs: 15000 });
  const records = query.data?.records ?? null;
  return (
    <div className="tool-list" data-tool-page="memory">
      {records === null ? <div className="tool-empty">{query.error ?? "…"}</div> : records.length === 0 ? <div className="tool-empty">{t("panel.memory.none")}</div> : records.map((record) => (
        <button key={record.id} type="button" className="tool-row press tall" onClick={() => navigate(record.entry ? pathFor("memory", record.entry) : pathFor("memory", null, { view: "facts" }))}>
          <Icon name="brain" size={16} />
          <span className="tool-row-main">
            <span className="tool-row-text">{record.text}</span>
            <span className="tool-row-meta">{[t(`memory.cat.${record.category}`), ...record.topics.slice(0, 2)].filter(Boolean).join(" · ")}</span>
          </span>
        </button>
      ))}
      <div className="tool-foot"><a className="btn" href={pathFor("memory")} onClick={(e) => { e.preventDefault(); navigate(pathFor("memory")); }}>{t("panel.memory.open")}</a></div>
    </div>
  );
}

// ── context and spend ────────────────────────────────────────────────────────────────────────

/** The context's page as a tool: how full, what fills it, what the cache reused, and Compact. */
export function ContextPane({ detail, busy, onCompact }: { detail: SessionDetail; busy: boolean; onCompact: () => void }) {
  const context = detail.context;
  if (!context) return <div className="tool-empty">{t("panel.context.none")}</div>;
  const percent = context.window > 0 ? Math.round((100 * context.tokens) / context.window) : null;
  return <div className="details tool-page" data-page="context"><div className="dt-page"><ContextBody context={context} percent={percent} busy={busy} onCompact={onCompact} /></div></div>;
}

/** The spend's page as a tool: the session's table, and what its provider has left under it. */
export function UsagePane({ sessionId, detail, provider, providerUsage }: { sessionId: string; detail: SessionDetail; provider: string; providerUsage: ProviderUsage | null }) {
  return (
    <div className="details tool-page" data-page="usage">
      <div className="dt-page"><UsageBody sessionId={sessionId} usage={detail.usage} /></div>
      {provider && <section className="dt-page tool-section" data-page="provider">
        <h3 className="dt-grp">{providerLabel(provider)}</h3>
        <ProviderUsageBody usage={providerUsage} />
      </section>}
    </div>
  );
}

/**
 * The day's part beside the session's total. "$13.72 · today $0" on a chat that had worked all day read
 * as a broken counter: the total came from yesterday's metered calls, and today's calls all went
 * through a subscription route, which bills nothing per call. The numbers were right; the words did
 * not say so. A day whose calls cost nothing says they were unmetered, and the title says where the
 * total comes from and that the day is UTC's.
 */
export function spendToday(usage: SessionDetail["usage"]): { short: string; title: string } {
  const calls = usage.c_today ?? 0;
  const spent = usage.usd_today ?? 0;
  const unmetered = calls > 0 && spent === 0;
  return {
    short: unmetered ? t("strip.cost.today.unmetered") : t("strip.cost.today", { sum: fmtUsd(spent) }),
    title: t(unmetered ? "strip.cost.title.unmetered" : "strip.cost.title.day", { total: fmtUsd(usage.usd), today: fmtUsd(spent), calls }),
  };
}

/** A tool's body with nothing more than a sentence in it. */
export function ToolEmpty({ children }: { children: ReactNode }) {
  return <div className="tool-empty">{children}</div>;
}

/** Re-reads a listing when a `task.` event names this chat's family: the Overview's board block. */
export function useChatBoard(sessionId: string | null, family: readonly string[]): { tasks: BoardTask[] | undefined } {
  const key = sessionId ? `/api/board?session=${encodeURIComponent(sessionId)}&include_done=1` : null;
  const query = useQuery<BoardTask[]>(key, { staleMs: 5000 });
  const { refresh } = query;
  const soon = useRef<number | null>(null);
  useEffect(() => () => { if (soon.current) window.clearTimeout(soon.current); }, [key]);
  const members = family.join(",");
  const touched = useCallback((id: string | null) => !id || members.split(",").includes(id), [members]);
  useEvent(["task."], (event) => {
    if (!key || soon.current || !touched(event.session_id)) return;
    soon.current = window.setTimeout(() => { soon.current = null; void refresh(); }, 300);
  }, [key, touched, refresh]);
  return { tasks: Array.isArray(query.data) ? query.data : undefined };
}

export type BoardTask = { id: string; title: string; status: "todo" | "doing" | "review" | "blocked" | "done" | "dropped" | string; needs_you?: string | null; updated_at?: string };
