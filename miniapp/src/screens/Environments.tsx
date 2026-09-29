// Settings → Environments: terminals and the agent's browser, one page because they are one decision —
// how many may run at once on this machine, and for how long.
//
// The machine's load bar is drawn once, at the top. Each half drew its own before, the same bar twice
// on one page with a different draft in each; now both caps, as they are typed, feed the one bar, so
// raising either shows at once what the machine would carry with both filled.

import { useCallback, useState } from "react";
import type { Settings, WorkloadsLoad } from "../api";
import { t } from "../i18n";
import { WorkloadsBar } from "../loadbar";
import { useQuery } from "../store";
import { BrowserSettingsTab, DEFAULT_BROWSER } from "./BrowserSettings";
import { DEFAULT_CAP, TerminalCap } from "./TerminalCap";

export function EnvironmentsTab({ s, save, toast }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; toast: (text: string) => void }) {
  // Every ten seconds, the daemon's own measuring period: polling faster shows the same numbers.
  const load = useQuery<WorkloadsLoad>("/api/workloads/load", { pollMs: 10000 });
  const [terminals, setTerminals] = useState<number | null>(null);
  const [browsers, setBrowsers] = useState<number | null>(null);
  const onTerminals = useCallback((cap: number | null) => setTerminals(cap), []);
  const onBrowsers = useCallback((cap: number | null) => setBrowsers(cap), []);
  const caps = {
    terminals: terminals ?? s.terminals?.running_cap ?? DEFAULT_CAP,
    browsers: browsers ?? s.browser?.running_cap ?? DEFAULT_BROWSER.running_cap,
  };
  return (
    <>
      <div className="card env-load">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.env.load")}</div>
        {load.data && (load.data.terminals || load.data.browsers) ? (
          <WorkloadsBar load={load.data} caps={caps} />
        ) : load.error ? (
          <div className="sub">{t("settings.cap.noload", { reason: load.error })}</div>
        ) : (
          <div className="sub">{t("common.loading")}</div>
        )}
      </div>
      <div id="terminal-sessions"><TerminalCap s={s} save={save} onDraft={onTerminals} /></div>
      <div id="browser-sessions"><BrowserSettingsTab s={s} save={save} toast={toast} onDraft={onBrowsers} /></div>
    </>
  );
}
