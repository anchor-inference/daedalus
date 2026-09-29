// @vitest-environment jsdom
// The tool groups as the app words and draws them: the share of sessions, the chip a session's group
// wears, the step a search that loaded a group becomes, and the two panels that change the modes.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionToolGroup, ToolGroupCatalogue } from "./api";
import { setLang } from "./i18n";
import { groupDetail, searchedGroup, stateChip, usageLine, usageShare } from "./toolgroups";
import { SessionToolGroups, ToolGroupsSettings } from "./toolgroupsview";
import { EMPTY_LIVE, applyLive, liveAfter } from "./turns";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the words", () => {
  beforeEach(() => setLang("en"));

  it("never rounds a group somebody used down to nothing", () => {
    expect(usageShare(1, 151).label).toBe("<1");
    expect(usageShare(2, 151).label).toBe("1");
    expect(usageShare(0, 151).label).toBe("0");
    expect(usageShare(3, 0).label).toBe("0");
    expect(usageLine(50, 151, 30)).toBe("used in 33% of sessions · 30 days");
  });

  it("says what a session's group is and whether it is still coming", () => {
    expect(stateChip({ state: "loaded", pending: false }).word).toBe("Loaded");
    expect(stateChip({ state: "loaded", pending: true }).word).toBe("Loads next message");
    // Loaded is the whole group: one tool of twelve a search brought in says so.
    expect(stateChip({ state: "loaded", pending: false, loaded: 12, tools: 12 }).word).toBe("Loaded");
    expect(stateChip({ state: "loaded", pending: false, loaded: 1, tools: 12 }).word).toBe("1 of 12 loaded");
    expect(stateChip({ state: "deferred", pending: false }).word).toBe("On demand");
    expect(stateChip({ state: "advertised", pending: false }).word).toBe("Always");
    expect(stateChip({ state: "off", pending: false }).word).toBe("Off");
    // Before any run has placed a group that goes by the window, the chip says the mode, not a guess.
    expect(stateChip({ state: "undecided", pending: false, load: "auto" }).word).toBe("When it fits");
  });

  it("reads the group a search asked for in either of its forms", () => {
    expect(searchedGroup({ group: "browser" })).toBe("browser");
    expect(searchedGroup({ select: "group:scheduling" })).toBe("scheduling");
    expect(searchedGroup({ query: "select:group:loop" })).toBe("loop");
    expect(searchedGroup({ query: "open a page" })).toBe("");
  });

  it("names a group in the reader's language, and one it does not know by its id", () => {
    expect(groupDetail("browser", 12)).toBe("Browser (12 tools)");
    setLang("ru");
    expect(groupDetail("browser", 12)).toBe("Браузер (12 инструментов)");
    expect(groupDetail("MCP server tracker", 0)).toBe("MCP server tracker");
  });
});

describe("the chat", () => {
  const run = { run_id: "r1" };

  it("puts a search's group on the search step and draws no second line for it", () => {
    let s = liveAfter(EMPTY_LIVE, "message_start", run);
    s = liveAfter(s, "tool_use_start", { ...run, tool_call_id: "c1", tool_name: "ToolSearch" });
    s = liveAfter(s, "tool_use_stop", { ...run, tool_call_id: "c1", final_input: { group: "browser" } });
    s = liveAfter(s, "tool_result", { ...run, tool_call_id: "c1", content: "loaded" });
    s = liveAfter(s, "tool_group_loaded", { ...run, group: "browser", via: "group", tools: Array.from({ length: 12 }, (_, i) => `B${i}`) });
    const turn = applyLive(null, s, Date.now());
    expect(turn.activity.filter((a) => a.kind === "group")).toHaveLength(0);
    const step = turn.activity.find((a) => a.kind === "tool");
    expect(step && step.kind === "tool" && step.groups).toEqual([{ group: "browser", tools: 12, via: "group" }]);
  });

  it("draws a line for a group loaded by calling one of its tools by name, and none for the seed", () => {
    let s = liveAfter(EMPTY_LIVE, "message_start", run);
    s = liveAfter(s, "tool_group_loaded", { ...run, group: "scheduling", via: "seed", tools: ["ScheduleCreate"] });
    s = liveAfter(s, "tool_group_loaded", { ...run, group: "browser", via: "direct_call", tools: ["BrowserOpen"] });
    const turn = applyLive(null, s, Date.now());
    expect(turn.activity).toEqual([{ kind: "group", group: "browser", tools: 1 }]);
  });
});

const CATALOGUE: ToolGroupCatalogue = {
  days: 30,
  sessions: 151,
  runs: 894,
  groups: [
    { name: "browser", description: "Drive a real browser", tools: ["BrowserOpen", "BrowserAct"], tokens: 1700, default: "lazy", load: "lazy", usage: { sessions: 2, runs: 3, calls: 9 } },
    { name: "board", description: "The task board", tools: ["BoardAdd"], tokens: 800, default: "auto", load: "eager", usage: { sessions: 20, runs: 40, calls: 300 } },
  ],
};

function respond(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("the panels", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  it("shows each group's cost, use and mode in Settings, marks the changed default, and saves a pick", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method === "PUT") return respond({ ...CATALOGUE, groups: CATALOGUE.groups.map((g) => (g.name === "browser" ? { ...g, load: "eager" } : g)), settings: { revision: "r2" } });
      return respond(CATALOGUE);
    });
    const onSettings = vi.fn();
    await act(async () => root.render(<ToolGroupsSettings toast={() => {}} revision="r1" onSettings={onSettings} />));
    const browser = host.querySelector('[data-group="browser"]')!;
    expect(browser.textContent).toContain("Browser");
    expect(browser.textContent).toContain("2 tools");
    expect(browser.textContent).toContain("used in 1% of sessions · 30 days");
    expect(browser.querySelector<HTMLElement>(".dropdown-btn")!.textContent).toBe("On demand");
    // Only a group moved off its default carries a Reset, and it names the default it goes back to.
    expect(browser.querySelector(".tgroup-reset")).toBeNull();
    expect(host.querySelector('[data-group="board"] .tgroup-reset')!.getAttribute("title")).toContain("When it fits");
    await act(async () => browser.querySelector<HTMLElement>(".dropdown-btn")!.click());
    const always = [...browser.querySelectorAll<HTMLElement>("[role=option]")].find((o) => o.dataset.value === "eager")!;
    await act(async () => always.click());
    const put = fetcher.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(String(put[0])).toContain("/api/tool-groups/browser");
    // Saved against the revision on screen, and the screen is handed the revision the save made, or
    // its next save of anything would be refused as changed in another window.
    expect(JSON.parse(String(put[1]!.body))).toEqual({ load: "eager", base_revision: "r1" });
    expect(onSettings).toHaveBeenCalledWith({ revision: "r2" });
    expect(host.querySelector('[data-group="browser"] .dropdown-btn')!.textContent).toBe("Always");
  });

  it("offers Load now only for a group on demand, and shows the answer's state", async () => {
    const groups: SessionToolGroup[] = [
      { name: "browser", description: "", tools: 12, load: "lazy", source: "default", state: "deferred", pending: false },
      { name: "board", description: "", tools: 4, load: "auto", source: "session", state: "advertised", pending: false },
      { name: "self_development", description: "", tools: 0, load: "lazy", source: "default", state: "off", pending: false },
    ];
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method === "POST") return respond({ groups: groups.map((g) => (g.name === "browser" ? { ...g, state: "loaded", pending: true } : g)) });
      return respond({ groups });
    });
    await act(async () => root.render(<SessionToolGroups sessionId="s1" toast={() => {}} />));
    const buttons = [...host.querySelectorAll(".stgroup .btn")];
    expect(buttons.map((b) => b.closest(".stgroup")!.getAttribute("data-group"))).toEqual(["browser"]);
    expect(host.querySelector('[data-group="board"]')!.textContent).toContain("this session");
    await act(async () => (buttons[0] as HTMLButtonElement).click());
    expect(String(fetcher.mock.calls.at(-1)![0])).toContain("/api/sessions/s1/tool-groups/browser/load");
    expect(host.querySelector('[data-group="browser"] .chip')!.textContent).toBe("Loads next message");
    expect(host.querySelectorAll(".stgroup .btn")).toHaveLength(0);
  });
});
