import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { shortDateTime } from "../format";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";

type Observation = {
  id: string;
  project_id: string | null;
  session_id: string;
  model: string;
  status: number;
  failure_class: "auth" | "billing" | "quota" | "rate" | "capacity" | "limited_unknown" | "capacity_unknown" | "other";
  reset_at: string | null;
  retry_after_at: string | null;
  hold_id: string | null;
  hold_state: string | null;
  hold_available: boolean;
};

type Limits = { provider_id: string; observations: Observation[] };
type Intent = { key: string; path: string; body: { expected_entity_revision: number; client_operation_id: string; observation_id?: string } };

export function ProviderLimit({ providerId }: { providerId: string }) {
  const key = `/api/providers/${encodeURIComponent(providerId)}/limits`;
  const { data, error, refresh } = useQuery<Limits>(key, { pollMs: 30000, staleMs: 10000 });
  const offline = useOffline();
  const [busy, setBusy] = useState(false);
  const [writeError, setWriteError] = useState("");
  const intent = useRef<Intent | null>(null);
  const latest = data?.observations[0];

  async function decide(kind: "hold" | "resume", observation: Observation) {
    if (!observation.project_id || busy || offline) return;
    const actionKey = `${kind}:${observation.id}:${observation.hold_id ?? ""}`;
    setBusy(true);
    setWriteError("");
    try {
      if (!intent.current || intent.current.key !== actionKey) {
        const project = await api.get<{ entity_revision: number }>(`/api/projects/${encodeURIComponent(observation.project_id)}/budget`);
        if (!Number.isInteger(project.entity_revision)) throw new Error(t("provider.limit.revisionMissing"));
        intent.current = {
          key: actionKey,
          path: kind === "hold" ? `/api/providers/${encodeURIComponent(providerId)}/holds`
            : `/api/providers/${encodeURIComponent(providerId)}/holds/${encodeURIComponent(observation.hold_id!)}/resume`,
          body: { expected_entity_revision: project.entity_revision, client_operation_id: crypto.randomUUID(),
                  ...(kind === "hold" ? { observation_id: observation.id } : {}) },
        };
      }
      await api.post(intent.current.path, intent.current.body);
      intent.current = null;
      refresh();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        intent.current = null;
        refresh();
        setWriteError(t("provider.limit.stale"));
      } else {
        setWriteError(t("provider.limit.unconfirmed"));
      }
    } finally { setBusy(false); }
  }

  if (!data?.observations.length && !error) return null;
  return <div className="provider-limit sub" role="status">
    {error && <span>{t("provider.limit.readFailed")} <button type="button" className="linkbtn" onClick={refresh}>{t("common.retry")}</button></span>}
    {latest && <>
      <span>{t(`provider.limit.${latest.failure_class}`)} · {latest.model}</span>
      {latest.reset_at && <span> · {t("provider.limit.reset", { when: shortDateTime(latest.reset_at) })}</span>}
      {!latest.reset_at && latest.retry_after_at && <span> · {t("provider.limit.retryAfter", { when: shortDateTime(latest.retry_after_at) })}</span>}
      {!latest.reset_at && !latest.retry_after_at && <span> · {t("provider.limit.timeUnknown")}</span>}
      {latest.hold_state && <span> · {t(`provider.limit.state.${latest.hold_state}`)}</span>}
      {latest.hold_available && latest.project_id && !latest.hold_id && <button type="button" className="linkbtn" disabled={busy || offline} onClick={() => void decide("hold", latest)}>{t("provider.limit.hold")}</button>}
      {latest.hold_state === "held" && latest.hold_id && latest.project_id && latest.reset_at && new Date(latest.reset_at).getTime() <= Date.now() && <button type="button" className="linkbtn" disabled={busy || offline} onClick={() => void decide("resume", latest)}>{t("provider.limit.resume")}</button>}
    </>}
    {writeError && <span> · {writeError} {intent.current && latest?.project_id && <button type="button" className="linkbtn" disabled={busy || offline} onClick={() => void decide(intent.current!.key.startsWith("hold:") ? "hold" : "resume", latest)}>{t("provider.limit.retryCommand")}</button>}</span>}
  </div>;
}
