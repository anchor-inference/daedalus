import { useEffect, useMemo, useRef, useState } from "react";
import { api, Proposal } from "../api";
import { diffFileName, parseDiff, splitPath, type DiffFile } from "../diff";
import { Counts, FileDiffBody, FileDiffHeader, type DiffMode } from "../diffpane";
import { groupByFolder, nextToView } from "../changetree";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, pathFor } from "../router";
import { useQuery } from "../store";
import { errorText } from "../ui";
import { Pill, Skeleton } from "../ui/components";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { Button, Popover, Segmented, Sheet } from "../ui/primitives";
import { readMode, readViewed, writeMode, writeViewed } from "../viewedfiles";
import "./changes.css";

/** The host cuts a pull request's diff at this many characters, so a diff this long was cut. */
const HOST_DIFF_LIMIT = 400_000;

type PickFilter = "all" | "pending";

/**
 * One change set in full: the files on the left with what has been read, the agent's own account of
 * the change above the file on the right, and the decision in the page's header. What the host cannot
 * give (a base branch, a commit message of its own, line notes sent to the chat) is not drawn.
 */
export function ChangeReview({ p, items, toast, onDecided }: { p: Proposal; items: Proposal[]; toast: (text: string) => void; onDecided: () => void }) {
  const phone = useMedia("(max-width: 1023px)");
  const diff = useQuery<{ diff: string }>(`/api/proposals/${p.id}/diff`, { staleMs: 60000 });
  const text = diff.data?.diff ?? "";
  const files = useMemo(() => parseDiff(text), [text]);
  const paths = useMemo(() => files.map(diffFileName), [files]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [viewed, setViewed] = useState(() => readViewed(p.id));
  const [mode, setMode] = useState<DiffMode>(readMode);
  const [filesOpen, setFilesOpen] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const current = files.find((f) => diffFileName(f) === chosen) ?? files[0] ?? null;
  const currentPath = current ? diffFileName(current) : "";
  const seen = paths.filter((path) => viewed.has(path)).length;
  const next = nextToView(paths, currentPath, viewed);
  const added = files.reduce((n, f) => n + f.added, 0);
  const removed = files.reduce((n, f) => n + f.removed, 0);
  const cut = text.length >= HOST_DIFF_LIMIT;

  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 });
  }, [currentPath]);

  const mark = (path: string, on: boolean) => {
    const now = new Set(viewed);
    if (on) now.add(path);
    else now.delete(path);
    setViewed(now);
    writeViewed(p.id, now);
  };
  const pick = (path: string) => {
    setChosen(path);
    setFilesOpen(false);
  };
  const changeMode = (m: DiffMode) => {
    setMode(m);
    writeMode(m);
  };
  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      const answer = await api.post<{ result: string }>(`/api/proposals/${p.id}/decide`, { decision, reason: decision === "reject" ? reason : "" });
      setRejecting(false);
      // The host answers a failed merge in words with the request itself succeeding, so the words are what the operator needs.
      if (answer.result) toast(answer.result);
      onDecided();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const decision = p.status === "pending" ? (
    <>
      <Button variant="quiet" disabled={busy} onClick={() => setRejecting(true)}>{t("changes.reject")}</Button>
      <Button variant="primary" busy={busy && !rejecting} onClick={() => void decide("approve")}>{t("inbox.approve")}</Button>
    </>
  ) : null;

  return (
    <>
      <PageHeader title={screenTitle("changes")} ruled back={phone ? pathFor("changes") : undefined}
        filters={<ChangePicker current={p} items={items} />}
        actions={phone ? undefined : decision ?? <Pill status={p.status} />} />
      <div className="review">
        <section className={`review-files ${filesOpen ? "open" : ""}`} aria-label={t("changes.files.label")}>
          <div className="review-files-head">
            <span className="review-files-count">{plural("changes.files", files.length)}</span>
            <span className="review-totals"><Counts added={added} removed={removed} /></span>
            {phone && (
              <button type="button" className="review-files-toggle" aria-expanded={filesOpen} onClick={() => setFilesOpen(!filesOpen)}>
                <span className="mono">{splitPath(currentPath).name}</span>
                <Icon name="chevron" size={14} />
              </button>
            )}
          </div>
          <div className="review-files-body">
            <label className="review-filter">
              <Icon name="search" size={16} />
              <input type="text" placeholder={t("changes.files.filter")} aria-label={t("changes.files.filter")} value={query} onChange={(e) => setQuery(e.target.value)} />
            </label>
            <div className="review-progress">
              <div>{t("changes.progress", { done: seen, total: files.length })}</div>
              <div className="review-bar" role="progressbar" aria-label={t("changes.progress.bar")} aria-valuemin={0} aria-valuemax={files.length} aria-valuenow={seen}>
                <div style={{ width: files.length ? `${(100 * seen) / files.length}%` : 0 }} />
              </div>
            </div>
            <FileTree files={files} query={query} current={currentPath} viewed={viewed} onPick={pick} onMark={mark} />
            <ChangeFacts p={p} />
          </div>
        </section>
        <section className="review-diff" aria-label={t("changes.diff.label")}>
          <div className="review-scroll" ref={scroller}>
            <AgentCard p={p} />
            {diff.loading && !diff.data && <div className="review-wait" aria-busy="true"><Skeleton rows={3} /></div>}
            {diff.error && !diff.data && (
              <div className="empty"><b>{t("changes.diff.error", { error: diff.error })}</b><button className="btn" onClick={() => void diff.refresh()}>{t("common.retry")}</button></div>
            )}
            {diff.data && !text.trim() && <div className="empty"><b>{t("changes.diff.empty")}</b></div>}
            {diff.data && text.trim() && !current && <pre className="filetext">{text}</pre>}
            {current && (
              <>
                <FileDiffHeader file={current} viewed={viewed.has(currentPath)} onViewed={(on) => mark(currentPath, on)} mode={phone ? undefined : mode} onMode={phone ? undefined : changeMode} />
                <FileDiffBody key={currentPath} file={current} mode={phone ? "unified" : mode}
                  cut={cut && currentPath === paths[paths.length - 1] ? t("changes.diff.cut", { kb: Math.round(HOST_DIFF_LIMIT / 1000) }) : undefined} />
                <div className="review-next">
                  <span>{next ? t("changes.next", { path: next }) : t(p.status === "pending" ? "changes.lastFile.pending" : "changes.lastFile")}</span>
                  {next && (
                    <Button size="sm" icon="forward" onClick={() => { mark(currentPath, true); pick(next); }}>{t("changes.markNext")}</Button>
                  )}
                </div>
              </>
            )}
          </div>
        </section>
      </div>
      {phone && decision && <div className="review-decide">{decision}</div>}
      {rejecting && (
        <Sheet title={t("inbox.reject.title")} size="sm" onClose={() => !busy && setRejecting(false)}
          footer={<>
            <Button variant="quiet" onClick={() => setRejecting(false)}>{t("common.cancel")}</Button>
            <Button variant="danger" busy={busy} onClick={() => void decide("reject")}>{t("inbox.reject.do")}</Button>
          </>}>
          <label className="field" htmlFor="change-reason">{t("inbox.reason")}</label>
          <textarea id="change-reason" className="field" rows={4} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("inbox.reason.placeholder")} />
        </Sheet>
      )}
    </>
  );
}

/** The change set in the header, and the list of the others behind it. */
function ChangePicker({ current, items }: { current: Proposal; items: Proposal[] }) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<PickFilter>("all");
  const trigger = useRef<HTMLButtonElement>(null);
  const pending = items.filter((x) => x.status === "pending");
  const shown = filter === "pending" ? pending : items;
  return (
    <>
      <button ref={trigger} type="button" className="changes-chip" aria-haspopup="listbox" aria-expanded={open} aria-label={t("changes.pick.for", { title: current.title })} onClick={() => setOpen(!open)}>
        <span className={`changes-dot ${current.status}`} aria-hidden="true" />
        <span className="changes-chip-title">{current.title}</span>
        <span className="mono changes-chip-branch">{current.branch}</span>
        <Icon name="chevron" size={14} />
      </button>
      {open && (
        <Popover anchor={trigger.current} onClose={() => setOpen(false)} label={t("changes.pick.list")} className="changes-pick" width={480}>
          <Segmented<PickFilter> value={filter} onChange={setFilter} label={t("changes.pick.list")} className="changes-pick-filter"
            options={[
              { id: "all", label: t("common.all"), badge: <span className="changes-count">{items.length}</span> },
              { id: "pending", label: t("changes.filter.pending"), badge: <span className="changes-count">{pending.length}</span> },
            ]} />
          <div role="listbox" aria-label={t("changes.pick.list")} className="changes-pick-list">
            {shown.map((x) => (
              <button key={x.id} type="button" role="option" aria-selected={x.id === current.id} className="changes-pick-row" onClick={() => { setOpen(false); if (x.id !== current.id) navigate(pathFor("changes", x.id)); }}>
                <span className="changes-pick-main">
                  <span className="changes-pick-title">{x.title}</span>
                  <span className="mono changes-pick-meta">{x.branch}{x.pr_number ? ` · PR #${x.pr_number}` : ""} · {relTime(x.created_at)}</span>
                </span>
                <span className={`changes-pick-state ${x.status}`}>{statusLabel(x.status)}</span>
                <span className="changes-pick-check">{x.id === current.id && <Icon name="check" size={16} />}</span>
              </button>
            ))}
            {!shown.length && <div className="changes-pick-none">{t("changes.empty.pending")}</div>}
          </div>
        </Popover>
      )}
    </>
  );
}

function statusLabel(status: string): string {
  const key = `status.${status}`;
  return ["pending", "merged", "rejected"].includes(status) ? t(key) : status;
}

/** The files by folder, each with a box to mark it read, its counts, and a word if it is new. */
function FileTree({ files, query, current, viewed, onPick, onMark }: { files: DiffFile[]; query: string; current: string; viewed: ReadonlySet<string>; onPick: (path: string) => void; onMark: (path: string, on: boolean) => void }) {
  const groups = useMemo(() => groupByFolder(files, query), [files, query]);
  const [closed, setClosed] = useState<ReadonlySet<string>>(new Set());
  if (!groups.length) return <div className="review-none">{files.length ? t("changes.files.none") : ""}</div>;
  return (
    <div role="list" className="review-tree" aria-label={t("changes.files.tree")}>
      {groups.map((group) => (
        <div key={group.dir} role="listitem" className="review-group">
          {group.dir && (
            <button type="button" className="review-dir" aria-expanded={!closed.has(group.dir)}
              onClick={() => setClosed((now) => { const copy = new Set(now); if (!copy.delete(group.dir)) copy.add(group.dir); return copy; })}>
              <Icon name={closed.has(group.dir) ? "forward" : "chevron"} size={14} />
              <span className="mono">{group.dir}</span>
            </button>
          )}
          {!closed.has(group.dir) && group.files.map((file) => {
            const path = diffFileName(file);
            const read = viewed.has(path);
            return (
              <div key={path} className={`review-file ${path === current ? "on" : ""} ${read ? "read" : ""}`}>
                <button type="button" className="review-check" role="checkbox" aria-checked={read} aria-label={t("changes.viewedFile", { path })} onClick={() => onMark(path, !read)}>
                  <span className={`dp-box ${read ? "on" : ""}`} aria-hidden="true">{read && <Icon name="check" size={12} />}</span>
                </button>
                <button type="button" className="review-pick" aria-current={path === current ? "true" : undefined} onClick={() => onPick(path)} title={path}>
                  <span className="mono review-fname">{splitPath(path).name}</span>
                  {file.kind === "added" && <span className="review-new">{t("changes.new")}</span>}
                  <span className="mono review-n add">{file.added ? `+${file.added}` : ""}</span>
                  <span className="mono review-n del">{file.removed ? `−${file.removed}` : ""}</span>
                </button>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/** What the host knows about the change set besides its files. */
function ChangeFacts({ p }: { p: Proposal }) {
  return (
    <dl className="review-facts">
      <dt className="review-facts-head">{t("changes.about")}</dt>
      <dd><span>{t("changes.about.repo")}</span><span className="mono">{p.repo}</span></dd>
      <dd><span>{t("changes.about.branch")}</span><span className="mono">{p.branch}</span></dd>
      {p.pr_number && (
        <dd><span>{t("changes.about.pr")}</span>{p.pr_url ? <a className="mono" href={p.pr_url} target="_blank" rel="noreferrer">#{p.pr_number} ↗</a> : <span className="mono">#{p.pr_number}</span>}</dd>
      )}
      <dd><span>{t("changes.about.opened")}</span><span title={absTime(p.created_at)}>{relTime(p.created_at)}</span></dd>
      {p.decided_at && <dd><span>{t("changes.about.decided")}</span><span title={absTime(p.decided_at)}>{relTime(p.decided_at)}</span></dd>}
    </dl>
  );
}

/** The agent's own words about the change, with the way to the chat that made it and to the pull request. */
function AgentCard({ p }: { p: Proposal }) {
  return (
    <div className="review-card">
      <div className="review-card-text">
        <div className="review-label">{t("changes.agent")}</div>
        <div className="review-summary">{p.summary || t("changes.nosummary")}</div>
        {p.reason && <div className="review-reason"><b>{t("changes.reason")}</b> {p.reason}</div>}
      </div>
      <div className="review-card-links">
        {p.session_id && (
          <button type="button" className="review-link" onClick={() => navigate(pathFor("agents", p.session_id))}><Icon name="link" size={14} />{t("changes.openChat")}</button>
        )}
        {p.pr_url && (
          <a className="review-link" href={p.pr_url} target="_blank" rel="noreferrer"><Icon name="external" size={14} />{t("changes.openPr")}</a>
        )}
      </div>
    </div>
  );
}
