import { useEffect, useState } from "react";
import { api, Proposal } from "../api";
import { Pill, Skeleton } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { absTime, relTime } from "../format";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle } from "../ui/index";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { plural, t } from "../i18n";

type Filter = "all" | "pending";

export function ProposalsScreen({ toast, selected }: { toast: (t: string) => void; selected?: string | null }) {
  const { data: items, error, loading, refresh } = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 10000 });
  const [filter, setFilter] = useState<Filter>("all");
  const pending = (items ?? []).filter((p) => p.status === "pending");
  const shown = filter === "pending" ? pending : items ?? [];
  const open = selected ? (items ?? []).find((p) => p.id === selected) ?? null : null;
  const done = () => {
    refresh();
    invalidate("/api/proposals");
  };
  return (
    <>
      <PageHeader title={screenTitle("changes")} subtitle={items ? (pending.length ? plural("changes.waiting", pending.length) : plural("changes.count", items.length)) : undefined}>
        <div className="chips">
          <button className="chip select" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>{t("common.all")}</button>
          <button className="chip select" aria-pressed={filter === "pending"} onClick={() => setFilter("pending")}>{t("changes.filter.pending")}{pending.length ? ` · ${pending.length}` : ""}</button>
        </div>
      </PageHeader>
      <div className="screen narrow">
        {loading && !error && <Skeleton rows={4} />}
        {error && !items && <div className="empty"><b>{t("changes.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {items && shown.length === 0 && (
          <div className="empty">
            <b>{t(filter === "pending" ? "changes.empty.pending" : "changes.empty")}</b>
            {filter === "all" && <div>{t("changes.empty.sub")}</div>}
          </div>
        )}
        {shown.map((p) => (
          <ProposalRow key={p.id} p={p} onOpen={() => navigate(pathFor("changes", p.id))} />
        ))}
      </div>
      {open && <ProposalSheet key={open.id} p={open} toast={toast} onDone={done} onClose={() => navigate(pathFor("changes"), { replace: true })} />}
    </>
  );
}

function ProposalRow({ p, onOpen }: { p: Proposal; onOpen: () => void }) {
  return (
    <div className={`erow proposal ${p.status}`} role="link" tabIndex={0} onClick={onOpen} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-2">{p.title}</span>
          <Pill status={p.status} />
        </div>
        <div className="erow-meta">
          <span className="mono">{p.branch}</span>
          {p.pr_number && <span className="sep">·</span>}
          {p.pr_number && <span className="nowrap">PR #{p.pr_number}</span>}
          <span className="sep">·</span>
          <span title={absTime(p.created_at)}>{relTime(p.created_at)}</span>
        </div>
        {p.summary && <div className="sub clamp-3 proposal-summary">{p.summary}</div>}
        {p.reason && <div className="sub" style={{ marginTop: 4 }}><b>{t("changes.reason")}</b> {p.reason}</div>}
      </div>
    </div>
  );
}

/** One proposal in full: the summary, the diff on request, and the decision when it is still open. */
function ProposalSheet({ p, toast, onDone, onClose }: { p: Proposal; toast: (t: string) => void; onDone: () => void; onClose: () => void }) {
  const [tab, setTab] = useState<"summary" | "diff">("summary");
  const [diff, setDiff] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (tab !== "diff" || diff !== null) return;
    api.get<{ diff: string }>(`/api/proposals/${p.id}/diff`).then((r) => setDiff(r.diff)).catch((e) => setDiff(t("changes.diff.error", { error: errorText(e) })));
  }, [tab, diff, p.id]);
  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      const r = await api.post<{ result: string }>(`/api/proposals/${p.id}/decide`, { decision, reason });
      toast(r.result);
      setRejecting(false);
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={p.title} onClose={onClose} size="wide" className="proposal-sheet">
      <div className="erow-meta" style={{ marginBottom: 8 }}>
        <Pill status={p.status} />
        <span className="mono">{p.repo}</span>
        <span className="sep">·</span>
        <span className="mono">{p.branch}</span>
        {p.pr_url && <span className="sep">·</span>}
        {p.pr_url && <a className="nowrap" href={p.pr_url} target="_blank" rel="noreferrer">PR #{p.pr_number} ↗</a>}
        <span className="sep">·</span>
        <span>{absTime(p.created_at)}</span>
      </div>
      <div className="segmented" role="tablist">
        <button role="tab" aria-selected={tab === "summary"} className={tab === "summary" ? "on" : ""} onClick={() => setTab("summary")}>{t("changes.tab.summary")}</button>
        <button role="tab" aria-selected={tab === "diff"} className={tab === "diff" ? "on" : ""} onClick={() => setTab("diff")}>{t("changes.tab.diff")}</button>
      </div>
      {tab === "summary" && (
        <>
          <div className="proposal-text">{p.summary || t("changes.nosummary")}</div>
          {p.reason && <div className="sub" style={{ marginTop: 8 }}><b>{t("changes.reason")}</b> {p.reason}</div>}
        </>
      )}
      {tab === "diff" && (diff === null ? <div className="empty">{t("changes.diff.loading")}</div> : <Diff text={diff} />)}
      {p.status === "pending" && !rejecting && (
        <div className="sheet-foot">
          <button className="btn" disabled={busy} onClick={() => setRejecting(true)}>{t("inbox.reject")}</button>
          <button className="btn primary" disabled={busy} onClick={() => decide("approve")}>{t("inbox.approve")}</button>
        </div>
      )}
      {rejecting && (
        <div className="sheet-foot column">
          <label className="field">{t("inbox.reason")}</label>
          <textarea className="field" rows={3} autoFocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("inbox.reason.placeholder")} />
          <div className="btnrow" style={{ justifyContent: "flex-end" }}>
            <button className="btn ghost" onClick={() => setRejecting(false)}>{t("common.cancel")}</button>
            <button className="btn danger solid" disabled={busy} onClick={() => decide("reject")}>{t("inbox.reject.do")}</button>
          </div>
        </div>
      )}
    </Sheet>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => {
        const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : line.startsWith("diff ") ? "file" : "";
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
