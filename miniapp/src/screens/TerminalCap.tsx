// Settings → Environments → terminal sessions: the machine-wide cap on running terminals. The load
// bar it moves is drawn once, at the top of the page (Environments.tsx).
//
// The cap has no upper bound on purpose. The bar follows the value as it is typed, and a value the
// estimate says the machine cannot carry is saved with a warning rather than refused: the operator
// knows what else will run on that machine, the estimate only knows what ran so far.

import { useEffect, useState } from "react";
import type { Settings, WorkloadsLoad } from "../api";
import { plural, t } from "../i18n";
import { useQuery } from "../store";
import { Row } from "../settingsrow";

export const DEFAULT_CAP = 20;

/** The cap a draft stands for: a whole number of at least one, or null while it is not one. */
export function capValue(draft: string): number | null {
  const v = Number(draft.trim());
  return draft.trim() !== "" && Number.isInteger(v) && v >= 1 ? v : null;
}

/** `onDraft` hands the page the cap as it is typed (null while it is not a number), so the one load
 *  bar at the top of Environments follows the field rather than the saved value. */
export function TerminalCap({ s, save, onDraft }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; onDraft?: (cap: number | null) => void }) {
  const configured = s.terminals?.running_cap ?? DEFAULT_CAP;
  const [draft, setDraft] = useState(String(configured));
  useEffect(() => setDraft(String(configured)), [configured]);
  // Read for the running count only; the same query, shared, draws the page's bar.
  const load = useQuery<WorkloadsLoad>("/api/workloads/load", { pollMs: 10000 });
  const value = capValue(draft);
  useEffect(() => onDraft?.(value), [onDraft, value]);

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
    </div>
  );
}
