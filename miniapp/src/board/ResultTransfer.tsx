import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type Artifact = { id: string; artifact_key: string };
type Host = { host_id: string; label: string; identity_state: string; ssh_configured: boolean; artifact_transfer_state: string };
type Transfer = { transfer_id: string; host_id: string; state: string; verified_bytes: number; total_bytes: number; published_digest: string | null; error_code: string | null; effect_id: string };
type Readiness = { available: boolean; reason: string; project_id: string; transfers: Transfer[] };
type Intent = { key: string; revision: number; id: string };

export function ResultTransfer({ projectId, artifacts, toast }: { projectId: string; artifacts: Artifact[]; toast: (text: string) => void }) {
  const [artifactId, setArtifactId] = useState(artifacts[0]?.id ?? "");
  const [hostId, setHostId] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const operation = useRef<Intent | null>(null);
  const offline = useOffline();
  const hosts = useQuery<{ items: Host[] }>("/api/runtime/hosts", { staleMs: 3000, pollMs: 5000 });
  const revision = useQuery<{ entity_revision: number }>(`/api/control/revisions?project=${encodeURIComponent(projectId)}`, { staleMs: 3000 });
  const readiness = useQuery<Readiness>(artifactId ? `/api/artifact-manifests/${encodeURIComponent(artifactId)}/transfer-readiness` : null, { staleMs: 3000, pollMs: 5000 });
  const selected = readiness.data?.transfers.find((item) => item.host_id === hostId) ?? null;
  const offered = (hosts.data?.items ?? []).filter((host) => host.ssh_configured && host.identity_state === "verified" && host.artifact_transfer_state === "supported");
  const canStart = !offline && !busy && !!hostId && offered.some((host) => host.host_id === hostId) && readiness.data?.available === true && readiness.data.project_id === projectId && Number.isInteger(revision.data?.entity_revision) && !hosts.error && !readiness.error && !revision.error;

  async function send(retry: boolean) {
    if (!canStart) return;
    const current = revision.data!.entity_revision;
    const key = JSON.stringify([artifactId, hostId, retry ? selected?.transfer_id : "new"]);
    if (operation.current?.key !== key) operation.current = { key, revision: current, id: crypto.randomUUID() };
    setBusy(true);
    setConflict(false);
    try {
      if (retry && selected) {
        await api.post(`/api/artifact-transfers/${encodeURIComponent(selected.transfer_id)}/retry`, {
          expected_entity_revision: operation.current.revision, client_operation_id: operation.current.id,
        });
      } else {
        await api.post(`/api/projects/${encodeURIComponent(projectId)}/artifact-transfers`, {
          manifest_id: artifactId, host_id: hostId,
          expected_entity_revision: operation.current.revision, client_operation_id: operation.current.id,
        });
      }
      operation.current = null;
      toast(t("transfer.queued"));
      readiness.refresh(); revision.refresh(); hosts.refresh();
    } catch (error) {
      toast(errorText(error));
      setConflict(error instanceof ApiError && error.status === 409);
      readiness.refresh();
    } finally { setBusy(false); }
  }

  return <details className="result-transfer">
    <summary>{t("transfer.title")}</summary>
    <p className="sub">{t("transfer.intro")}</p>
    <label className="field">{t("transfer.artifact")}
      <select className="field" value={artifactId} onChange={(event) => { setArtifactId(event.target.value); operation.current = null; setConflict(false); }}>
        {artifacts.map((artifact) => <option key={artifact.id} value={artifact.id}>{artifact.artifact_key}</option>)}
      </select>
    </label>
    <label className="field">{t("transfer.host")}
      <select className="field" value={hostId} onChange={(event) => { setHostId(event.target.value); operation.current = null; setConflict(false); }}>
        <option value="">{t("transfer.chooseHost")}</option>
        {offered.map((host) => <option key={host.host_id} value={host.host_id}>{host.label}</option>)}
      </select>
    </label>
    {(offline || hosts.error || revision.error || readiness.error) && <p className="result-warning" role="status">{t("transfer.unconfirmed")}</p>}
    {hosts.data && offered.length === 0 && <p className="sub">{t("transfer.noHost")}</p>}
    {readiness.data && !readiness.data.available && <p className="sub">{t("transfer.noFile")}</p>}
    {selected && <p className={selected.state === "failed" || selected.state === "unknown" ? "result-warning" : "sub"} role="status">
      {t(`transfer.state.${selected.state}`)}{selected.state === "published" && selected.published_digest ? ` · ${selected.verified_bytes}/${selected.total_bytes} · ${selected.published_digest}` : ""}
      {selected.error_code ? ` · ${selected.error_code}` : ""}
    </p>}
    {selected?.state === "unknown" && <p className="sub">{t("transfer.unknownHint")}</p>}
    {canStart && !selected && <button type="button" className="btn small" disabled={busy} onClick={() => void send(false)}>{t("transfer.start")}</button>}
    {canStart && (selected?.state === "failed" || selected?.state === "unknown") && <button type="button" className="btn small" disabled={busy} onClick={() => void send(true)}>{t("transfer.retry")}</button>}
    {conflict && <button type="button" className="linkbtn" onClick={() => { operation.current = null; setConflict(false); revision.refresh(); readiness.refresh(); }}>{t("transfer.refreshIntent")}</button>}
  </details>;
}
