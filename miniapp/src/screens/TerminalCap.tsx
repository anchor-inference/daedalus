// Settings → Environments → terminal sessions: the machine-wide cap on running terminals, with the
// load bar under it.
//
// The cap has no upper bound on purpose. The bar follows the value as it is typed, and a value the
// estimate says the machine cannot carry is saved with a warning rather than refused: the operator
// knows what else will run on that machine, the estimate only knows what ran so far.

import { useEffect, useState } from "react";
import type { Settings, WorkloadsLoad } from "../api";
import { plural, t } from "../i18n";
import { WorkloadsBar } from "../loadbar";
import { useQuery } from "../store";
import { Row } from "../settingsrow";

export const DEFAULT_CAP = 20;

/** The cap a draft stands for: a whole number of at least one, or null while it is not one. */
export function capValue(draft: string): number | null {
  const v = Number(draft.trim());
  return draft.trim() !== "" && Number.isInteger(v) && v >= 1 ? v : null;
}

export function TerminalCap({ s, save }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void> }) {
  const configured = s.terminals?.running_cap ?? DEFAULT_CAP;
  const [draft, setDraft] = useState(String(configured));
  useEffect(() => setDraft(String(configured)), [configured]);
  // Every ten seconds, the daemon's own measuring period: polling faster shows the same numbers.
  // Terminals and browsers share the machine: where there are browsers too, the bar is both, each
  // filled to its own cap and judged together (WorkloadsBar); without them it is the terminals' own.
  const load = useQuery<WorkloadsLoad>("/api/workloads/load", { pollMs: 10000 });
  const value = capValue(draft);

  function commit() {
    if (value === null) {
      setDraft(String(configured));
      return;
    }
    if (value !== configured) void save({ terminals: { ...(s.terminals ?? {}), running_cap: value } });
  }

  return (
    <div className="card terminal-cap">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.cap.title")}</div>
      <div className="sub">{t("settings.cap.sub")}</div>
      <Row title={t("settings.cap.label")} htmlFor="terminal-cap" desc={load.data?.terminals ? plural("settings.cap.running", load.data.terminals.running) : undefined}>
        <span className="settings-num">
          <input
            id="terminal-cap"
            className="field"
            type="number"
            inputMode="numeric"
            min={1}
            step={1}
            value={draft}
            aria-invalid={value === null}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />
          <span className="settings-unit" />
        </span>
      </Row>
      {value === null && <div className="sub push-error">{t("settings.cap.invalid")}</div>}
      {load.data?.terminals ? (
        <WorkloadsBar load={load.data} caps={{ terminals: value ?? configured }} />
      ) : load.error ? (
        <div className="sub">{t("settings.cap.noload", { reason: load.error })}</div>
      ) : (
        <div className="sub">{t("common.loading")}</div>
      )}
    </div>
  );
}
