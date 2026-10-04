// The composer's one circle, its keys, its draft and its docks, read back without a browser.

import { describe, expect, it } from "vitest";
import {
  answersComplete,
  questionsKey,
  composerKey,
  composerContext,
  clearSendIntent,
  draftTargetChanged,
  dockKey,
  draftKey,
  fieldHeight,
  MAX_ROWS,
  pendingApproval,
  placeholderKey,
  primaryAction,
  readDraft,
  readDraftTarget,
  readSteers,
  sendIntent,
  steersAfter,
  writeDraft,
  writeDraftTarget,
} from "./composer";

const idle = { status: "idle" as const, hasDraft: false, hasFiles: false, asking: false, sending: false };

describe("the primary circle", () => {
  it("sends when there is something to send, and is otherwise disabled", () => {
    expect(primaryAction(idle)).toEqual({ action: "send", enabled: false });
    expect(primaryAction({ ...idle, hasDraft: true })).toEqual({ action: "send", enabled: true });
    expect(primaryAction({ ...idle, hasFiles: true })).toEqual({ action: "send", enabled: true });
  });

  it("stops a run when the pill is empty and queues a steer when it is not", () => {
    expect(primaryAction({ ...idle, status: "running" })).toEqual({ action: "stop", enabled: true });
    expect(primaryAction({ ...idle, status: "running", hasDraft: true })).toEqual({ action: "queue", enabled: true });
    expect(primaryAction({ ...idle, status: "running", hasFiles: true })).toEqual({ action: "queue", enabled: true });
  });

  it("is Reply while the agent waits for an answer, and Send again once the operator types", () => {
    expect(primaryAction({ ...idle, status: "waiting", asking: true })).toEqual({ action: "reply", enabled: true });
    expect(primaryAction({ ...idle, status: "waiting", asking: true, hasDraft: true })).toEqual({ action: "send", enabled: true });
    expect(primaryAction({ ...idle, status: "waiting", asking: false })).toEqual({ action: "send", enabled: false });
  });

  it("does nothing twice while a send is on its way, but a stop is never held back", () => {
    expect(primaryAction({ ...idle, hasDraft: true, sending: true }).enabled).toBe(false);
    expect(primaryAction({ ...idle, status: "running", hasDraft: true, sending: true }).enabled).toBe(false);
    expect(primaryAction({ ...idle, status: "running", sending: true })).toEqual({ action: "stop", enabled: true });
  });

  it("names the placeholder by what typing would do", () => {
    expect(placeholderKey("idle", false)).toBe("session.composer.idle");
    expect(placeholderKey("running", false)).toBe("session.composer.running");
    expect(placeholderKey("waiting", true)).toBe("session.composer.waiting");
    expect(placeholderKey("idle", true)).toBe("session.composer.waiting");
  });
});

const key = (over: Partial<Pick<KeyboardEvent, "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "isComposing" | "keyCode">>) => ({ key: "", code: "", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...over });

describe("keys in the field", () => {
  it("never sends or answers while an input method is composing text", () => {
    expect(composerKey(key({ key: "Enter", isComposing: true }), { enterSends: true, paletteOpen: false })).toBeNull();
    expect(composerKey(key({ key: "Enter", keyCode: 229 }), { enterSends: true, paletteOpen: false })).toBeNull();
    expect(dockKey(key({ key: "н", code: "KeyY", isComposing: true }), false)).toBeNull();
  });
  it("sends on Enter where Enter sends, and inserts a line with Shift", () => {
    expect(composerKey(key({ key: "Enter" }), { enterSends: true, paletteOpen: false })).toBe("send");
    expect(composerKey(key({ key: "Enter", shiftKey: true }), { enterSends: true, paletteOpen: false })).toBe("newline");
    expect(composerKey(key({ key: "Enter" }), { enterSends: false, paletteOpen: false })).toBe("newline");
    expect(composerKey(key({ key: "Enter", ctrlKey: true }), { enterSends: false, paletteOpen: false })).toBe("send");
    expect(composerKey(key({ key: "Enter", metaKey: true }), { enterSends: false, paletteOpen: false })).toBe("send");
  });

  it("reads the letter shortcuts by position, so a Russian layout has the same keys", () => {
    expect(composerKey(key({ key: "ь", code: "KeyM", ctrlKey: true }), { enterSends: true, paletteOpen: false })).toBe("model");
    expect(composerKey(key({ key: "m", code: "KeyM", metaKey: true }), { enterSends: true, paletteOpen: false })).toBe("model");
    expect(composerKey(key({ key: "Ы", code: "KeyS", ctrlKey: true, shiftKey: true }), { enterSends: true, paletteOpen: false })).toBe("stop");
    expect(composerKey(key({ key: "s", code: "KeyS", ctrlKey: true }), { enterSends: true, paletteOpen: false })).toBeNull();
    expect(composerKey(key({ key: "m", code: "KeyM" }), { enterSends: true, paletteOpen: false })).toBeNull();
  });

  it("completes a slash command on Tab only while the palette is open", () => {
    expect(composerKey(key({ key: "Tab" }), { enterSends: true, paletteOpen: true })).toBe("complete");
    expect(composerKey(key({ key: "Tab" }), { enterSends: true, paletteOpen: false })).toBeNull();
    expect(composerKey(key({ key: "Escape" }), { enterSends: true, paletteOpen: true })).toBe("escape");
  });

  it("answers the dock with plain letters and never from inside a text field", () => {
    expect(dockKey(key({ key: "y", code: "KeyY" }), false)).toBe("approve");
    expect(dockKey(key({ key: "н", code: "KeyY" }), false)).toBe("approve");
    expect(dockKey(key({ key: "n", code: "KeyN" }), false)).toBe("deny");
    expect(dockKey(key({ key: "y", code: "KeyY" }), true)).toBeNull();
    expect(dockKey(key({ key: "y", code: "KeyY", ctrlKey: true }), false)).toBeNull();
  });
});

describe("the field's height", () => {
  it("follows the text up to eight lines and no further", () => {
    expect(fieldHeight(40, 21, 20)).toBe(41);
    expect(fieldHeight(600, 21, 20)).toBe(21 * MAX_ROWS + 20);
  });
});

class MemoryStorage {
  map = new Map<string, string>();
  getItem(k: string) {
    return this.map.get(k) ?? null;
  }
  setItem(k: string, v: string) {
    this.map.set(k, v);
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
}

describe("the draft", () => {
  it("keeps the exact target and intent until an explicit review accepts changed settings", () => {
    const store = new MemoryStorage();
    const target = { session: "s1", project: "p1", workspace: "one", model: "m1", mode: "agent", effort: "high", reply: "12:answer", intent: "queue" as const };
    writeDraftTarget("s1", target, store);
    expect(readDraftTarget("s1", store)).toEqual(target);
    expect(readDraftTarget("s2", store)).toBeNull();
    expect(draftTargetChanged(target, { ...target, model: "m2" })).toBe(true);
    expect(draftTargetChanged(target, { ...target, reply: "13:answer" })).toBe(true);
    expect(draftTargetChanged(target, { ...target, intent: "steer" })).toBe(false);
    writeDraftTarget("s1", null, store);
    expect(readDraftTarget("s1", store)).toBeNull();
  });
  it("reuses the pending send identity only for the same complete intent", () => {
    const store = new MemoryStorage();
    const first = sendIntent("s1", 'message:[["a.txt",3,1]]', store);
    expect(sendIntent("s1", 'message:[["a.txt",3,1]]', store)).toBe(first);
    expect(sendIntent("s1", 'changed:[["a.txt",3,1]]', store)).not.toBe(first);
    clearSendIntent("s1", store);
    expect(sendIntent("s1", 'message:[["a.txt",3,1]]', store)).not.toBe(first);
  });
  it("is kept per session and comes back", () => {
    const store = new MemoryStorage();
    writeDraft("s1", "half a thought", store);
    expect(readDraft("s1", store)).toBe("half a thought");
    expect(readDraft("s2", store)).toBe("");
    expect(store.map.has(draftKey("s1"))).toBe(true);
  });

  it("is removed rather than stored empty", () => {
    const store = new MemoryStorage();
    writeDraft("s1", "words", store);
    writeDraft("s1", "   ", store);
    expect(store.map.size).toBe(0);
    expect(readDraft("s1", store)).toBe("");
  });

  it("survives a storage that throws", () => {
    const broken = { getItem: () => { throw new Error("private mode"); }, setItem: () => { throw new Error("full"); }, removeItem: () => { throw new Error("full"); } };
    expect(() => writeDraft("s1", "x", broken)).not.toThrow();
    expect(readDraft("s1", broken)).toBe("");
  });
});

describe("the steer queue", () => {
  it("reads the host's list and drops what is not a steer", () => {
    const raw = [{ id: "q_1", text: "also the log", queued_at: "2026-09-18T11:04:22+00:00" }, { id: "q_2", text: "   " }, { text: "no id" }, null, "junk"];
    expect(readSteers(raw)).toEqual([{ id: "q_1", text: "also the log", queued_at: "2026-09-18T11:04:22+00:00" }]);
    expect(readSteers({ detail: "not found" })).toEqual([]);
    expect(readSteers(undefined)).toEqual([]);
  });

  it("is replaced whole by every steer_changed event, whatever the reason", () => {
    const before = [{ id: "q_1", text: "one", queued_at: null }];
    expect(steersAfter(before, { reason: "queued", queued: [{ id: "q_1", text: "one" }, { id: "q_2", text: "two" }] })).toHaveLength(2);
    expect(steersAfter(before, { reason: "consumed", count: 0, queued: [] })).toEqual([]);
    expect(steersAfter(before, { reason: "cleared" })).toEqual([]);
  });
});

describe("the approval dock", () => {
  const refusal = (id: string, key: string) => ({ tool_calls: [], tool_results: [{ id, content: `refused by policy.\nApproval key: ${key}`, is_error: true }] });
  const call = (id: string, name: string, args: Record<string, unknown>) => ({ tool_calls: [{ id, name, arguments: args }], tool_results: [] });

  it("names the refused call and its key", () => {
    const a = pendingApproval([call("c1", "Exec", { command: "rm -rf build" }), refusal("c1", "0123456789ab")], new Set());
    expect(a).toEqual({ key: "0123456789ab", callId: "c1", tool: "Exec", detail: "rm -rf build" });
  });

  it("is silent once the key was spent or waved away, and for an ordinary error", () => {
    const history = [call("c1", "Exec", { command: "x" }), refusal("c1", "0123456789ab")];
    expect(pendingApproval(history, new Set(["0123456789ab"]))).toBeNull();
    expect(pendingApproval([call("c1", "Exec", { command: "x" }), { tool_calls: [], tool_results: [{ id: "c1", content: "exit 1", is_error: true }] }], new Set())).toBeNull();
  });

  it("offers the newest refusal", () => {
    const history = [call("c1", "Exec", { command: "a" }), refusal("c1", "0123456789ab"), call("c2", "WebFetch", { url: "https://example.org" }), refusal("c2", "abcdef012345")];
    expect(pendingApproval(history, new Set())?.key).toBe("abcdef012345");
    expect(pendingApproval(history, new Set())?.detail).toBe("https://example.org");
  });
});

describe("the agent's questions", () => {
  it("are complete when every one has a choice or a word", () => {
    const qs = [{ question: "a" }, { question: "b" }];
    expect(answersComplete(qs, [{ selected: ["x"], custom: "" }, { selected: [], custom: "" }])).toBe(false);
    expect(answersComplete(qs, [{ selected: ["x"], custom: "" }, { selected: [], custom: "y" }])).toBe(true);
    expect(answersComplete([], [])).toBe(false);
    // Blanks not yet laid out for the questions are not an answer.
    expect(answersComplete(qs, [])).toBe(false);
  });

  it("are the same questions when the session is read again, and new ones when they change", () => {
    const asked = [{ question: "Keep it to the usual 8?", header: "Length", options: [{ label: "Top 8" }, { label: "All 14" }], allow_custom: true }];
    // Every read of the session brings the pending questions as a new array with new objects.
    const reread = JSON.parse(JSON.stringify(asked));
    expect(reread).not.toBe(asked);
    expect(questionsKey(reread)).toBe(questionsKey(asked));
    // A description is not what is asked; a different option or question is.
    expect(questionsKey([{ ...asked[0], options: [{ label: "Top 8", description: "the usual" }, { label: "All 14" }] }])).toBe(questionsKey(asked));
    expect(questionsKey([{ ...asked[0], options: [{ label: "Top 8" }, { label: "All 20" }] }])).not.toBe(questionsKey(asked));
    expect(questionsKey([{ ...asked[0], question: "Keep it short?" }])).not.toBe(questionsKey(asked));
    expect(questionsKey(null)).toBe(questionsKey([]));
  });
});

describe("the card layout", () => {
  it("reserves one phone line, grows, then scrolls after five", () => {
    expect(fieldHeight(0, 22.4, 16, 1, 5)).toBe(39);
    expect(fieldHeight(83, 22.4, 16, 1, 5)).toBe(83);
    expect(fieldHeight(900, 22.4, 16, 1, 5)).toBe(128);
  });

  it("uses the physical Enter keys with either alphabet", () => {
    for (const code of ["Enter", "NumpadEnter"]) {
      expect(composerKey(key({ key: "Unidentified", code }), { enterSends: true, paletteOpen: false })).toBe("send");
      expect(composerKey(key({ key: "Unidentified", code, shiftKey: true }), { enterSends: true, paletteOpen: false })).toBe("newline");
    }
  });

  it("shows short project and distinct workspace names", () => {
    expect(composerContext({ project: "Bakery", workspace: "projects/site/" })).toEqual([
      { kind: "project", name: "Bakery" }, { kind: "workspace", name: "site" },
    ]);
    expect(composerContext({ workspace: "projects\\site" })).toEqual([{ kind: "workspace", name: "site" }]);
  });

  it("omits absent, duplicate, internal and opaque context", () => {
    expect(composerContext()).toEqual([]);
    expect(composerContext({ project: " ", workspace: "." })).toEqual([]);
    expect(composerContext({ project: "Voice", workspace: "voice", system: true })).toEqual([]);
    expect(composerContext({ project: "Bakery", workspace: "bakery" })).toEqual([{ kind: "project", name: "Bakery" }]);
    expect(composerContext({ workspace: "a1b2c3d4e5f6" })).toEqual([]);
  });
});
