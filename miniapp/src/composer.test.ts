// The composer's one circle, its keys, its draft and its docks, read back without a browser.

import { describe, expect, it } from "vitest";
import { t } from "./i18n";
import {
  answeredTitle,
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
  readQueued,
  readReturned,
  returnedDraft,
  returnedNoticeKey,
  sendIntent,
  settledIds,
  queuedAfter,
  withQueued,
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

  it("stops a run when the pill is empty and queues the message for after the turn when it is not", () => {
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
    // A guessed next message is taken by Tab; the palette still wins, and Shift+Tab still moves the focus back.
    expect(composerKey(key({ key: "Tab" }), { enterSends: true, paletteOpen: false, ghost: true })).toBe("accept");
    expect(composerKey(key({ key: "Tab" }), { enterSends: true, paletteOpen: true, ghost: true })).toBe("complete");
    expect(composerKey(key({ key: "Tab", shiftKey: true }), { enterSends: true, paletteOpen: false, ghost: true })).toBeNull();
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
  it("keeps the target and points out only a move of the chat, never a setting picked beside the draft", () => {
    const store = new MemoryStorage();
    const target = { session: "s1", project: "p1", workspace: "one", model: "m1", effort: "high", reply: "12:answer" };
    writeDraftTarget("s1", target, store);
    expect(readDraftTarget("s1", store)).toEqual(target);
    expect(readDraftTarget("s2", store)).toBeNull();
    expect(draftTargetChanged(target, { ...target, workspace: "two" })).toBe(true);
    expect(draftTargetChanged(target, { ...target, project: "p2" })).toBe(true);
    expect(draftTargetChanged(target, { ...target, model: "m2", effort: "low", reply: "13:answer" })).toBe(false);
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

describe("the queue", () => {
  it("reads the host's list, keeps which queue holds each message, and drops what is not a message", () => {
    const raw = [
      { id: "q_1", kind: "steer", text: "also the log", queued_at: "2026-09-18T11:04:22+00:00" },
      { id: "q_3", kind: "follow_up", text: "then the docs", queued_at: null },
      { id: "q_2", text: "   " }, { text: "no id" }, null, "junk",
    ];
    expect(readQueued(raw)).toEqual([
      { id: "q_1", kind: "steer", text: "also the log", queued_at: "2026-09-18T11:04:22+00:00", files: [] },
      { id: "q_3", kind: "follow_up", text: "then the docs", queued_at: null, files: [] },
    ]);
    expect(readQueued({ detail: "not found" })).toEqual([]);
    expect(readQueued(undefined)).toEqual([]);
  });

  it("keeps a message that is only a file, and names the files a message carries", () => {
    // A screenshot pasted during a run with no words used to be dropped as an empty card.
    const raw = [
      { id: "shot", kind: "follow_up", text: "", files: ["{8928C48B-9635-4A10-B7D6-0123456789AB}.png"] },
      { id: "both", kind: "steer", text: "this window", files: ["window.png", 7, ""] },
    ];
    expect(readQueued(raw)).toEqual([
      { id: "shot", kind: "follow_up", text: "", queued_at: null, files: ["{8928C48B-9635-4A10-B7D6-0123456789AB}.png"] },
      { id: "both", kind: "steer", text: "this window", queued_at: null, files: ["window.png"] },
    ]);
  });

  it("is replaced whole by every steer_changed event, whatever the reason", () => {
    const before = [{ id: "q_1", kind: "follow_up" as const, text: "one", queued_at: null, files: [] }];
    expect(queuedAfter(before, { reason: "queued", queued: [{ id: "q_1", kind: "follow_up", text: "one" }, { id: "q_2", kind: "follow_up", text: "two" }] })).toHaveLength(2);
    expect(queuedAfter(before, { reason: "steered", queued: [{ id: "q_1", kind: "steer", text: "one" }] })[0].kind).toBe("steer");
    expect(queuedAfter(before, { reason: "consumed", count: 0, queued: [] })).toEqual([]);
    expect(queuedAfter(before, { reason: "cleared" })).toEqual([]);
  });

  it("names the messages that will never reach the transcript, so their sending bubbles can go", () => {
    // A withdrawn message's bubble waited for a transcript row that never comes, and read as sent.
    expect(settledIds({ reason: "withdrawn", withdrawn: ["c-1", 7, ""], queued: [] })).toEqual(["c-1"]);
    expect(settledIds({ reason: "returned", returned: [{ id: "c-2", text: "first", files: [] }, { text: "no id" }] })).toEqual(["c-2"]);
    expect(settledIds({ reason: "consumed", queued: [] })).toEqual([]);
  });

  it("takes back what a stop returned as one draft, in order, and keeps the names of its files", () => {
    const back = readReturned([{ id: "a", text: "first thought" }, { id: "b", text: "  ", files: ["shot.png", 3] }, { id: "c", text: "second thought" }, null]);
    expect(back).toEqual([
      { id: "a", text: "first thought", files: [] },
      { id: "b", text: "  ", files: ["shot.png"] },
      { id: "c", text: "second thought", files: [] },
    ]);
    expect(returnedDraft(back)).toBe("first thought\n\nsecond thought");
    expect(readReturned({ not: "a list" })).toEqual([]);
  });

  it("says what handed the words back: a revert or a clear is not a stop", () => {
    expect(returnedNoticeKey("stop", false)).toBe("composer.steer.returned");
    expect(returnedNoticeKey(undefined, true)).toBe("composer.steer.returned.files");
    expect(returnedNoticeKey("revert", false)).toBe("composer.steer.returned.revert");
    expect(returnedNoticeKey("clear", true)).toBe("composer.steer.returned.clear.files");
    for (const cause of ["stop", "revert", "clear"]) {
      for (const withFiles of [false, true]) expect(t(returnedNoticeKey(cause, withFiles), { names: "a.png" })).not.toMatch(/^\[/);
    }
  });

  it("draws a card for a message the host has just queued, once, until the host's list replaces it", () => {
    const card = { id: "c-3", kind: "follow_up" as const, text: "after this", queued_at: null, files: [] };
    const once = withQueued([], card);
    expect(once).toEqual([card]);
    // The change event got there first: the host's card is kept, not doubled.
    const hosts = [{ ...card, queued_at: "2026-10-28T10:00:00+00:00" }];
    expect(withQueued(hosts, card)).toBe(hosts);
  });
});

describe("the approval dock", () => {
  const refusal = (id: string, key: string, run_id?: string) => ({ run_id, tool_calls: [], tool_results: [{ id, content: `refused by policy.\nApproval key: ${key}`, is_error: true }] });
  const call = (id: string, name: string, args: Record<string, unknown>, run_id?: string) => ({ run_id, tool_calls: [{ id, name, arguments: args }], tool_results: [] });
  const open = (...keys: string[]) => ({ open: keys });

  it("names the refused call and its key", () => {
    const a = pendingApproval([call("c1", "Exec", { command: "rm -rf build" }), refusal("c1", "0123456789ab")], new Set(), open("0123456789ab"));
    expect(a).toEqual({ key: "0123456789ab", callId: "c1", tool: "Exec", detail: "rm -rf build" });
  });

  it("makes the agent's ask for the operator's Chrome a switch naming that Chrome and the reason", () => {
    const asked = { tool_calls: [], tool_results: [{ id: "c1", content: "Waiting for the operator: asked to work in their Chrome «Work». Approval key: 0123456789ab.", is_error: true }] };
    const a = pendingApproval([call("c1", "BrowserAskOperatorChrome", { reason: "read the page signed in as you" }), asked], new Set(), open("0123456789ab"));
    expect(a).toEqual({ key: "0123456789ab", callId: "c1", tool: "BrowserAskOperatorChrome", detail: "read the page signed in as you", chrome: { name: "Work", reason: "read the page signed in as you" } });
  });

  it("is silent once the key was spent or waved away, and for an ordinary error", () => {
    const history = [call("c1", "Exec", { command: "x" }), refusal("c1", "0123456789ab")];
    expect(pendingApproval(history, new Set(["c1"]), open("0123456789ab"))).toBeNull();
    expect(pendingApproval([call("c1", "Exec", { command: "x" }), { tool_calls: [], tool_results: [{ id: "c1", content: "exit 1", is_error: true }] }], new Set(), open("0123456789ab"))).toBeNull();
  });

  // The bank form: allowed once, the agent clicked again, and the same key came back on a new call.
  // The screen that answered the first refusal offered nothing, and the buttons were in the folded steps.
  it("offers a refusal of the same action again after the first one was answered", () => {
    const history = [call("c1", "BrowserAct", { action: "click" }, "r1"), refusal("c1", "a1b2c3d4e5f6", "r1"), call("c2", "BrowserAct", { action: "click" }, "r2"), refusal("c2", "a1b2c3d4e5f6", "r2")];
    expect(pendingApproval(history, new Set(["c1"]), open("a1b2c3d4e5f6"))).toMatchObject({ key: "a1b2c3d4e5f6", callId: "c2" });
    expect(pendingApproval(history, new Set(["c1", "c2"]), open("a1b2c3d4e5f6"))).toBeNull();
    // A browser action has no command to quote; the dock says what the host asked about instead.
    const said = { tool_calls: [], tool_results: [{ id: "c3", content: "[in the operator's own Chrome «Work»]\nneeds the operator's approval: this would submit a form on example.org (rule browser.sensitive). Approval key: 0123456789ab.", is_error: true }] };
    expect(pendingApproval([call("c3", "BrowserAct", { action: "click" }), said], new Set(), open("0123456789ab"))?.detail).toBe("this would submit a form on example.org");
  });

  it("offers the newest refusal", () => {
    const history = [call("c1", "Exec", { command: "a" }), refusal("c1", "0123456789ab"), call("c2", "WebFetch", { url: "https://example.org" }), refusal("c2", "abcdef012345")];
    const both = open("0123456789ab", "abcdef012345");
    expect(pendingApproval(history, new Set(), both)?.key).toBe("abcdef012345");
    expect(pendingApproval(history, new Set(), both)?.detail).toBe("https://example.org");
  });

  // The meal-plan chat: allowed, done by hand, and still offered on every reload days later.
  it("offers nothing the host no longer lists as open, however the request was closed", () => {
    const history = [call("c1", "BrowserAct", { action: "click" }), refusal("c1", "ed4293a63452")];
    expect(pendingApproval(history, new Set(), open())).toBeNull();
    expect(pendingApproval(history, new Set(), { open: undefined })).toBeNull();
    expect(pendingApproval(history, new Set(), open("0123456789ab"))).toBeNull();
    // A newer refusal that is closed does not hide an older one in the same run that is still open.
    const two = [...history, call("c2", "Exec", { command: "b" }), refusal("c2", "abcdef012345")];
    expect(pendingApproval(two, new Set(), open("ed4293a63452"))?.key).toBe("ed4293a63452");
  });

  it("offers nothing from a run older than the newest one", () => {
    const history = [call("c1", "Exec", { command: "x" }, "r1"), refusal("c1", "0123456789ab", "r1"), { run_id: "r2", tool_calls: [], tool_results: [] }];
    expect(pendingApproval(history, new Set(), open("0123456789ab"))).toBeNull();
    // A run that has started but written nothing yet is the newest run too.
    expect(pendingApproval(history.slice(0, 2), new Set(), { open: ["0123456789ab"], runId: "r2" })).toBeNull();
    // The run that asked, idle now that the agent ended its turn to wait: still offered.
    expect(pendingApproval(history.slice(0, 2), new Set(), { open: ["0123456789ab"], runId: "r1" })?.key).toBe("0123456789ab");
    expect(pendingApproval(history.slice(0, 2), new Set(), { open: ["0123456789ab"], runId: null })?.key).toBe("0123456789ab");
  });
});

describe("the agent's questions", () => {
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

describe("Voice mode in ordinary chats", () => {
  // Voice mode is a beta with a chat of its own. A round button that led there sat beside the
  // microphone in every chat's composer, on the phone in both modes and on the start screen; the
  // composers offer dictation and nothing else, and Voice mode is reached from the navigation.
  const sources = import.meta.glob(["./composerbox.tsx", "./screens/Start.tsx", "./screens/Session.tsx", "./orchestration.tsx"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

  it("is not offered by any chat composer", () => {
    expect(Object.keys(sources).length).toBe(4);
    for (const [file, text] of Object.entries(sources)) {
      expect(text, file).not.toMatch(/pathFor\("voice"\)|data-action="voice"|VoiceCircle/);
    }
  });
});

describe("a question settled elsewhere", () => {
  it("is titled by who settled it, and every title is a word of the dictionary", () => {
    expect(answeredTitle("orchestrator")).toBe("composer.answered.orchestrator");
    expect(answeredTitle("operator")).toBe("composer.answered.operator");
    expect(answeredTitle("system")).toBe("composer.answered.system");
    expect(answeredTitle("staff")).toBe("composer.answered.other");
    for (const by of ["orchestrator", "operator", "system", ""]) expect(t(answeredTitle(by))).not.toBe(answeredTitle(by));
  });
});
