import { useState } from "react";
import { Proposal } from "../api";
import { Pill, Skeleton } from "../ui/components";
import { absTime, relTime } from "../format";
import { navigate, pathFor } from "../router";
import { Page } from "../shell";
import { PageHeader, screenTitle } from "../ui/index";
import { invalidate, useQuery } from "../store";
import { plural, t } from "../i18n";
import { ChangeReview } from "./ChangeReview";

type Filter = "all" | "pending";

/** The changes the agent proposes to its own code: the list, and one of them opened for review. */
export function ProposalsScreen({ toast, selected }: { toast: (t: string) => void; selected?: string | null }) {
  const { data: items, error, loading, refresh } = useQuery<Proposal[]>("/api/proposals", { pollMs: 60000, staleMs: 10000 });
  const open = selected ? (items ?? []).find((p) => p.id === selected) ?? null : null;
  const done = () => {
    refresh();
    invalidate("/api/proposals");
  };
  if (open && items) return <ChangeReview key={open.id} p={open} items={items} toast={toast} onDecided={done} />;
  return <ProposalList items={items} error={error} loading={loading} refresh={refresh} missing={selected && items ? selected : null} />;
}

function ProposalList({ items, error, loading, refresh, missing }: { items: Proposal[] | undefined; error: string | null; loading: boolean; refresh: () => void; missing: string | null }) {
  const [filter, setFilter] = useState<Filter>("all");
  const pending = (items ?? []).filter((p) => p.status === "pending");
  const shown = filter === "pending" ? pending : items ?? [];
  return (
    <>
      <PageHeader title={screenTitle("changes")} subtitle={items ? (pending.length ? plural("changes.waiting", pending.length) : plural("changes.count", items.length)) : undefined}
        ruled
        filters={
        <div className="chips">
          <button className="chip select" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>{t("common.all")}</button>
          <button className="chip select" aria-pressed={filter === "pending"} onClick={() => setFilter("pending")}>{t("changes.filter.pending")}{pending.length ? ` · ${pending.length}` : ""}</button>
        </div>
        }
      />
      <Page width="list">
        {missing && <div className="sub changes-missing" role="alert">{t("changes.missing")}</div>}
        {loading && !error && !items && <Skeleton rows={4} />}
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
      </Page>
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
        {p.summary && <div className="sub clamp-2 proposal-summary">{p.summary}</div>}
        {p.reason && <div className="sub clamp-2"><b>{t("changes.reason")}</b> {p.reason}</div>}
      </div>
    </div>
  );
}
