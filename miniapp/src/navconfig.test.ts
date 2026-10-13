// The resolver that places every destination. The first claim is the one a fresh installation rests
// on: with nothing configured the rail, the menu and the drawer are exactly the lists the app has
// always drawn, written out here rather than imported so a change to the defaults is a change to this
// file too.

import { describe, expect, it } from "vitest";
import { type NavConfig, type PageInfo, catalogue, desktopLayout, drawerLayout, iconPath, moveItem, pagePath, resolveNav } from "./navconfig";

const ids = (items: { id: string }[]) => items.map((it) => it.id);
const DOSING: PageInfo = { slug: "dosing", title: { en: "Dosing", ru: "Дозировка" }, icon: null, icon_path: "M4 12h16M12 4v16", path: "/app/p/dosing", version: 3, updated_at: "2026-10-11T08:00:00Z" };

describe("with nothing configured", () => {
  const nav = resolveNav({ config: null, pages: null, selfdev: "server" });

  it("draws the rail the app has always drawn", () => {
    expect(ids(nav.rail)).toEqual(["inbox", "board", "calendar", "diagrams", "changes", "terminals", "companion", "schedules"]);
  });

  it("puts the rest under More, in their groups", () => {
    expect(nav.more.map((g) => [g.key, ids(g.items)])).toEqual([
      ["work", ["harnesses"]],
      ["autonomy", ["services"]],
      ["knowledge", ["memory"]],
      ["observe", ["usage", "health"]],
    ]);
  });

  it("gives each mode its own phone drawer and one More sheet", () => {
    expect(ids(nav.drawer.agents)).toEqual(["inbox", "terminals", "board", "companion", "usage"]);
    expect(ids(nav.drawer.orchestration)).toEqual(["inbox", "terminals", "usage"]);
    expect(ids(nav.phoneMore)).toEqual(["calendar", "diagrams", "changes", "schedules", "services", "memory", "harnesses", "health"]);
  });

  it("is the same answer for an empty section as for a missing one", () => {
    expect(resolveNav({ config: { rail: [], more: [], drawer: [], hidden: [], icons: {} }, selfdev: "server" })).toEqual(nav);
  });

  it("names every item and links it to its screen", () => {
    const inbox = nav.rail[0];
    expect(inbox).toMatchObject({ id: "inbox", kind: "screen", screen: "inbox", title: "Inbox", icon: "inbox", iconPath: null, path: "/app/inbox" });
  });
});

describe("what the installation has", () => {
  it("leaves Changes out where the agent cannot change its own code", () => {
    const nav = resolveNav({ selfdev: "off" });
    expect(ids(nav.rail)).not.toContain("changes");
    expect(ids(nav.phoneMore)).not.toContain("changes");
    expect(nav.catalogue.some((it) => it.id === "changes")).toBe(false);
  });

  it("carries the counts as badges", () => {
    const nav = resolveNav({ selfdev: "server", counts: { inbox: 4, changes: 2 } });
    expect(nav.rail.find((it) => it.id === "inbox")?.badge).toBe(4);
    expect(nav.rail.find((it) => it.id === "changes")?.badge).toBe(2);
    expect(nav.rail.find((it) => it.id === "board")?.badge).toBe(0);
  });
});

describe("the operator's layout", () => {
  it("orders the rail as listed, and an unlisted shipped place keeps its spot after them", () => {
    const nav = resolveNav({ config: { rail: ["terminals", "inbox", "memory"] }, selfdev: "server" });
    expect(ids(nav.rail)).toEqual(["terminals", "inbox", "memory", "board", "calendar", "diagrams", "changes", "companion", "schedules"]);
    expect(nav.more.find((g) => g.key === "knowledge")).toBeUndefined();
  });

  it("moves a shipped rail place under More, into its group, in the listed order", () => {
    const nav = resolveNav({ config: { more: ["health", "board", "usage"] }, selfdev: "server" });
    expect(ids(nav.rail)).not.toContain("board");
    expect(ids(nav.more.find((g) => g.key === "work")!.items)).toEqual(["board", "harnesses"]);
    expect(ids(nav.more.find((g) => g.key === "observe")!.items)).toEqual(["health", "usage"]);
  });

  it("hides an item everywhere, over every other list", () => {
    const nav = resolveNav({ config: { rail: ["companion"], drawer: ["companion", "inbox"], hidden: ["companion", "usage"] }, selfdev: "server" });
    for (const list of [nav.rail, nav.more.flatMap((g) => g.items), nav.drawer.agents, nav.drawer.orchestration, nav.phoneMore, nav.shown]) {
      expect(ids(list)).not.toContain("companion");
      expect(ids(list)).not.toContain("usage");
    }
    expect(nav.catalogue.some((it) => it.id === "companion")).toBe(true);
  });

  it("ignores ids it does not know", () => {
    const nav = resolveNav({ config: { rail: ["page:gone", "nonsense", "inbox"], drawer: ["page:gone", "board"], hidden: ["ghost"], icons: { ghost: "bolt", inbox: "not-an-icon" } }, selfdev: "server" });
    expect(ids(nav.rail)[0]).toBe("inbox");
    expect(ids(nav.drawer.agents)).toEqual(["board"]);
    expect(nav.rail[0].icon).toBe("inbox");
  });

  it("takes a set drawer as the whole drawer, for both modes, and sends the rest to More", () => {
    const nav = resolveNav({ config: { drawer: ["board", "inbox"] }, selfdev: "server" });
    expect(ids(nav.drawer.agents)).toEqual(["board", "inbox"]);
    expect(ids(nav.drawer.orchestration)).toEqual(["board", "inbox"]);
    expect(ids(nav.phoneMore)).toEqual(["companion", "calendar", "diagrams", "terminals", "harnesses", "changes", "schedules", "services", "memory", "usage", "health"]);
  });

  it("draws an icon chosen in place of the item's own", () => {
    const nav = resolveNav({ config: { icons: { terminals: "bolt" } }, selfdev: "server" });
    expect(nav.rail.find((it) => it.id === "terminals")?.icon).toBe("bolt");
  });
});

describe("pages", () => {
  it("join the menu under their own heading and the phone's More, and stay off the rail until placed", () => {
    const nav = resolveNav({ pages: [DOSING], selfdev: "server" });
    const page = nav.more.find((g) => g.key === "pages")!.items[0];
    expect(page).toMatchObject({ id: "page:dosing", kind: "page", screen: null, title: "Dosing", icon: "file", iconPath: "M4 12h16M12 4v16", path: "/app/p/dosing", badge: 0 });
    expect(ids(nav.rail)).not.toContain("page:dosing");
    expect(ids(nav.phoneMore).at(-1)).toBe("page:dosing");
    expect(ids(nav.drawer.agents)).not.toContain("page:dosing");
  });

  it("go where they are placed, like any screen", () => {
    const nav = resolveNav({ config: { rail: ["page:dosing", "inbox"], drawer: ["inbox", "page:dosing"] }, pages: [DOSING], selfdev: "server" });
    expect(ids(nav.rail).slice(0, 2)).toEqual(["page:dosing", "inbox"]);
    expect(ids(nav.drawer.agents)).toEqual(["inbox", "page:dosing"]);
    expect(nav.more.find((g) => g.key === "pages")).toBeUndefined();
  });

  it("take an icon name over their own glyph, and fall back to their slug for a title", () => {
    const named = resolveNav({ config: { icons: { "page:dosing": "flag" } }, pages: [DOSING], selfdev: "server" });
    expect(named.shown.find((it) => it.id === "page:dosing")).toMatchObject({ icon: "flag", iconPath: null });
    const [bare] = catalogue([{ slug: "cad", title: null, icon: "pen", icon_path: "<script>" }], "server").filter((it) => it.kind === "page");
    expect(bare).toMatchObject({ title: "cad", icon: "pen", iconPath: null, path: pagePath("cad") });
  });

  it("are listed once, and a broken entry is skipped", () => {
    const pages = [DOSING, DOSING, { slug: "" }, null as unknown as PageInfo];
    expect(catalogue(pages, "server").filter((it) => it.kind === "page")).toHaveLength(1);
  });
});

describe("a page's own glyph", () => {
  it("is one path on the 24 grid", () => {
    expect(iconPath("M4 12h16M12 4v16")).toBe("M4 12h16M12 4v16");
    expect(iconPath("  m12 3a9 9 0 1 0 0 18\n9 9 0 0 0 0-18z ")).toBe("m12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z");
    expect(iconPath("M-.5 1.5l3-3")).toBe("M-.5 1.5l3-3");
  });

  it("refuses anything that is not path commands and numbers", () => {
    for (const bad of ['M4 4"/><script>', "M4 4 url(x)", "<path d='M1 1'/>", "4 4h16", "M", "", null, 42, "M4 4e5"]) {
      expect(iconPath(bad)).toBeNull();
    }
  });

  it("refuses a coordinate off the grid, and a path too long to be a glyph", () => {
    expect(iconPath("M100 100h800")).toBeNull();
    expect(iconPath("M1 1" + "l1 1".repeat(400))).toBeNull();
  });
});

describe("what Settings writes", () => {
  it("splits its order into the three lists and keeps what it could not show", () => {
    const previous: NavConfig = { rail: ["page:away", "inbox"], hidden: ["page:gone"] };
    const known = new Set(["inbox", "board", "companion"]);
    expect(desktopLayout(previous, ["board", "inbox", "companion"], { board: "rail", inbox: "rail", companion: "hidden" }, known)).toEqual({
      rail: ["board", "inbox", "page:away"],
      more: [],
      hidden: ["companion", "page:gone"],
    });
    expect(drawerLayout({ drawer: ["page:away", "inbox"] }, ["board"], known)).toEqual(["board", "page:away"]);
  });

  it("moves one item and closes the gap", () => {
    expect(moveItem(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveItem(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
    expect(moveItem(["a", "b"], 5, 0)).toEqual(["a", "b"]);
  });
});
