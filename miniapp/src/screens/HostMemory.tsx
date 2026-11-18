import { useCallback, useEffect, useRef, useState } from "react";
import { api, HostMemoryCap, Settings } from "../api";
import { bytes } from "../format";
import { t } from "../i18n";
import { NumInput, Row } from "../settingsrow";
import { Segmented } from "../ui/index";

/** The shipped value of `terminals.host_memory_cap`, as `daedalus/config.py` has it. */
export const HOST_MEMORY_DEFAULT = "60%";

type Kind = "percent" | "gb" | "none";

/** "60%", "24G" or "none", taken apart into its kind and its number. */
export function parseCap(value: string): { kind: Kind; amount: number } {
  if (value === "none") return { kind: "none", amount: 0 };
  if (value.endsWith("%")) return { kind: "percent", amount: Number(value.slice(0, -1)) };
  return { kind: "gb", amount: Number(value.slice(0, -1)) };
}

/** The setting from its kind and number; a kind switched to starts from a sensible number. */
export function formatCap(kind: Kind, amount: number): string {
  if (kind === "none") return "none";
  return kind === "percent" ? `${amount}%` : `${amount}G`;
}

/** The memory every program the agent runs on the host may use together, in Settings → Limits,
 *  with what the host's terminal daemon has in force beside it. The cap is the kernel's, enforced on
 *  the daemon's own control group; the page only reads it back, so what it shows is what holds. */
export function HostMemoryRow({ s, save }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void> }) {
  const value = s.terminals?.host_memory_cap ?? HOST_MEMORY_DEFAULT;
  const { kind, amount } = parseCap(value);
  const [view, setView] = useState<HostMemoryCap | null>(null);
  const [failed, setFailed] = useState(false);
  const sequence = useRef(0);
  const load = useCallback(() => {
    const mine = ++sequence.current;
    api.get<HostMemoryCap>("/api/terminals/envs/host/memory")
      .then((next) => { if (mine === sequence.current) { setView(next); setFailed(false); } })
      .catch(() => { if (mine === sequence.current) setFailed(true); });
  }, []);
  // Read again after a change reaches the daemon, which is within a couple of seconds of the save.
  useEffect(() => {
    load();
    const timer = window.setTimeout(load, 3000);
    return () => window.clearTimeout(timer);
  }, [load, value]);

  function choose(next: Kind) {
    const total = view?.status?.machine_bytes ?? 0;
    const start = next === "percent" ? 60 : next === "gb" ? Math.max(1, Math.round(((total || 16 * 2 ** 30) * 0.6) / 2 ** 30)) : 0;
    void save({ terminals: { ...s.terminals!, host_memory_cap: formatCap(next, start) } });
  }

  const changed = value !== HOST_MEMORY_DEFAULT;
  return (
    <Row title={t("settings.limits.hostmem")} desc={<>{t("settings.limits.hostmem.sub")} <HostMemoryState view={view} failed={failed} /></>}
      htmlFor="limits-hostmem" stack data-setting="terminals.host_memory_cap"
      changed={changed} defaultText={changed ? HOST_MEMORY_DEFAULT : undefined} onReset={() => void save({ terminals: { ...s.terminals!, host_memory_cap: HOST_MEMORY_DEFAULT } })}>
      <Segmented<Kind> label={t("settings.limits.hostmem")} value={kind} onChange={choose}
        options={[{ id: "percent", label: t("settings.limits.hostmem.percent") }, { id: "gb", label: t("settings.limits.hostmem.gb") }, { id: "none", label: t("settings.limits.hostmem.none") }]} />
      {kind !== "none" && (
        <NumInput id="limits-hostmem" label={t("settings.limits.hostmem")} value={amount} min={kind === "percent" ? 1 : 0.25} max={kind === "percent" ? 100 : undefined} step={kind === "percent" ? 5 : 1}
          unit={kind === "percent" ? "%" : t("disk.limit.unit")} onSave={(v) => void save({ terminals: { ...s.terminals!, host_memory_cap: formatCap(kind, v) } })} />
      )}
    </Row>
  );
}

/** One line of what is in force: the machine, what the programs use, the cap — or why there is none. */
function HostMemoryState({ view, failed }: { view: HostMemoryCap | null; failed: boolean }) {
  if (failed) return <span className="settings-hostmem-state" data-state="failed">{t("settings.limits.hostmem.failed")}</span>;
  if (!view) return null;
  const status = view.status;
  if (!status) return <span className="settings-hostmem-state" data-state={view.reason}>{t(view.reason === "outdated" ? "settings.limits.hostmem.outdated" : "settings.limits.hostmem.unreachable")}</span>;
  if (!status.supported) return <span className="settings-hostmem-state" data-state="unsupported">{t("settings.limits.hostmem.unsupported", { why: status.reason ?? "" })}</span>;
  const cap = status.max_bytes === null ? t("settings.limits.hostmem.nocap") : bytes(status.max_bytes);
  return (
    <span className="settings-hostmem-state" data-state="ok">
      {t("settings.limits.hostmem.state", { machine: bytes(status.machine_bytes), used: bytes(Math.max(0, status.current_bytes)), cap })}
      {status.oom_kills > 0 && <> {t("settings.limits.hostmem.kills", { n: status.oom_kills })}</>}
      {status.mode === "own" && <> {t("settings.limits.hostmem.own")}</>}
      {status.mode === "job_object" && <> {t("settings.limits.hostmem.windows")}</>}
      {view.error && <> {t("settings.limits.hostmem.error", { why: view.error })}</>}
    </span>
  );
}
