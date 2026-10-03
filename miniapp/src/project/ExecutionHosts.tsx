import { useRef, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type Host = {
  host_id: string; label: string; entity_revision: number; identity_state: string;
  fingerprint: string; pending_fingerprint: string | null; public_key: string;
  host_generation: number; reachability: string; capabilities_state: string;
  artifact_transfer_state: string; ssh_configured: boolean; probe_error: string | null;
  observed_at: string | null; effects_allowed: boolean; blockers: string[];
};
type Hosts = { items: Host[]; collection_revision: number };
type Challenge = { challenge_id: string; host_id: string; nonce: string; public_key: string; message_base64: string; expires_at: string };
type Intent = { key: string; id: string };

export function ExecutionHosts({ toast }: { toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  return <details className="project-runtime-hosts" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("host.title")}</summary>
    {open && <HostContent toast={toast} />}
  </details>;
}

function HostContent({ toast }: { toast: (message: string) => void }) {
  const query = useQuery<Hosts>("/api/runtime/hosts", { staleMs: 3000 });
  const offline = useOffline();
  const [label, setLabel] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [sshHost, setSshHost] = useState("");
  const [sshPort, setSshPort] = useState("22");
  const [sshUser, setSshUser] = useState("");
  const [remoteRoot, setRemoteRoot] = useState("");
  const [busy, setBusy] = useState(false);
  const intent = useRef<Intent | null>(null);
  const revision = query.data?.collection_revision;

  async function enroll() {
    const name = label.trim();
    const key = publicKey.trim();
    const address = sshHost.trim();
    const sshReady = !address || (!!sshUser.trim() && !!remoteRoot.trim() && Number(sshPort) > 0 && Number(sshPort) <= 65535);
    if (busy || offline || !name || !key || !Number.isInteger(revision) || !sshReady) return;
    const transport = address ? { ssh_host: address, ssh_port: Number(sshPort), ssh_user: sshUser.trim(), remote_root: remoteRoot.trim() } : {};
    const signature = JSON.stringify([name, key, revision, transport]);
    if (intent.current?.key !== signature) intent.current = { key: signature, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post("/api/runtime/hosts", { label: name, public_key: key, ...transport, expected_collection_revision: revision, client_operation_id: intent.current.id });
      intent.current = null;
      setLabel(""); setPublicKey("");
      setSshHost(""); setSshPort("22"); setSshUser(""); setRemoteRoot("");
      toast(t("host.enrolled"));
      query.refresh();
    } catch (error) { toast(errorText(error)); query.refresh(); }
    finally { setBusy(false); }
  }

  return <div className="project-extension-list">
    <p className="sub">{t("host.intro")}</p>
    {query.error && <div className="result-warning" role="status">{t("host.unknown")} <button type="button" className="linkbtn" onClick={query.refresh}>{t("common.retry")}</button></div>}
    {query.data?.items.length === 0 && <div className="sub">{t("host.none")}</div>}
    {query.data?.items.map((host) => <HostCard key={host.host_id} host={host} revision={revision} refresh={query.refresh} offline={offline} toast={toast} />)}
    <details>
      <summary>{t("host.pair")}</summary>
      <p className="sub">{t("host.pairHint")}</p>
      <label className="field">{t("host.name")}<input className="field" value={label} maxLength={120} onChange={(event) => setLabel(event.target.value)} /></label>
      <label className="field">{t("host.publicKey")}<input className="field mono" value={publicKey} maxLength={48} autoComplete="off" onChange={(event) => setPublicKey(event.target.value)} /></label>
      <details><summary>{t("host.sshSettings")}</summary>
        <p className="sub">{t("host.sshHint")}</p>
        <label className="field">{t("host.sshAddress")}<input className="field" value={sshHost} maxLength={253} onChange={(event) => setSshHost(event.target.value)} /></label>
        <label className="field">{t("host.sshPort")}<input className="field" type="number" min="1" max="65535" value={sshPort} onChange={(event) => setSshPort(event.target.value)} /></label>
        <label className="field">{t("host.sshUser")}<input className="field" value={sshUser} maxLength={64} onChange={(event) => setSshUser(event.target.value)} /></label>
        <label className="field">{t("host.remoteRoot")}<input className="field" value={remoteRoot} maxLength={256} onChange={(event) => setRemoteRoot(event.target.value)} /></label>
      </details>
      <button type="button" className="btn small" disabled={busy || offline || !Number.isInteger(revision) || !label.trim() || !publicKey.trim() || (!!sshHost.trim() && (!sshUser.trim() || !remoteRoot.trim() || Number(sshPort) < 1 || Number(sshPort) > 65535))} onClick={() => void enroll()}>{t("host.recordKey")}</button>
    </details>
  </div>;
}

function HostCard({ host, revision, refresh, offline, toast }: { host: Host; revision: number | undefined; refresh: () => void; offline: boolean; toast: (message: string) => void }) {
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [signature, setSignature] = useState("");
  const [candidateKey, setCandidateKey] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const intent = useRef<Intent | null>(null);
  const canWrite = !offline && !busy && Number.isInteger(revision);

  async function requestChallenge() {
    if (!canWrite) return;
    const proposed = candidateKey.trim() || null;
    const key = JSON.stringify([host.host_id, host.entity_revision, revision, proposed]);
    if (intent.current?.key !== key) intent.current = { key, id: crypto.randomUUID() };
    setBusy(true);
    try {
      const result = await api.post<Challenge>(`/api/runtime/hosts/${encodeURIComponent(host.host_id)}/challenge`, {
        expected_collection_revision: revision, expected_host_revision: host.entity_revision,
        candidate_public_key: proposed, client_operation_id: intent.current.id,
      });
      setChallenge(result);
      intent.current = null;
      refresh();
    } catch (error) { toast(errorText(error)); refresh(); }
    finally { setBusy(false); }
  }

  async function probe() {
    if (!canWrite || !host.ssh_configured) return;
    const key = JSON.stringify([host.host_id, host.entity_revision, revision, "probe"]);
    if (intent.current?.key !== key) intent.current = { key, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`/api/runtime/hosts/${encodeURIComponent(host.host_id)}/probe`, {
        expected_collection_revision: revision, expected_host_revision: host.entity_revision, client_operation_id: intent.current.id,
      });
      intent.current = null;
      toast(t("host.probeRecorded"));
      refresh();
    } catch (error) { toast(errorText(error)); refresh(); }
    finally { setBusy(false); }
  }

  async function observe() {
    if (!canWrite || !challenge || !signature.trim()) return;
    setBusy(true);
    try {
      await api.post(`/api/runtime/hosts/${encodeURIComponent(host.host_id)}/observe`, {
        challenge_id: challenge.challenge_id, nonce: challenge.nonce, public_key: challenge.public_key, signature: signature.trim(),
      });
      setChallenge(null); setSignature("");
      toast(t("host.proofRecorded"));
      refresh();
    } catch (error) { toast(errorText(error)); refresh(); }
    finally { setBusy(false); }
  }

  async function decide(decision: "accept_rotation" | "reject") {
    if (!canWrite || !reason.trim()) return;
    const key = JSON.stringify([host.host_id, host.entity_revision, host.host_generation, revision, decision, reason.trim()]);
    if (intent.current?.key !== key) intent.current = { key, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`/api/runtime/hosts/${encodeURIComponent(host.host_id)}/decision`, {
        decision, reason: reason.trim(), observed_generation: host.host_generation,
        expected_host_revision: host.entity_revision, expected_collection_revision: revision, client_operation_id: intent.current.id,
      });
      intent.current = null; setReason("");
      toast(t("host.decisionRecorded"));
      refresh();
    } catch (error) { toast(errorText(error)); refresh(); }
    finally { setBusy(false); }
  }

  return <article className="project-extension">
    <div className="project-extension-head"><b>{host.label}</b><span className="sub">{t(`host.state.${host.identity_state}`)}</span></div>
    <p className="sub" role="status">{host.effects_allowed ? t("host.capabilityUnknown") : t("host.blocked")}</p>
    {host.ssh_configured && <div>
      <p className="sub">{host.artifact_transfer_state === "supported" ? t("host.transferSupported") : t("host.transferUnknown")}</p>
      {host.probe_error && <p className="result-warning" role="status">{t("host.probeFailed")}: {host.probe_error}</p>}
      <button type="button" className="btn small" disabled={!canWrite || host.identity_state === "changed" || host.identity_state === "rejected"} onClick={() => void probe()}>{t("host.probe")}</button>
    </div>}
    <details><summary>{t("host.identityDetails")}</summary>
      <div className="mono">{t("host.pinned")}: {host.fingerprint}</div>
      {host.pending_fingerprint && <div className="mono">{t("host.observed")}: {host.pending_fingerprint}</div>}
      <div className="sub">{t("host.reachability")}: {t(`host.reach.${host.reachability}`)} · {t("host.capabilityUnknown")}</div>
    </details>
    {(host.identity_state === "pending" || host.identity_state === "rotating" || host.identity_state === "verified") && <div>
      {host.identity_state === "verified" && <label className="field">{t("host.candidateKey")}<input className="field mono" value={candidateKey} maxLength={48} autoComplete="off" onChange={(event) => setCandidateKey(event.target.value)} /></label>}
      <button type="button" className="btn small" disabled={!canWrite} onClick={() => void requestChallenge()}>{t("host.challenge")}</button>
      {challenge && <div>
        <p className="sub">{t("host.challengeHint")}</p>
        <div className="mono" role="status">{challenge.message_base64}</div>
        <label className="field">{t("host.signature")}<input className="field mono" value={signature} maxLength={96} autoComplete="off" onChange={(event) => setSignature(event.target.value)} /></label>
        <button type="button" className="btn small" disabled={!canWrite || !signature.trim()} onClick={() => void observe()}>{t("host.verify")}</button>
      </div>}
    </div>}
    {host.identity_state === "changed" && <div>
      <p className="result-warning">{t("host.changedHint")}</p>
      <label className="field">{t("host.reason")}<textarea className="field" value={reason} maxLength={1000} onChange={(event) => setReason(event.target.value)} /></label>
      <div className="btnrow">
        <button type="button" className="btn small" disabled={!canWrite || !reason.trim()} onClick={() => void decide("accept_rotation")}>{t("host.acceptKey")}</button>
        <button type="button" className="btn small ghost" disabled={!canWrite || !reason.trim()} onClick={() => void decide("reject")}>{t("host.rejectKey")}</button>
      </div>
    </div>}
  </article>;
}
