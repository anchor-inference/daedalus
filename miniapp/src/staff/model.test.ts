import { describe, expect, it } from "vitest";
import type { ChannelHealth, StaffMessage, StaffTurn } from "../api";
import { setLang } from "../i18n";
import { ASIDE_KEY, ASIDE_TERMINAL_MIN, ASIDE_W_MAX, ASIDE_W_MIN, alwaysServer, askWords, clampAside, mcpServer, readAsideWidth, rememberAsideWidth, ruleWords } from "./model";
import { answeredBy, applyMessageEvent, attention, canAlways, channelWords, composerWhen, defaultMode, healthParts, keyboardBlocks, listRows, mergeTurns, nextSince, nowChoice, openRequests, outboxRows, stripRows, turnFacts } from "./model";

const msg = (id: string, state: StaffMessage["state"], created_at: string, origin: StaffMessage["origin"] = "orchestrator"): StaffMessage => ({
  id, staff_id: "st-ira", origin, text: id, mode: "after_turn", state, attempts: 1, created_at, updated_at: created_at, error: "",
});

const turn = (index: number, role: StaffTurn["role"], text: string, started_at = ""): StaffTurn => ({ index, role, text, tools: [], started_at, ended_at: "", usage: null });

describe("a receipt", () => {
  const rows = [msg("m1", "queued", "2026-09-25T10:00:00Z"), msg("m2", "submitted", "2026-09-25T10:01:00Z")];

  it("moves forward with its event", () => {
    const after = applyMessageEvent(rows, { message_id: "m1", state: "written" });
    expect(after.map((m) => m.state)).toEqual(["written", "submitted"]);
    expect(applyMessageEvent(after, { message_id: "m1", state: "acknowledged" })[0].state).toBe("acknowledged");
  });

  it("never goes back, and never leaves accepted or failed", () => {
    expect(applyMessageEvent(rows, { message_id: "m2", state: "written" })).toBe(rows);
    const accepted = applyMessageEvent(rows, { message_id: "m2", state: "acknowledged" });
    expect(applyMessageEvent(accepted, { message_id: "m2", state: "failed" })[1].state).toBe("acknowledged");
    const failed = applyMessageEvent(rows, { message_id: "m1", state: "failed", error: "no acknowledgement" });
    expect([failed[0].state, failed[0].error]).toEqual(["failed", "no acknowledgement"]);
    expect(applyMessageEvent(failed, { message_id: "m1", state: "acknowledged" })[0].state).toBe("failed");
  });

  it("leaves a message it does not hold, or an event it cannot read, to the next read of the list", () => {
    expect(applyMessageEvent(rows, { message_id: "m9", state: "written" })).toBe(rows);
    expect(applyMessageEvent(rows, { message_id: "m1", state: "sideways" })).toBe(rows);
    expect(applyMessageEvent(rows, {})).toBe(rows);
  });

  it("is shown newest last, three at most", () => {
    const many = [msg("a", "acknowledged", "2026-09-25T10:00:00Z"), msg("d", "queued", "2026-09-25T10:03:00Z"), msg("b", "failed", "2026-09-25T10:01:00Z"), msg("c", "submitted", "2026-09-25T10:02:00Z")];
    expect(stripRows(many).map((m) => m.id)).toEqual(["b", "c", "d"]);
  });
});

describe("the messages beside the terminal", () => {
  const at = (minute: number) => `2026-09-25T10:${String(minute).padStart(2, "0")}:00Z`;

  it("are listed newest first", () => {
    const rows = [msg("a", "acknowledged", at(0)), msg("c", "queued", at(2)), msg("b", "failed", at(1))];
    expect(listRows(rows).map((m) => m.id)).toEqual(["c", "b", "a"]);
  });

  it("ask for attention while one is on its way or failed with nothing accepted since", () => {
    const quiet = [msg("a", "acknowledged", at(0)), msg("b", "acknowledged", at(1))];
    expect(attention(quiet)).toEqual({ failed: [], pending: [] });
    const failedLast = [msg("a", "acknowledged", at(0)), msg("b", "failed", at(1), "operator")];
    expect(attention(failedLast).failed.map((m) => m.id)).toEqual(["b"]);
    // A failure an accepted message came after is history: the marker is not lit for it.
    const failedBefore = [msg("a", "failed", at(0)), msg("b", "acknowledged", at(1))];
    expect(attention(failedBefore).failed).toEqual([]);
    const moving = [msg("a", "acknowledged", at(0)), msg("b", "written", at(1)), msg("c", "queued", at(2)), msg("d", "submitted", at(3))];
    expect(attention(moving).pending.map((m) => m.id)).toEqual(["d", "c", "b"]);
  });

  it("follow the Feed's last turn, oldest first, only while the transcript cannot have them", () => {
    const rows = [msg("a", "acknowledged", at(0)), msg("c", "queued", at(2)), msg("b", "failed", at(1), "operator"), msg("z", "failed", at(0))];
    expect(outboxRows(rows).map((m) => m.id)).toEqual(["b", "c"]);
  });
});

describe("the Feed", () => {
  it("replaces the turn still being written and adds the new ones in order", () => {
    const have = [turn(0, "user", "task"), turn(1, "assistant", "reading")];
    const fresh = [turn(1, "assistant", "reading the cart, then the tests"), turn(2, "orchestrator", "use the sheet")];
    const merged = mergeTurns(have, fresh);
    expect(merged.map((t) => [t.index, t.text])).toEqual([[0, "task"], [1, "reading the cart, then the tests"], [2, "use the sheet"]]);
    expect(nextSince(merged)).toBe(2);
    expect(mergeTurns(have, [])).toBe(have);
    expect(nextSince([])).toBe(0);
  });

  it("counts the turns asked for and how long the current one has run", () => {
    const now = Date.parse("2026-09-25T10:30:00Z");
    const turns = [turn(0, "user", "task", "2026-09-25T10:00:00Z"), turn(1, "assistant", "ok"), turn(2, "orchestrator", "more", "2026-09-25T10:12:00Z"), turn(3, "assistant", "…")];
    expect(turnFacts(turns, "2026-09-25T10:20:00Z", now)).toEqual({ turn: 2, minutes: 18 });
    expect(turnFacts([], "2026-09-25T10:25:00Z", now)).toEqual({ turn: 0, minutes: 5 });
    expect(turnFacts([], null, now)).toEqual({ turn: 0, minutes: null });
  });
});

describe("what the capabilities decide", () => {
  it("offers 'now' by what a steer means for the CLI", () => {
    expect(nowChoice({ steer: "degrade_to_queue" })).toEqual({ enabled: false, hint: "staff.now.degrades" });
    expect(nowChoice({ steer: "cancel_and_send" })).toEqual({ enabled: true, hint: "staff.now.interrupts" });
    expect(nowChoice({ steer: "native" })).toEqual({ enabled: true, hint: "staff.now.native" });
    expect(nowChoice({ steer: "tui_queue" })).toEqual({ enabled: true, hint: "staff.now.tui" });
    expect(nowChoice(null).enabled).toBe(true);
  });

  it("sends a message now unless the operator chose otherwise, the CLI cannot, or now would stop the turn", () => {
    expect(composerWhen(nowChoice({ steer: "tui_queue" }), null)).toBe("now");
    expect(composerWhen(nowChoice({ steer: "native" }), null)).toBe("now");
    expect(composerWhen(nowChoice(null), null)).toBe("now");
    expect(composerWhen(nowChoice({ steer: "tui_queue" }), "after_turn")).toBe("after_turn");
    expect(composerWhen(nowChoice({ steer: "degrade_to_queue" }), "now")).toBe("after_turn");
    expect(composerWhen(nowChoice({ steer: "cancel_and_send" }), null)).toBe("after_turn");
    expect(composerWhen(nowChoice({ steer: "cancel_and_send" }), "now")).toBe("now");
  });

  it("offers 'always' only on a permission of a CLI that asks for them", () => {
    expect(canAlways({ kind: "permission" }, { permissions: "hook_then_keys" })).toBe(true);
    expect(canAlways({ kind: "permission" }, { permissions: "structured" })).toBe(true);
    expect(canAlways({ kind: "permission" }, { permissions: "none" })).toBe(false);
    expect(canAlways({ kind: "question" }, { permissions: "structured" })).toBe(false);
    expect(canAlways({ kind: "permission" }, null)).toBe(false);
  });

  it("names the status channel by its code, and by the table's label for a code it does not know", () => {
    setLang("ru");
    expect(channelWords({ status_channel: "hooks", status_channel_label: "hooks per launch" })).toBe("хуки на каждый запуск");
    expect(channelWords({ status_channel: "carrier-pigeon", status_channel_label: "a pigeon" })).toBe("a pigeon");
    setLang("en");
    expect(channelWords(null)).toBe("");
  });
});

describe("the rest of the view", () => {
  it("opens on the terminal on a desktop and on the Feed on a phone, unless chosen otherwise", () => {
    expect(defaultMode(1440, null, true)).toBe("terminal");
    expect(defaultMode(390, null, true)).toBe("feed");
    expect(defaultMode(1440, "feed", true)).toBe("feed");
    expect(defaultMode(1440, "terminal", false)).toBe("feed");
  });

  it("shows the keyboard banner only while a person holds it and the orchestrator's message waits", () => {
    const waiting = [msg("m1", "queued", "2026-09-25T10:00:00Z")];
    const mine = [msg("m1", "queued", "2026-09-25T10:00:00Z", "operator")];
    expect(keyboardBlocks({ owner: "human", until: null }, waiting)).toBe(true);
    expect(keyboardBlocks({ owner: "human", until: 1000 }, waiting, 2000)).toBe(false);
    expect(keyboardBlocks({ owner: "auto", until: null }, waiting)).toBe(false);
    expect(keyboardBlocks({ owner: "human", until: null }, mine)).toBe(false);
    expect(keyboardBlocks(null, waiting)).toBe(false);
  });

  it("lists a member's open requests oldest first and reads who answered first", () => {
    const asks = [
      { staff_id: "st-ira", resolved_at: null, created_at: "2026-09-25T10:02:00Z", id: "b" },
      { staff_id: "st-ira", resolved_at: "2026-09-25T10:03:00Z", created_at: "2026-09-25T10:00:00Z", id: "done" },
      { staff_id: "st-max", resolved_at: null, created_at: "2026-09-25T10:00:00Z", id: "other" },
      { staff_id: "st-ira", resolved_at: null, created_at: "2026-09-25T10:01:00Z", id: "a" },
    ];
    expect(openRequests(asks, "st-ira").map((a) => a.id)).toEqual(["a", "b"]);
    expect(answeredBy("request k7m2qd was already answered by the orchestrator")).toBe("orchestrator");
    expect(answeredBy("something else")).toBe("");
  });

  it("says what is wrong with a member's channels first, in the order it matters", () => {
    const health: ChannelHealth = {
      team_tools: "missing", last_hook_at: "2026-09-25T10:00:00Z", last_team_call_at: null, last_signal_at: "2026-09-25T10:00:00Z", silent_s: 420, silence_after_s: 300,
      silent: true, last_message: { id: "m3", state: "failed" }, last_acknowledged_at: "2026-09-25T09:00:00Z", problems: ["team_tools_missing", "silent", "message_failed"], level: "warn",
    };
    expect(healthParts(health).map((p) => [p.key, p.warn])).toEqual([
      ["staff.health.tools.missing", true], ["staff.health.silent", true], ["staff.health.hook", false], ["staff.health.failed", true],
    ]);
    expect(healthParts({ ...health, team_tools: "connected", silent: false, silent_s: 60, last_message: { id: "m3", state: "acknowledged" }, problems: [], level: "ok" }).map((p) => p.key)).toEqual([
      "staff.health.tools.connected", "staff.health.hook", "staff.health.accepted",
    ]);
    expect(healthParts(null)).toEqual([]);
  });
});

describe("always for a whole MCP server", () => {
  const claude = { permissions: "hook_then_keys" as const, standing_rules: true };
  const nav = { kind: "permission" as const, detail: { tool: "mcp__daedalus_browser__BrowserNavigate" } };

  it("is offered for an MCP tool of a CLI that keeps rules, named by its server", () => {
    expect(mcpServer("mcp__daedalus_browser__BrowserNavigate")).toBe("daedalus_browser");
    expect(mcpServer("mcp__shop__cart__add")).toBe("shop");
    expect(alwaysServer(nav, claude)).toBe("daedalus_browser");
  });

  it("is never offered for a built-in tool, a question, or a CLI whose always knows no server", () => {
    for (const tool of ["Bash", "Write", "mcp__shop", "mcp__shop__", "mcpx__shop__cart"]) expect(alwaysServer({ kind: "permission", detail: { tool } }, claude)).toBe("");
    expect(alwaysServer({ kind: "question", detail: nav.detail }, claude)).toBe("");
    expect(alwaysServer(nav, { permissions: "structured", standing_rules: false })).toBe("");
    expect(alwaysServer(nav, { permissions: "structured" })).toBe("");
    expect(alwaysServer(nav, null)).toBe("");
  });

  it("says what a kept rule covers in the operator's words", () => {
    setLang("en");
    expect(ruleWords("mcp__daedalus_browser")).toBe("all daedalus_browser tools");
    expect(ruleWords("mcp__daedalus_browser__BrowserNavigate")).toBe("BrowserNavigate of daedalus_browser");
    setLang("ru");
    expect(ruleWords("mcp__daedalus_browser")).toBe("все инструменты daedalus_browser");
    setLang("en");
    expect(ruleWords("Bash(npm test:*)")).toBe("Bash(npm test:*)");
  });

  it("names an MCP tool in a request the way a rule names it, and leaves every other request alone", () => {
    setLang("en");
    const tool = "mcp__daedalus_browser__BrowserNavigate";
    expect(askWords({ text: `${tool}: https://example.com`, detail: { tool } })).toBe("BrowserNavigate of daedalus_browser: https://example.com");
    expect(askWords({ text: "Bash: npm install", detail: { tool: "Bash" } })).toBe("Bash: npm install");
    expect(askWords({ text: "Which colour?", detail: {} })).toBe("Which colour?");
  });
});

describe("the column beside the terminal", () => {
  const store = () => {
    const kept = new Map<string, string>();
    return { kept, getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v), removeItem: (k: string) => void kept.delete(k) };
  };

  it("is held between its bounds and leaves the terminal its share of the row", () => {
    expect(clampAside(100, 1600)).toBe(ASIDE_W_MIN);
    expect(clampAside(5000, 2400)).toBe(ASIDE_W_MAX);
    expect(clampAside(420.4, 1600)).toBe(420);
    expect(clampAside(900, 1100)).toBe(1100 - ASIDE_TERMINAL_MIN);
    // A row too narrow for both still gives the column its least width.
    expect(clampAside(500, 600)).toBe(ASIDE_W_MIN);
  });

  it("is kept per browser, and a reset gives the stylesheet its own width back", () => {
    const s = store();
    expect(readAsideWidth(s)).toBeNull();
    rememberAsideWidth(432, s);
    expect(s.kept.get(ASIDE_KEY)).toBe("432");
    expect(readAsideWidth(s)).toBe(432);
    rememberAsideWidth(null, s);
    expect(s.kept.has(ASIDE_KEY)).toBe(false);
    expect(readAsideWidth(s)).toBeNull();
  });

  it("reads nothing it cannot trust, and survives a browser with no storage", () => {
    const s = store();
    for (const bad of ["wide", "", "12", String(ASIDE_W_MAX + 1), "NaN"]) {
      s.kept.set(ASIDE_KEY, bad);
      expect(readAsideWidth(s)).toBeNull();
    }
    const broken = { getItem: () => { throw new Error("private"); }, setItem: () => { throw new Error("private"); }, removeItem: () => { throw new Error("private"); } };
    expect(readAsideWidth(broken)).toBeNull();
    expect(() => rememberAsideWidth(400, broken)).not.toThrow();
    expect(() => rememberAsideWidth(null, broken)).not.toThrow();
    expect(readAsideWidth(null)).toBeNull();
  });
});
