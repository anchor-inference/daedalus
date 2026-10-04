import { useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { useQuery } from "../store";
import { errorText } from "../ui";

type Launch = {
  id: string; task_id: string; state: string; claim_generation: number;
  created_at: string; claimed_at: string | null; completed_at: string | null;
  error: string | null; attempt_id: string | null; attempt_state: string | null;
  provider_session_recorded: boolean;
};
type Inspection = { items: Launch[]; next_after: string | null };

/** A session-local receipt disappears with browser storage, while its launch claim still blocks work. */
export function UncertainLaunches({ projectId }: { projectId: string }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/uncertain-launches`;
  const first = useQuery<Inspection>(`${base}?limit=25`, { staleMs: 0 });
  const [extra, setExtra] = useState<Launch[]>([]);
  const [next, setNext] = useState<string | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [warning, setWarning] = useState("");
  const firstItems = first.data?.items ?? [];
  const firstIds = new Set(firstItems.map((item) => item.id));
  const items = [...firstItems, ...extra.filter((item) => !firstIds.has(item.id))];
  const cursor = next === undefined ? first.data?.next_after : next;

  async function reload() {
    setExtra([]);
    setNext(undefined);
    await first.refresh();
  }

  async function more() {
    if (!cursor || busy) return;
    setBusy(true);
    setWarning("");
    try {
      const page = await api.get<Inspection>(`${base}?limit=25&after=${encodeURIComponent(cursor)}`);
      setExtra((rows) => [...rows, ...page.items]);
      setNext(page.next_after);
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  if (!items.length && !warning && !first.error) return null;
  return <details className="pboard-unknown-stops pboard-uncertain-launches" open={!!warning}>
    <summary>{first.error && !items.length ? t("pboard.launchClaims.unavailable") : t("pboard.launchClaims.title", { count: items.length })}</summary>
    <p className="sub">{t("pboard.launchClaims.help")}</p>
    {first.error && <p className="result-warning" role="alert">{first.error}</p>}
    {warning && <p className="result-warning" role="alert">{warning}</p>}
    {items.map((item) => <div className="pboard-unknown-row" key={item.id}>
      <div><b>{item.task_id}</b> · <code>{item.id}</code> · {t(`pboard.launchClaims.${item.state}`)}</div>
      <details><summary>{t("pboard.unknown.observation")}</summary>
        <dl>
          {(["claim_generation", "created_at", "claimed_at", "completed_at", "error", "attempt_id",
            "attempt_state", "provider_session_recorded"] as const).map((key) =>
            <div key={key}><dt>{t(`pboard.launchClaims.${key}`)}</dt><dd><code>{typeof item[key] === "boolean" ? t(item[key] ? "pboard.unknown.yes" : "pboard.unknown.no") : String(item[key] ?? "—")}</code></dd></div>)}
        </dl>
      </details>
    </div>)}
    {cursor && <button type="button" className="linkbtn" disabled={busy} onClick={() => void more()}>{t("pboard.unknown.more")}</button>}
    <button type="button" className="linkbtn" disabled={busy} onClick={() => void reload()}>{t("pboard.launchClaims.reload")}</button>
  </details>;
}
