import { describe, expect, it } from "vitest";
import { activeId, clampFraction, close, EMPTY, focus, fromAddress, hasPanes, maximise, normalise, openingView, place, replace, setFractions, setLayout, shownSlots, swap, toAddress, type Workspace } from "./workspacestate";

const four: Workspace = { ...EMPTY, layout: 4, panes: ["a", "b", "c", "d"] };
const address = (detail: string | null, query = "") => ({ detail, query: new URLSearchParams(query) });

describe("the splitters", () => {
  it("never leave a pane less than a fifth, and fall back to half on nonsense", () => {
    expect(clampFraction(0.05)).toBe(0.2);
    expect(clampFraction(0.95)).toBe(0.8);
    expect(clampFraction(0.37)).toBe(0.37);
    expect(clampFraction(Number.NaN)).toBe(0.5);
    expect(setFractions(four, { col: 0 }).col).toBe(0.2);
    expect(setFractions(four, { row: 1 }).row).toBe(0.8);
  });

  it("keep the same object when nothing moved, so a drag that stays put renders nothing", () => {
    expect(setFractions(four, { col: 0.5 })).toBe(four);
  });
});

describe("the layout", () => {
  it("keeps every terminal through 4 → 1 → 4", () => {
    const one = setLayout(four, 1);
    expect(shownSlots(one)).toEqual([0]);
    expect(one.panes).toEqual(["a", "b", "c", "d"]);
    expect(setLayout(one, 4)).toEqual({ ...four, active: 0 });
  });

  it("moves the keyboard to a shown place when its place is hidden", () => {
    expect(setLayout({ ...four, active: 3 }, 2).active).toBe(0);
    expect(setLayout({ ...four, active: 1 }, 2).active).toBe(1);
  });

  it("counts as set up only when a shown place holds a terminal", () => {
    expect(hasPanes(EMPTY)).toBe(false);
    expect(hasPanes({ ...EMPTY, panes: [null, "x", null, null] })).toBe(false);
    expect(hasPanes({ ...EMPTY, layout: 2, panes: [null, "x", null, null] })).toBe(true);
  });
});

describe("a swap", () => {
  it("trades two places, and the keyboard follows the pane that moved", () => {
    const swapped = swap({ ...four, active: 0 }, 0, 3);
    expect(swapped.panes).toEqual(["d", "b", "c", "a"]);
    expect(swapped.active).toBe(3);
    expect(swap({ ...four, active: 3 }, 0, 3).active).toBe(0);
    expect(swap({ ...four, active: 1 }, 0, 3).active).toBe(1);
  });

  it("onto itself or outside the grid changes nothing", () => {
    expect(swap(four, 2, 2)).toBe(four);
    expect(swap(four, 0, -1)).toBe(four);
    expect(swap(four, 0, 4)).toBe(four);
  });
});

describe("showing a terminal", () => {
  it("focuses the place that already holds it instead of showing it twice", () => {
    const next = place({ ...four, active: 0 }, "c");
    expect(next.panes).toEqual(["a", "b", "c", "d"]);
    expect(next.active).toBe(2);
  });

  it("fills the active place when it is empty, then the first empty shown place, then replaces the active one", () => {
    expect(place({ ...EMPTY, layout: 2, panes: [null, null, null, null], active: 1 }, "x").panes).toEqual([null, "x", null, null]);
    expect(place({ ...EMPTY, layout: 2, panes: ["a", null, null, null], active: 0 }, "x")).toMatchObject({ panes: ["a", "x", null, null], active: 1 });
    expect(place({ ...EMPTY, layout: 2, panes: ["a", "b", null, null], active: 1 }, "x")).toMatchObject({ panes: ["a", "x", null, null], active: 1 });
  });

  it("brings a hidden terminal forward by trading places, so the one it covers is kept", () => {
    const next = place({ ...four, layout: 1, active: 0 }, "c");
    expect(next.panes).toEqual(["c", "b", "a", "d"]);
    expect(next.active).toBe(0);
  });

  it("leaves a maximised pane for another one", () => {
    expect(place(maximise(four, 1), "c")).toMatchObject({ zoom: null, active: 2 });
  });
});

describe("closing and restarting", () => {
  it("empties only the closed place", () => {
    expect(close(four, 1).panes).toEqual(["a", null, "c", "d"]);
    expect(close(close(four, 1), 1).panes).toEqual(["a", null, "c", "d"]);
  });

  it("puts a restarted terminal back where the old one was", () => {
    expect(replace(four, "b", "b2").panes).toEqual(["a", "b2", "c", "d"]);
    expect(replace(four, "zz", "b2")).toBe(four);
  });
});

describe("maximise", () => {
  it("draws one place over the grid and gives it back", () => {
    const zoomed = maximise(four, 2);
    expect(shownSlots(zoomed)).toEqual([2]);
    expect(zoomed.active).toBe(2);
    expect(maximise(zoomed, 2).zoom).toBeNull();
    expect(maximise({ ...four, layout: 1 }, 0).zoom).toBeNull();
  });

  it("is left by focusing another place", () => {
    expect(focus(maximise(four, 2), 0)).toMatchObject({ zoom: null, active: 0 });
  });
});

describe("storage", () => {
  it("survives whatever it finds", () => {
    expect(normalise(null)).toEqual(EMPTY);
    expect(normalise({ layout: 7, panes: "x", col: "wide", active: 9 })).toEqual(EMPTY);
    expect(normalise({ layout: 3, panes: ["a", "a", " b ", 4, "c"], col: 0.9 })).toMatchObject({ layout: 4, panes: ["a", null, "b", null], col: 0.8 });
  });
});

describe("the address", () => {
  it("round-trips: what the page writes reads back as the same workspace", () => {
    for (const ws of [four, { ...four, active: 2, col: 0.3 }, { ...four, panes: ["a", null, "c", null], active: 1 }, maximise(four, 3), { ...EMPTY, layout: 2 as const }]) {
      const { detail, query } = toAddress(ws);
      expect(fromAddress(ws, address(detail, new URLSearchParams(query).toString()))).toEqual(ws);
    }
  });

  it("names the active terminal in the path and the places in the query", () => {
    expect(toAddress({ ...four, active: 1 })).toEqual({ detail: "b", query: { layout: "4", panes: "a,b,c,d" } });
    expect(toAddress({ ...EMPTY, panes: [null, "b", null, null] })).toEqual({ detail: null, query: { layout: "1", panes: ",b", view: "workspace" } });
  });

  it("opens a pasted link on the same grid, whatever this browser had stored", () => {
    const ws = fromAddress(EMPTY, address("c", "layout=4&panes=a,b,c"));
    expect(ws).toMatchObject({ layout: 4, panes: ["a", "b", "c", null], active: 2 });
  });

  it("shows a bare terminal link in the stored workspace", () => {
    expect(fromAddress({ ...four, active: 0 }, address("d"))).toMatchObject({ panes: ["a", "b", "c", "d"], active: 3 });
    expect(fromAddress(EMPTY, address("x"))).toMatchObject({ panes: ["x", null, null, null], active: 0 });
  });

  it("still opens the old full-screen grid address side by side", () => {
    expect(fromAddress(EMPTY, address("a", "with=b,c"))).toMatchObject({ layout: 4, panes: ["a", "b", "c", null], active: 0 });
    expect(fromAddress(EMPTY, address("a", "with=b"))).toMatchObject({ layout: 2, panes: ["a", "b", null, null] });
    expect(fromAddress(EMPTY, address("a", "with=b,a,,c,d,e")).panes).toEqual(["a", "b", "c", "d"]);
  });

  it("names nothing when the active place is empty", () => {
    expect(activeId({ ...four, panes: [null, "b", null, null], active: 0 })).toBeNull();
  });
});

describe("which view opens", () => {
  it("is the Workspace when panes are set up, unless the Overview was chosen last", () => {
    expect(openingView(null, EMPTY)).toBe("overview");
    expect(openingView("workspace", EMPTY)).toBe("overview");
    expect(openingView(null, four)).toBe("workspace");
    expect(openingView("workspace", four)).toBe("workspace");
    expect(openingView("overview", four)).toBe("overview");
  });
});
