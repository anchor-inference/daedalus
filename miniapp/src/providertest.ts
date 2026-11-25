// "Test connection" for one provider: what the host answered, how fast, or why it did not. Settings
// draws it in a provider's foot and the first-run flow under the endpoint just picked, so the two
// say the same thing in the same words and a change to the wording is made once.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { lang, plural, t } from "./i18n";

export type TestResult = { ok: boolean; latency_ms: number | null; status: number | null; detail: string; models?: number };
export type TestState = { state: "running" } | ({ state: "done" } & TestResult) | { state: "error"; detail: string };
export type TestNote = { text: string; tone: "live" | "attention" | "idle" };

/** Seconds with the reader's decimal mark: "0,18 s" in Russian. */
function seconds(ms: number): string {
  return t("prov.test.seconds", { s: new Intl.NumberFormat(lang(), { maximumFractionDigits: 2 }).format(ms / 1000) });
}

/** The line beside Test connection: what the endpoint answered, how fast, or why it did not. */
export function testNote(test: TestState | null): TestNote | null {
  if (!test) return null;
  if (test.state === "running") return { text: t("prov.test.running"), tone: "idle" };
  if (test.state === "error") return { text: test.detail, tone: "attention" };
  if (!test.ok) return { text: test.detail || t("prov.test.failed"), tone: "attention" };
  const said = test.latency_ms == null ? t("prov.test.ok.plain") : t("prov.test.ok", { time: seconds(test.latency_ms) });
  return { text: test.models ? `${said} · ${plural("prov.test.models", test.models)}` : said, tone: "live" };
}

/**
 * The test of provider `id`, run on demand. A second press while one is out starts a new turn and the
 * first answer is dropped, and picking another provider forgets the last one's answer: a "works" left
 * on screen under a different endpoint would be a lie.
 */
export function useProviderTest(id: string) {
  const [test, setTest] = useState<TestState | null>(null);
  const turn = useRef(0);
  useEffect(() => {
    turn.current += 1;
    setTest(null);
  }, [id]);
  const run = useCallback(async () => {
    const mine = ++turn.current;
    setTest({ state: "running" });
    try {
      const answer = await api.post<TestResult>(`/api/providers/${encodeURIComponent(id)}/test`, {});
      if (mine === turn.current) setTest({ state: "done", ...answer });
    } catch (e) {
      if (mine === turn.current) setTest({ state: "error", detail: (e as Error).message });
    }
  }, [id]);
  return { test, run, note: testNote(test) };
}
