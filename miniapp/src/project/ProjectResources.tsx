import { useEffect, useState } from "react";
import { t } from "../i18n";
import { useQuery } from "../store";

type Limits = { memory_bytes: number; cpu_millis: number; process_count: number; disk_bytes: number };
type Profile = {
  project_id: string;
  configured: boolean;
  profile_revision: number | null;
  state: "enabled" | "disabled";
  limits: Limits | null;
};
type Capability = { available: boolean; reason?: string; kind?: string; sandbox?: string };
type Capabilities = {
  container: Capability;
  host: Capability;
  native: Capability;
  disk_quota_supported: boolean;
};
type Write = (method: "PUT", path: string, fields: Record<string, unknown>, label: string,
              onSuccess?: () => void) => Promise<boolean>;

export const resourceProfileKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/resource-profile`;

function positiveInteger(value: string, minimum: number, maximum: number): boolean {
  if (!/^[1-9][0-9]*$/.test(value)) return false;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= minimum && number <= maximum;
}

export function ProjectResources({ projectId, write, canWrite }: {
  projectId: string;
  write: Write;
  canWrite: boolean;
}) {
  const { data, error, refresh } = useQuery<Profile>(resourceProfileKey(projectId), { staleMs: 15000 });
  const capability = useQuery<Capabilities>("/api/admission/resources", { staleMs: 30000 });
  const [loaded, setLoaded] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [memory, setMemory] = useState("");
  const [cpu, setCpu] = useState("");
  const [processes, setProcesses] = useState("");
  const [saving, setSaving] = useState(false);
  const revision = `${data?.project_id ?? projectId}:${data?.profile_revision ?? 0}`;

  useEffect(() => {
    if (!data || loaded === revision) return;
    setEnabled(data.state === "enabled");
    setMemory(data.limits ? String(data.limits.memory_bytes / 1048576) : "");
    setCpu(data.limits ? String(data.limits.cpu_millis) : "");
    setProcesses(data.limits ? String(data.limits.process_count) : "");
    setLoaded(revision);
  }, [data, loaded, revision]);

  const valid = !enabled || (positiveInteger(memory, 16, 1 << 30)
    && positiveInteger(cpu, 10, 100000) && positiveInteger(processes, 1, 65536));
  const next: Limits = {
    memory_bytes: positiveInteger(memory, 1, 1 << 30) ? Number(memory) * 1048576 : 0,
    cpu_millis: positiveInteger(cpu, 1, 100000) ? Number(cpu) : 0,
    process_count: positiveInteger(processes, 1, 65536) ? Number(processes) : 0,
    disk_bytes: 0,
  };
  const changed = data && (data.state !== (enabled ? "enabled" : "disabled")
    || (data.configured && JSON.stringify(data.limits) !== JSON.stringify(next)));

  async function save() {
    if (!data || loaded !== revision || !valid || !changed || saving || !canWrite) return;
    setSaving(true);
    try {
      await write("PUT", resourceProfileKey(projectId), { state: enabled ? "enabled" : "disabled", ...next },
        t("resource.saved"), refresh);
    } finally { setSaving(false); }
  }

  const compact = data?.configured && data.state === "enabled" && data.limits
    ? t("resource.summary", { memory: String(data.limits.memory_bytes / 1048576),
        cpu: String(data.limits.cpu_millis), processes: String(data.limits.process_count) })
    : data?.configured ? t("resource.off") : null;

  return <details className="project-resources">
    <summary>{t("resource.title")} {compact && <span className="sub">· {compact}</span>}</summary>
    {error && <div className="sub" role="status">{t("resource.readError")} <button type="button" className="linkbtn" onClick={refresh}>{t("common.retry")}</button></div>}
    {!data && !error && <div className="sub">{t("common.loading")}</div>}
    {data && <>
      <p className="sub">{t("resource.scope")}</p>
      <label className="toggle-row"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
        <span>{t("resource.enable")}</span></label>
      {enabled && <>
        <div className="project-resource-fields">
          <label htmlFor={`resource-memory-${projectId}`}>{t("resource.memory")}</label>
          <input id={`resource-memory-${projectId}`} className="field" inputMode="numeric" value={memory} onChange={(event) => setMemory(event.target.value)} />
          <label htmlFor={`resource-cpu-${projectId}`}>{t("resource.cpu")}</label>
          <input id={`resource-cpu-${projectId}`} className="field" inputMode="numeric" value={cpu} onChange={(event) => setCpu(event.target.value)} />
          <label htmlFor={`resource-processes-${projectId}`}>{t("resource.processes")}</label>
          <input id={`resource-processes-${projectId}`} className="field" inputMode="numeric" value={processes} onChange={(event) => setProcesses(event.target.value)} />
        </div>
        <div className="sub">{t("resource.diskUnsupported")}</div>
        <div className="sub">{t("resource.nativeUnsupported")}</div>
        {capability.error && <div className="sub" role="status">{t("resource.capabilityUnknown")}</div>}
        {capability.data && (["container", "host"] as const).map((env) => {
          const status = capability.data?.[env];
          return <div className="sub" key={env}>{t(`folder.env.${env}.long`)}: {status?.available
            ? t("resource.capabilityReady") : t("resource.capabilityUnavailable", { reason: status?.reason ?? t("resource.unknown") })}</div>;
        })}
      </>}
      <button type="button" className="btn small" onClick={() => void save()}
        disabled={loaded !== revision || !valid || !changed || saving || !canWrite}>{t("resource.save")}</button>
    </>}
  </details>;
}
