import { useState } from "react";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import type { ProjectTask, TeamMember } from "./board";

type Role = "worker" | "reviewer" | "orchestrator";
type Item = { id?: string; text?: string; claim?: string };
type Packet = { task_id: string; title: string; role: Role; role_hint?: string; contract_revision: number;
  contract: { brief?: Record<string, string>; requirements?: Item[]; checklist?: Item[]; acceptance?: string };
  dependencies?: { task_id: string; title: string; result_digest?: string | null }[];
  facts: { fact_id: string; claim: string; source?: string; source_kind?: string; source_id?: string }[];
  artifacts: { id: string; key: string; kind: string; digest: string }[];
  source_refs: string[]; packet_hash: string };
type HistoryEntry = { staff_session_id: string; staff_id: string; role: Role; role_hint: string;
  contract_revision: number; packet_hash: string; source_refs: string[];
  source_current: boolean; current_packet_hash: string | null; created_at: string; session_ended_at: string | null };
type History = { task_id: string; entries: HistoryEntry[] };
type HistoricalPacket = HistoryEntry & { packet: Packet };

function sourceLabel(kind?: string): string {
  return t(["file", "manifest", "run"].includes(kind ?? "") ? `taskContext.source.${kind}` : "taskContext.source.other");
}

function PacketView({ packet }: { packet: Packet }) {
  const brief = packet.contract?.brief ?? {};
  return <div className="project-extension-list">
    <p className="sub">{t("taskContext.contract", { n: packet.contract_revision })}</p>
    {(["objective", "deliverable", "boundaries", "done_when"] as const).filter((key) => brief[key]).map((key) =>
      <p key={key}><b>{t(`pboard.brief.${key}`)}: </b>{brief[key]}</p>)}
    {!!packet.contract?.acceptance && <p><b>{t("taskContext.acceptance")}: </b>{packet.contract.acceptance}</p>}
    {!!packet.contract?.requirements?.length && <div><b>{t("taskContext.requirements")}</b><ul>{packet.contract.requirements.map((item, index) => <li key={item.id ?? index}>{item.text}</li>)}</ul></div>}
    {!!packet.contract?.checklist?.length && <div><b>{t("taskContext.checks")}</b><ul>{packet.contract.checklist.map((item, index) => <li key={item.id ?? index}>{item.text}</li>)}</ul></div>}
    {!!packet.dependencies?.length && <div><b>{t("taskContext.dependencies")}</b><ul>{packet.dependencies.map((item) => <li key={item.task_id}>{item.title}</li>)}</ul></div>}
    <div><b>{t("taskContext.facts")}</b>{packet.facts.length ? <ul>{packet.facts.map((fact) =>
      <li key={fact.fact_id}>{fact.claim} <span className="sub">· {sourceLabel(fact.source_kind)}</span></li>)}</ul> : <p className="sub">{t("taskContext.none")}</p>}</div>
    <div><b>{t("taskContext.artifacts")}</b>{packet.artifacts.length ? <ul>{packet.artifacts.map((artifact) =>
      <li key={artifact.id}>{artifact.key} <span className="sub">· {artifact.kind}</span></li>)}</ul> : <p className="sub">{t("taskContext.none")}</p>}</div>
    <details><summary>{t("taskContext.technical")}</summary><p className="mono">{packet.packet_hash}</p>
      <ul>{packet.source_refs.map((ref, index) => <li className="mono" key={`${ref}:${index}`}>{ref}</li>)}</ul>
    </details>
  </div>;
}

export function TaskContext({ task, staff }: { task: ProjectTask; staff: TeamMember[] }) {
  const [open, setOpen] = useState(false);
  return <section className="task-workflow">
    <button type="button" className="pboard-fold section-title" aria-expanded={open} onClick={() => setOpen(!open)}>{t("taskContext.title")}</button>
    {open && <ContextContent task={task} staff={staff} />}
  </section>;
}

function ContextContent({ task, staff }: { task: ProjectTask; staff: TeamMember[] }) {
  const [role, setRole] = useState<Role>("worker");
  const [staffId, setStaffId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const offline = useOffline();
  const base = `/api/board/${encodeURIComponent(task.id)}`;
  const preview = useQuery<Packet>(`${base}/context?role=${role}${role === "worker" && staffId ? `&staff_id=${encodeURIComponent(staffId)}` : ""}`, { staleMs: 0 });
  const history = useQuery<History>(`${base}/context-history?limit=25`, { staleMs: 0 });
  const entry = history.data?.entries.find((item) => item.staff_session_id === sessionId);
  const supplied = useQuery<HistoricalPacket>(entry ? `${base}/context-history/${encodeURIComponent(entry.staff_session_id)}` : null, { staleMs: 0 });
  const currentPreview = !preview.error && preview.data?.task_id === task.id ? preview.data : null;
  const currentHistory = !history.error && history.data?.task_id === task.id ? history.data : null;
  const currentSupplied = !supplied.error && supplied.data?.staff_session_id === entry?.staff_session_id ? supplied.data : null;
  return <div className="task-workflow-body">
    <p className="sub">{t("taskContext.intro")}</p>
    {offline && <p className="result-warning" role="status">{t("taskContext.offline")}</p>}
    <div className="project-extension">
      <b>{t("taskContext.preview")}</b>
      <p className="sub">{t("taskContext.previewHelp")}</p>
      <label className="field">{t("taskContext.role")}
        <select className="field" value={role} onChange={(event) => setRole(event.target.value as Role)}>
          {(["worker", "reviewer", "orchestrator"] as Role[]).map((value) => <option key={value} value={value}>{t(`taskContext.role.${value}`)}</option>)}
        </select>
      </label>
      {role === "worker" && <label className="field">{t("taskContext.member")}
        <select className="field" value={staffId} onChange={(event) => setStaffId(event.target.value)}>
          <option value="">{t("taskContext.generic")}</option>
          {staff.map((member) => <option value={member.id} key={member.id}>{member.name}</option>)}
        </select>
      </label>}
      {preview.error && <div className="result-warning" role="status">{t("taskContext.unavailable")} <button type="button" className="linkbtn" onClick={() => void preview.refresh()}>{t("common.retry")}</button></div>}
      {!currentPreview && !preview.error && <p className="sub">{t("common.loading")}</p>}
      {currentPreview && <><div className="btnrow"><span className="sub">{t(offline ? "taskContext.cachedPreview" : staffId && role === "worker" ? "taskContext.memberPreview" : "taskContext.genericPreview")}</span><button type="button" className="linkbtn" disabled={offline} onClick={() => void preview.refresh()}>{t("taskContext.refresh")}</button></div>
        <PacketView packet={currentPreview} /></>}
    </div>
    <details><summary>{t("taskContext.supplied")}</summary>
      <p className="sub">{t("taskContext.suppliedHelp")}</p>
      {history.error && <div className="result-warning" role="status">{t("taskContext.historyUnavailable")} <button type="button" className="linkbtn" onClick={() => void history.refresh()}>{t("common.retry")}</button></div>}
      {!currentHistory && !history.error && <p className="sub">{t("common.loading")}</p>}
      {currentHistory?.entries.length === 0 && <p className="sub">{t("taskContext.noSupplied")}</p>}
      {!!currentHistory?.entries.length && <label className="field">{t("taskContext.session")}
        <select className="field" value={entry?.staff_session_id ?? ""} onChange={(event) => setSessionId(event.target.value)}>
          <option value="">{t("taskContext.chooseSession")}</option>
          {currentHistory.entries.map((item) => <option key={item.staff_session_id} value={item.staff_session_id}>{staff.find((member) => member.id === item.staff_id)?.name ?? t("taskContext.unknownMember")} · {new Date(item.created_at).toLocaleString()}</option>)}
        </select>
      </label>}
      {entry && <div className="project-extension">
        <p className={!offline && currentSupplied?.source_current ? "sub" : "result-warning"} role="status">{t(offline || !currentSupplied ? "taskContext.freshnessUnknown" : currentSupplied.source_current ? "taskContext.current" : "taskContext.stale")}</p>
        {supplied.error && <div className="result-warning" role="status">{t("taskContext.packetUnavailable")} <button type="button" className="linkbtn" onClick={() => void supplied.refresh()}>{t("common.retry")}</button></div>}
        {!currentSupplied && !supplied.error && <p className="sub">{t("common.loading")}</p>}
        {currentSupplied && <><p className="sub">{t("taskContext.suppliedAt", { time: new Date(currentSupplied.created_at).toLocaleString() })}</p><PacketView packet={currentSupplied.packet} /></>}
      </div>}
    </details>
  </div>;
}
