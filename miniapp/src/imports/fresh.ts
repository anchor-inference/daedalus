// The fresh sessions the start screen offers to continue.
//
// The host lists the programs and, per program, the folders that have sessions with the newest time
// in each; it does not list sessions across folders. So the freshest folders of the programs that
// have any are scanned — three folders of at most three programs — and their sessions pooled. The
// result is kept for five minutes for the page's life: the start screen is visited often, every scan
// reads the machine, and a card that is a few minutes stale costs nothing. A host that does not
// answer simply leaves the card out; the explorer is where its failure is explained.

import { useEffect, useState } from "react";
import { api } from "../api";
import { FRESH_MS, harnessOrder, scanUrl, type ForeignSession, type HarnessList, type ScanResult } from "./model";

export type Fresh = { sessions: ForeignSession[]; home?: string };

const KEEP_MS = 5 * 60 * 1000;
const PROGRAMS = 3;
const FOLDERS = 3;

let kept: { at: number; value: Fresh } | null = null;
let asking: Promise<Fresh> | null = null;

async function discover(now = Date.now()): Promise<Fresh> {
  const list = await api.get<HarnessList>("/api/imports/harnesses");
  if (list.recent) return { sessions: list.recent, home: list.home };
  const programs = harnessOrder(list.harnesses).filter((h) => h.found && h.sessions > 0).slice(0, PROGRAMS);
  const found: ForeignSession[] = [];
  let home: string | undefined;
  await Promise.all(programs.map(async (program) => {
    const places = await api.get<ScanResult>(scanUrl(program.id, "", "", false));
    home = home || places.home || undefined;
    const fresh = [...places.folders]
      .filter((f) => f.sessions > 0 && f.latest && now - Date.parse(f.latest) < FRESH_MS)
      .sort((a, b) => Date.parse(b.latest ?? "") - Date.parse(a.latest ?? ""))
      .slice(0, FOLDERS);
    for (const folder of fresh) {
      const scan = await api.get<ScanResult>(scanUrl(program.id, folder.path, "", false));
      home = home || scan.home || undefined;
      found.push(...scan.here);
    }
  }));
  return { sessions: found, home };
}

/** Forget what was found: an import just made one of them a chat. */
export function forgetFresh(): void {
  kept = null;
}

export function useFreshSessions(enabled: boolean): Fresh {
  const [value, setValue] = useState<Fresh>(() => kept?.value ?? { sessions: [] });
  useEffect(() => {
    if (!enabled) return;
    if (kept && Date.now() - kept.at < KEEP_MS) { setValue(kept.value); return; }
    let gone = false;
    asking = asking ?? discover().finally(() => { asking = null; });
    asking.then((found) => {
      kept = { at: Date.now(), value: found };
      if (!gone) setValue(found);
    }).catch(() => { /* the machine does not answer: no card */ });
    return () => { gone = true; };
  }, [enabled]);
  return enabled ? value : { sessions: [] };
}
