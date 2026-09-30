// @vitest-environment jsdom
// The operator's recorded steps as the Browser tab shows them: each step in words (a secret one says
// only whose it was), the recording as the socket tells it, and which finished one waits for a draft.
import { describe, expect, it } from "vitest";
import type { BrowserStep, BrowserWorkflow } from "../api";
import { setLang, t } from "../i18n";
import { nextWorkflow } from "./live";
import { doingSteps, stepWords } from "./model";
import { currentRecording, waitingRecording } from "./steps";

function step(n: number, over: Partial<BrowserStep> = {}): BrowserStep {
  return { n, at: 1_790_000_000_000 + n, tab: "t1", url: "https://shop.example/", action: "click", element: { role: "button", name: `Button ${n}` }, ...over };
}

function flow(over: Partial<BrowserWorkflow> = {}): BrowserWorkflow {
  return {
    id: "w1", group_id: "g1", env: "container", project_id: null, session_id: "s1", staff_id: null, state: "stopped", values: "slots", reason: "operator",
    start_url: "https://shop.example/", start_title: "Shop", started_at: "2026-09-30T10:00:00.000Z", stopped_at: "2026-09-30T10:05:00.000Z", steps: [step(1)], note_id: "", ...over,
  };
}

function said(s: BrowserStep): string {
  const words = stepWords(s);
  return t(words.key, words.vars);
}

describe("a recorded step in words", () => {
  it("names the element as the page does, and a blank by its name", () => {
    setLang("en");
    expect(said(step(1))).toBe("Clicked “Button 1”");
    expect(said(step(2, { action: "type", element: { role: "searchbox", name: "Search" }, slot: "search", submit: true }))).toBe("Typed into “Search” and sent it (a blank: search)");
    expect(said(step(3, { action: "type", element: { role: "searchbox", name: "Search" }, slot: "search", value: "blue shoes" }))).toBe("Typed “blue shoes” into “Search”");
    expect(said(step(4, { action: "select", element: { role: "combobox", name: "Size" }, option: "42" }))).toBe("Chose “42” in “Size”");
    expect(said(step(5, { action: "press", keys: "Escape", count: 2, element: undefined }))).toBe("Pressed Escape ×2");
    expect(said(step(6, { action: "navigate", to: "https://shop.example/reports", element: undefined }))).toBe("Opened shop.example");
    expect(said(step(7, { action: "navigate", go: "back", element: undefined }))).toBe("Went back");
    expect(said(step(8, { action: "click", element: undefined, point: [10, 20] }))).toBe("Clicked a place the page does not name");
  });

  it("says a secret step is the operator's and that nothing of it was kept", () => {
    setLang("en");
    expect(said(step(1, { action: "handoff", reason: "login" }))).toContain("not recorded");
    expect(said(step(2, { action: "handoff", reason: "payment" }))).toContain("card details");
    setLang("ru");
    expect(said(step(3, { action: "handoff", reason: "two_factor" }))).toContain("одноразовый код");
    setLang("en");
  });

  it("counts only the steps that do something", () => {
    expect(doingSteps([step(1), step(2, { action: "arrive", to: "https://x/" }), step(3, { action: "scroll", direction: "down" }), step(4, { action: "expect", text: "Done" })])).toBe(1);
  });
});

describe("the recording on the socket", () => {
  it("adds each step, puts a replacement in its place, and keeps the steps when it stops", () => {
    let live = nextWorkflow(null, { type: "workflow", state: "started", id: "w1", recording: true, steps: 0 });
    expect(live).toEqual({ id: "w1", recording: true, steps: [], count: 0, reason: "" });
    live = nextWorkflow(live, { type: "workflow", state: "step", id: "w1", step: step(1), steps: 1 });
    live = nextWorkflow(live, { type: "workflow", state: "step", id: "w1", step: step(1, { action: "type", slot: "q" }), steps: 1, replaces: 1 });
    live = nextWorkflow(live, { type: "workflow", state: "step", id: "w1", step: step(2), steps: 2 });
    expect(live.steps.map((s) => s.action)).toEqual(["type", "click"]);
    live = nextWorkflow(live, { type: "workflow", state: "stopped", id: "w1", recording: false, steps: 2, reason: "control" });
    expect(live).toMatchObject({ recording: false, count: 2, reason: "control" });
    expect(live.steps).toHaveLength(2);
  });

  it("joins the host's earlier steps to the socket's for a view that attached midway", () => {
    const listed = flow({ state: "recording", steps: [step(1), step(2)] });
    const live = { id: "w1", recording: true, steps: [step(3)], count: 3, reason: "" };
    expect(currentRecording(live, listed)?.steps.map((s) => s.n)).toEqual([1, 2, 3]);
    expect(currentRecording(null, listed)?.recording).toBe(true);
    // Stopped on the socket: nothing records any more, whatever the listing read before.
    expect(currentRecording({ ...live, recording: false }, flow({ state: "stopped" }))).toBeNull();
    expect(currentRecording(null, null)).toBeNull();
  });

  it("offers the newest finished recording that is neither drafted nor empty", () => {
    const empty = flow({ id: "w3", steps: [step(1, { action: "scroll", direction: "down", element: undefined })] });
    const drafted = flow({ id: "w2", note_id: "n1" });
    const waiting = flow({ id: "w1" });
    expect(waitingRecording([empty, drafted, waiting])?.id).toBe("w1");
    expect(waitingRecording([drafted])).toBeNull();
  });
});
