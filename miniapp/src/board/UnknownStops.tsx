import { useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type UnknownStop = {
  id: string; task_id: string; state: string; host_generation: number;
  staff_session_id: string | null; runtime_kind: string; provider_session_ref: string;
  native_run_id: string | null; runtime_instance: string | null;
  parent_kind: string; parent_id: string; generation: number; cancel_state: string;
  updated_at: string; phase: string | null; deadline_at: string | null;
  exit_observed: boolean; no_entry_observed: boolean; generation_matches_host_record: boolean;
  recovery_blocker: "ready" | "previous_host" | "runtime_identity_missing" | "exit_unobserved" | "containment_unavailable" | "container_not_empty";
  exit_evidence: { runtime_ref: string; host_generation: number; contract_revision: number; observed_status: string; observed_at: string } | null;
  no_entry_evidence: { host_generation: number; contract_revision: number; observed_at: string } | null;
  containment_evidence: { source: "profile" | "writer"; state: string; host_generation: string;
    latest_observation: ContainmentObservation | null; release_observation: ContainmentObservation | null;
    stale_observation: ContainmentObservation | null } | null;
};
type ContainmentObservation = { id: string; host_generation: string; observation_kind: string;
  enforced: number; populated: number | null; observed_at: string };
type Inspection = { items: UnknownStop[]; next_after: string | null };

function observationLine(row: ContainmentObservation): string {
  return t("pboard.unknown.proof.observation", { id: row.id, kind: row.observation_kind,
    host: row.host_generation, enforced: t(row.enforced ? "pboard.unknown.yes" : "pboard.unknown.no"),
    populated: row.populated === null ? "—" : t(row.populated ? "pboard.unknown.yes" : "pboard.unknown.no"),
    at: row.observed_at });
}

/** Keep uncertain stops beside the board's work, with an explicit read after each reconcile. */
export function UnknownStops({ projectId }: { projectId: string }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/unknown-stops`;
  const first = useQuery<Inspection>(`${base}?limit=25`, { staleMs: 0 });
  const [extra, setExtra] = useState<UnknownStop[]>([]);
  const [next, setNext] = useState<string | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [warning, setWarning] = useState("");
  const [outcome, setOutcome] = useState("");
  const offline = useOffline();
  const items = [...(first.data?.items ?? []), ...extra];
  const cursor = next === undefined ? first.data?.next_after : next;

  async function reload() {
    setExtra([]);
    setNext(undefined);
    await first.refresh();
  }

  async function more() {
    if (!cursor || busy) return;
    setBusy("more");
    setWarning("");
    try {
      const page = await api.get<Inspection>(`${base}?limit=25&after=${encodeURIComponent(cursor)}`);
      setExtra((rows) => [...rows, ...page.items]);
      setNext(page.next_after);
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(null); }
  }

  async function reconcile(id: string) {
    if (busy || offline) return;
    setBusy(id);
    setWarning("");
    setOutcome("");
    try {
      const result = await api.post<{ cancel_state: string }>(`${base}/${encodeURIComponent(id)}/reconcile`, {});
      setOutcome(t("pboard.unknown.result", { id, state: result.cancel_state }));
    } catch (error) { setWarning(errorText(error)); }
    finally {
      await reload();
      setBusy(null);
    }
  }

  // An empty inspection leaves the usual board entirely quiet.
  if (!items.length && !outcome && !warning && !first.error) return null;
  return <details className="pboard-unknown-stops" open={!!outcome || !!warning}>
    <summary>{first.error && !items.length ? t("pboard.unknown.unavailable") : t("pboard.unknown.title", { count: items.length })}</summary>
    <p className="sub">{t("pboard.unknown.help")}</p>
    {first.error && <p className="result-warning" role="alert">{first.error}</p>}
    {warning && <p className="result-warning" role="alert">{warning}</p>}
    {outcome && <p className="sub" role="status">{outcome}</p>}
    {items.map((item) => <div className="pboard-unknown-row" key={item.id}>
      <div><b>{item.task_id}</b> · <code>{item.id}</code></div>
      <div className="sub">{t("pboard.unknown.phase", { phase: item.phase ?? "—", deadline: item.deadline_at ?? "—" })}</div>
      <p className="sub" role="status">{t(`pboard.unknown.blocker.${item.recovery_blocker}`)}</p>
      <details><summary>{t("pboard.unknown.observation")}</summary>
        <dl>
          {(["state", "cancel_state", "parent_kind", "parent_id", "generation", "host_generation",
            "generation_matches_host_record", "staff_session_id", "runtime_kind", "provider_session_ref",
            "native_run_id", "runtime_instance", "exit_observed", "no_entry_observed", "updated_at"] as const).map((key) =>
            <div key={key}><dt>{t(`pboard.unknown.${key}`)}</dt><dd><code>{typeof item[key] === "boolean" ? t(item[key] ? "pboard.unknown.yes" : "pboard.unknown.no") : String(item[key] ?? "—")}</code></dd></div>)}
          {item.exit_evidence && <div><dt>{t("pboard.unknown.proof.exit")}</dt><dd><code>{t("pboard.unknown.proof.exitLine", {
            ref: item.exit_evidence.runtime_ref, status: item.exit_evidence.observed_status,
            host: item.exit_evidence.host_generation, revision: item.exit_evidence.contract_revision,
            at: item.exit_evidence.observed_at })}</code></dd></div>}
          {item.no_entry_evidence && <div><dt>{t("pboard.unknown.proof.noEntry")}</dt><dd><code>{t("pboard.unknown.proof.noEntryLine", {
            host: item.no_entry_evidence.host_generation, revision: item.no_entry_evidence.contract_revision,
            at: item.no_entry_evidence.observed_at })}</code></dd></div>}
          {item.runtime_kind === "cli" && <div><dt>{t("pboard.unknown.proof.containment")}</dt><dd><code>{item.containment_evidence
            ? t("pboard.unknown.proof.binding", { source: t(`pboard.unknown.proof.${item.containment_evidence.source}`),
              state: item.containment_evidence.state, host: item.containment_evidence.host_generation })
            : t("pboard.unknown.proof.missing")}</code></dd></div>}
          {item.containment_evidence?.latest_observation &&
            item.containment_evidence.latest_observation.id !== item.containment_evidence.release_observation?.id &&
            <div><dt>{t("pboard.unknown.proof.latest")}</dt><dd><code>{observationLine(item.containment_evidence.latest_observation)}</code></dd></div>}
          {item.containment_evidence?.release_observation && <div><dt>{t("pboard.unknown.proof.release")}</dt>
            <dd><code>{observationLine(item.containment_evidence.release_observation)}</code></dd></div>}
          {item.containment_evidence?.stale_observation && <div><dt>{t("pboard.unknown.proof.stale")}</dt>
            <dd><code>{observationLine(item.containment_evidence.stale_observation)}</code></dd></div>}
        </dl>
      </details>
      <button type="button" className="linkbtn" disabled={!!busy || offline || item.recovery_blocker !== "ready"} onClick={() => void reconcile(item.id)}>{t("pboard.unknown.reconcile")}</button>
    </div>)}
    {cursor && <button type="button" className="linkbtn" disabled={!!busy} onClick={() => void more()}>{t("pboard.unknown.more")}</button>}
    <button type="button" className="linkbtn" disabled={!!busy} onClick={() => void reload()}>{t("pboard.unknown.reload")}</button>
  </details>;
}
