import { describe, expect, it } from "vitest";
import type { BrowserGroup } from "../api";
import { actionWords, domainOf, driveState, extraCount, isOpenGroup, mergeActions, nearestCorner, needsOf, needWords, pipGroup, pipHidden, readCorner, rememberCorner, rowOfEvent, savingData, secure, viewGone } from "./model";

function group(over: Partial<BrowserGroup> = {}): BrowserGroup {
  return {
    id: "g1", owner: { kind: "session", id: "s1", label: "Bakery" }, session_id: "s1", staff_id: null, project_id: null, profile: "session-s1", env: "container",
    status: "running", viewport: { w: 1280, h: 800 }, tabs: [{ id: "t1", url: "https://shop.example.com/", title: "Shop", favicon_url: "", loading: false, active: true }],
    active_tab: "t1", control: { owner: "agent", holder: null, until: null, reason: "" }, needs_you: null, acting: false, last_action: null,
    created_at: "2026-09-26T10:00:00Z", last_activity_at: "2026-09-26T10:00:00Z", ...over,
  };
}

describe("who drives", () => {
  it("reads the most specific fact first", () => {
    expect(driveState(group())).toBe("idle");
    expect(driveState(group({ acting: true }))).toBe("acting");
    expect(driveState(group({ control: { owner: "paused", holder: null, until: null, reason: "" } }))).toBe("paused");
    const need = { reason: "login", what: "Sign in to github.com", url: "https://github.com/login", at: "" };
    expect(driveState(group({ needs_you: need, control: { owner: "paused", holder: null, until: null, reason: "login" } }))).toBe("needs");
    expect(driveState(group({ needs_you: need, control: { owner: "human", holder: "laptop", until: null, reason: "" } }))).toBe("other");
    expect(driveState(group({ status: "closed", acting: true }))).toBe("closed");
  });

  it("believes this window's socket about whose hand it is", () => {
    const held = group({ control: { owner: "human", holder: "c1", until: null, reason: "" } });
    expect(driveState(held, { owner: "human", holder: "you", until: null, reason: "" })).toBe("you");
    expect(driveState(held, { owner: "agent", holder: null, until: null, reason: "" })).toBe("idle");
  });

  // The transitions a take-and-give-back goes through, as the banner shows them.
  it("walks agent → you → agent, and paused → agent on resume", () => {
    const g = group({ acting: true });
    const steps = [
      { owner: "agent", holder: null },
      { owner: "human", holder: "you" },
      { owner: "agent", holder: null },
      { owner: "paused", holder: null },
      { owner: "agent", holder: null },
    ] as const;
    expect(steps.map((c) => driveState(g, { ...c, until: null, reason: "" }))).toEqual(["acting", "you", "acting", "paused", "acting"]);
  });
});

describe("addresses", () => {
  it("shows the host without www, and the address itself when it has none", () => {
    expect(domainOf("https://www.github.com/login?x=1")).toBe("github.com");
    expect(domainOf("http://127.0.0.1:8103/menu")).toBe("127.0.0.1:8103");
    expect(domainOf("about:blank")).toBe("about:blank");
    expect(domainOf("not a url")).toBe("not a url");
  });

  it("locks https only", () => {
    expect(secure("https://a.example")).toBe(true);
    expect(secure("http://a.example")).toBe(false);
  });
});

describe("the action log in words", () => {
  it("says what the agent did to which element", () => {
    expect(actionWords({ kind: "click", element: "the Add to cart button", name: "Add to cart" })).toEqual({ key: "browser.act.click", vars: { what: "Add to cart" } });
    expect(actionWords({ kind: "type", element: "the search field", name: "Search", text: "rye bread" })).toEqual({ key: "browser.act.typed", vars: { what: "Search", text: "rye bread" } });
    expect(actionWords({ kind: "type", element: "the password", name: "Password", text_len: 14 })).toEqual({ key: "browser.act.typedn", vars: { what: "Password", n: 14 } });
    expect(actionWords({ kind: "navigate", element: "", name: "", url: "https://www.github.com/x" })).toEqual({ key: "browser.act.open", vars: { where: "github.com" } });
    expect(actionWords({ kind: "handoff", element: "", name: "", needs: { reason: "login", what: "sign in" } }).key).toBe("browser.act.handoff");
    expect(actionWords({ kind: "teleport", element: "x", name: "" }).key).toBe("browser.act.other");
    expect(actionWords({ kind: "dialog", element: "Saved!", name: "", actor: "page" })).toEqual({ key: "browser.act.dialog_auto", vars: { what: "Saved!" } });
    expect(actionWords({ kind: "dialog", element: "", name: "", actor: "agent" }).key).toBe("browser.act.dialog");
  });

  it("merges the listing and the live rows by id, newest first", () => {
    const event = { type: "action" as const, id: "a3", group: "g1", tab: "t1", actor: "agent" as const, kind: "click", name: "Pay", element: "the Pay button", at: Date.parse("2026-09-26T10:00:03Z") };
    const live = rowOfEvent(event);
    const listed = [
      { id: "a1", at: "2026-09-26T10:00:01Z", actor: "agent", kind: "navigate", element: "", name: "", tab: "t1", url: "https://shop.example.com" },
      { id: "a2", at: "2026-09-26T10:00:02Z", actor: "agent", kind: "type", element: "search", name: "Search", tab: "t1", text: "rye" },
    ];
    expect(mergeActions(listed, [live]).map((r) => r.id)).toEqual(["a3", "a2", "a1"]);
    const again = mergeActions([...listed, { ...rowOfEvent(event), sensitive: { kinds: ["purchase"], decision: "asked" } }], [live]);
    expect(again.length).toBe(3);
    expect(again[0].sensitive?.decision).toBe("asked");
  });
});

describe("the corner preview", () => {
  it("snaps to the corner nearest where it was let go", () => {
    expect(nearestCorner(900, 100, 1000, 800)).toBe("tr");
    expect(nearestCorner(100, 100, 1000, 800)).toBe("tl");
    expect(nearestCorner(100, 700, 1000, 800)).toBe("bl");
    expect(nearestCorner(700, 700, 1000, 800)).toBe("br");
  });

  it("remembers the corner per device and falls back to the top right", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    expect(readCorner(storage)).toBe("tr");
    rememberCorner("bl", storage);
    expect(readCorner(storage)).toBe("bl");
    store.set("daedalus.browser.pip.corner", "middle");
    expect(readCorner(storage)).toBe("tr");
  });

  it("shows the group that needs the operator over the one that is busy", () => {
    const busy = group({ id: "g1", acting: true, last_activity_at: "2026-09-26T10:05:00Z" });
    const needs = group({ id: "g2", needs_you: { reason: "login", what: "sign in", url: "", at: "" }, last_activity_at: "2026-09-26T10:01:00Z" });
    const closed = group({ id: "g3", status: "closed", needs_you: { reason: "login", what: "", url: "", at: "" } });
    expect(pipGroup([busy, needs, closed])?.id).toBe("g2");
    expect(pipGroup([closed])).toBeNull();
    // The host reports a group it closed after ten idle minutes as "idle": that is a closed browser,
    // and its card stayed in the corner as an empty frame until this was read as closed.
    const idled = group({ id: "g4", status: "idle", tabs: [] });
    expect(isOpenGroup(idled)).toBe(false);
    expect(pipGroup([idled])).toBeNull();
    expect(driveState(idled)).toBe("closed");
    expect(extraCount([busy, idled], busy)).toBe(0);
    expect(pipGroup([group({ id: "a", last_activity_at: "1" }), group({ id: "b", last_activity_at: "2" })])?.id).toBe("b");
  });

  it("stays hidden until the group does something new or asks for the operator", () => {
    const g = group({ last_activity_at: "2026-09-26T10:00:00Z" });
    const hidden = new Map([["g1", "2026-09-26T10:00:00Z"]]);
    expect(pipHidden(g, hidden)).toBe(true);
    expect(pipHidden({ ...g, last_activity_at: "2026-09-26T10:00:05Z" }, hidden)).toBe(false);
    expect(pipHidden({ ...g, needs_you: { reason: "captcha", what: "", url: "", at: "" } }, hidden)).toBe(false);
    expect(pipHidden(g, new Map())).toBe(false);
  });

  it("counts the other tabs and groups for its chip", () => {
    const a = group({ tabs: [...group().tabs, { id: "t2", url: "", title: "", favicon_url: "", loading: false, active: false }] });
    expect(extraCount([a, group({ id: "g2" })], a)).toBe(2);
    expect(extraCount([group()], group())).toBe(0);
  });
});

describe("saving data", () => {
  it("follows the connection's own hint, and is always on in Telegram on a phone", () => {
    expect(savingData({ connection: { saveData: true } }, false)).toBe(true);
    expect(savingData({ connection: { effectiveType: "3g" } }, false)).toBe(true);
    expect(savingData({ connection: { effectiveType: "4g" } }, false)).toBe(false);
    expect(savingData(undefined, false)).toBe(false);
    expect(savingData(undefined, true)).toBe(true);
  });
});

describe("which request is current", () => {
  const need = { reason: "login", what: "sign in", url: "", at: "" };
  const control = (owner: "agent" | "human" | "paused") => ({ owner, holder: null, until: null, reason: "" });
  it("takes the socket's own request first", () => {
    expect(needsOf(null, { needs: need, control: control("paused"), clientId: "c1" })).toBe(need);
  });
  it("keeps the listing's while nothing live says otherwise", () => {
    expect(needsOf(need, { needs: null, control: null, clientId: null })).toBe(need);
    expect(needsOf(need, { needs: null, control: control("paused"), clientId: "c1" })).toBe(need);
  });
  it("drops a request control has already answered", () => {
    expect(needsOf(need, { needs: null, control: control("agent"), clientId: "c1" })).toBeNull();
    expect(needsOf(need, { needs: null, control: control("human"), clientId: "c1" })).toBeNull();
  });
});

describe("a request in words", () => {
  it("is the agent's sentence, or the reason's for one the daemon raised without one", () => {
    expect(needWords({ reason: "login", what: "Sign in to accounts.example.com" })).toBe("Sign in to accounts.example.com");
    expect(needWords({ reason: "field_forbidden", what: "" })).toBe("type into a password or payment field");
    expect(needWords({ reason: "future", what: "" })).toBe("Needs you");
    expect(needWords(null)).toBe("");
  });
});

describe("the action log's rows", () => {
  it("are one row per action when the host's listing and the live event both have it", () => {
    const live = { id: "a3f-7", at: "2026-09-26T10:00:01Z", actor: "agent", kind: "click", element: "the button", name: "Buy", tab: "t1" };
    const listed = { ...live, id: "412", action_id: "a3f-7" };
    const rows = mergeActions([listed], [live]);
    expect(rows.map((r) => r.id)).toEqual(["412"]);
  });
});

describe("a browser the host no longer has", () => {
  it("is gone only when the socket was refused for that reason", () => {
    expect(viewGone({ kind: "unavailable", reason: "gone" })).toBe(true);
    expect(viewGone({ kind: "unavailable", reason: "environment" })).toBe(false);
    expect(viewGone({ kind: "reconnecting" })).toBe(false);
    expect(isOpenGroup(group())).toBe(true);
    expect(isOpenGroup(group({ status: "lost" }))).toBe(false);
  });
});
