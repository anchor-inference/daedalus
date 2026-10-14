// @vitest-environment jsdom
// A custom page and its agent: what the app forwards into each open page's frame (only that page's
// PageEmit events and only its own agent's conversation), the agent's replies handed over once, the
// bridge's agent calls on the page's side, and the page's messages as the chat draws them.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppEvent, MessageView } from "./api";
import { answer, forwardToPage, pageEvents, PROTOCOL, runReplies, runState, serveBridge, type BridgeDeps, type PageContext } from "./pagebridge";
import { systemNote } from "./turns";
import kitSource from "../kit/daedalus.js?raw";

let seq = 0;
function event(type: string, payload: Record<string, unknown>, sessionId: string | null = null): AppEvent {
  seq += 1;
  return { seq, at: "2026-10-11T10:00:00Z", type, project_id: null, session_id: sessionId, staff_id: null, terminal_id: null, payload };
}

function reply(seqNo: number, text: string, runId: string, over: Partial<MessageView> = {}): MessageView {
  return { role: "assistant", seq: seqNo, run_id: runId, text, thinking: "", tool_calls: [], tool_results: [], created_at: "2026-10-11T10:00:00Z", ...over };
}

/** A stand-in for events.ts: handlers registered, events delivered by hand. */
function stream() {
  const handlers = new Set<(e: AppEvent) => void>();
  return {
    on: (_types: readonly string[], handler: (e: AppEvent) => void) => {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
    emit: (e: AppEvent) => [...handlers].forEach((h) => h(e)),
    get count() {
      return handlers.size;
    },
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("what one page is handed", () => {
  const page: PageContext = { slug: "cad", sessionId: "s1" };

  it("passes a PageEmit for its own slug under the agent's own name, and nobody else's", () => {
    expect(pageEvents(event("pages.emit", { slug: "cad", event: "scene.updated", data: { file: "model.glb" } }), page)).toEqual([{ event: "scene.updated", data: { file: "model.glb" } }]);
    expect(pageEvents(event("pages.emit", { slug: "orders", event: "scene.updated", data: 1 }), page)).toEqual([]);
    expect(pageEvents(event("pages.emit", { slug: "cad", event: "x" }), page)).toEqual([{ event: "x", data: null }]);
  });

  it("passes its agent's events and runs, and no other session's", () => {
    const started = pageEvents(event("run.started", { run_id: "r1", origin: "page:cad", title: "CAD" }, "s1"), page);
    expect(started.map((e) => e.event)).toEqual(["agent.event", "agent.run"]);
    expect(started[1].data).toEqual({ run_id: "r1", status: "running", origin: "page:cad" });
    expect(pageEvents(event("run.started", { run_id: "r9", origin: "operator", title: "x" }, "s2"), page)).toEqual([]);
    expect(pageEvents(event("session.unread_result", { unread: true }, "s1"), page).map((e) => e.event)).toEqual(["agent.event"]);
    // A page without an agent hears nothing of any session.
    expect(pageEvents(event("run.started", { run_id: "r1", origin: "x", title: "x" }, "s1"), { slug: "cad", sessionId: null })).toEqual([]);
    expect(pageEvents(event("notify", { title: "x" }), page)).toEqual([]);
  });

  it("reads a run's state from the events that move it", () => {
    expect(runState(event("session.status", { status: "waiting", run_id: "r1" }))).toEqual({ run_id: "r1", status: "waiting" });
    expect(runState(event("run.finished", { run_id: "r1", status: "completed", origin: "page:cad", summary: "Done", duration_s: 2 }))).toEqual({ run_id: "r1", status: "completed", origin: "page:cad", summary: "Done" });
    expect(runState(event("run.finished", { run_id: "r2", status: "failed", origin: "page:cad", error: "boom" }))).toMatchObject({ status: "failed", error: "boom" });
    expect(runState(event("ask.opened", {}))).toBeNull();
  });

  it("hands over a run's answers with words only", () => {
    const messages = [
      { role: "user", seq: 1, run_id: "r1", text: "go", thinking: "", tool_calls: [], tool_results: [], created_at: "" } as MessageView,
      reply(2, "", "r1"),
      reply(3, "Rebuilt the mesh.", "r1"),
      reply(4, "Older", "r0"),
      reply(5, "Summary", "r1", { summary: true }),
      reply(6, "Note", "r1", { internal: true }),
    ];
    expect(runReplies(messages, "r1", -1).map((r) => r.text)).toEqual(["Rebuilt the mesh."]);
    expect(runReplies(messages, "r1", 3)).toEqual([]);
  });
});

describe("forwarding into frames", () => {
  it("sends each frame only its own page's events, follows a rebinding, and stops", async () => {
    const events = stream();
    const cad: { event: string; data: unknown }[] = [];
    const orders: { event: string; data: unknown }[] = [];
    let cadPage: PageContext = { slug: "cad", sessionId: "s1" };
    const messages = vi.fn(async (sessionId: string) => (sessionId === "s1" ? [reply(7, "Optimised.", "r1"), reply(8, "And smoothed.", "r1")] : [reply(3, "Orders done", "r5")]));
    const stopCad = forwardToPage((e, d) => cad.push({ event: e, data: d }), () => cadPage, { on: events.on, messages });
    const stopOrders = forwardToPage((e, d) => orders.push({ event: e, data: d }), () => ({ slug: "orders", sessionId: "s2" }), { on: events.on, messages });

    events.emit(event("pages.emit", { slug: "cad", event: "scene.updated", data: { file: "model.glb" } }));
    expect(cad).toEqual([{ event: "scene.updated", data: { file: "model.glb" } }]);
    expect(orders).toEqual([]);

    events.emit(event("run.finished", { run_id: "r1", status: "completed", origin: "page:cad", summary: "Optimised." }, "s1"));
    await flush();
    expect(cad.filter((e) => e.event === "agent.message").map((e) => (e.data as { text: string }).text)).toEqual(["Optimised.", "And smoothed."]);
    expect(orders.some((e) => e.event.startsWith("agent."))).toBe(false);
    // The same run's end heard twice (a second tab, a replay) hands nothing over twice.
    events.emit(event("run.finished", { run_id: "r1", status: "completed", origin: "page:cad" }, "s1"));
    await flush();
    expect(cad.filter((e) => e.event === "agent.message")).toHaveLength(2);

    // Rebound to another session: the old one's events no longer reach the page.
    cadPage = { slug: "cad", sessionId: "s3" };
    const before = cad.length;
    events.emit(event("run.started", { run_id: "r2", origin: "operator", title: "x" }, "s1"));
    expect(cad.length).toBe(before);

    stopCad();
    stopOrders();
    expect(events.count).toBe(0);
    events.emit(event("pages.emit", { slug: "cad", event: "late", data: 1 }));
    expect(cad.some((e) => e.event === "late")).toBe(false);
  });

  it("drops answers read for an agent the page no longer has", async () => {
    const events = stream();
    const sent: string[] = [];
    let page: PageContext = { slug: "cad", sessionId: "s1" };
    let release: (m: MessageView[]) => void = () => undefined;
    const messages = () => new Promise<MessageView[]>((resolve) => { release = resolve; });
    forwardToPage((e) => sent.push(e), () => page, { on: events.on, messages });
    events.emit(event("run.finished", { run_id: "r1", status: "completed", origin: "x" }, "s1"));
    page = { slug: "cad", sessionId: "s2" };
    release([reply(4, "late", "r1")]);
    await flush();
    expect(sent).not.toContain("agent.message");
  });

  it("is spoken through the frame's own window only", () => {
    const posted: unknown[] = [];
    const other: unknown[] = [];
    const frame = { contentWindow: { postMessage: (m: unknown) => posted.push(m) }, addEventListener: () => undefined, removeEventListener: () => undefined } as unknown as HTMLIFrameElement;
    const otherFrame = { contentWindow: { postMessage: (m: unknown) => other.push(m) }, addEventListener: () => undefined, removeEventListener: () => undefined } as unknown as HTMLIFrameElement;
    const deps: BridgeDeps = { theme: () => ({ tokens: {}, scheme: "dark", theme: "", lang: "en", phone: false }), toast: () => undefined, navigate: () => undefined, openFile: () => undefined };
    const one = serveBridge(frame, deps);
    const two = serveBridge(otherFrame, deps);
    const events = stream();
    forwardToPage(one.send, () => ({ slug: "cad", sessionId: null }), { on: events.on, messages: async () => [] });
    forwardToPage(two.send, () => ({ slug: "orders", sessionId: null }), { on: events.on, messages: async () => [] });
    events.emit(event("pages.emit", { slug: "cad", event: "scene.updated", data: 1 }));
    expect(posted).toEqual([{ daedalus: PROTOCOL, event: "scene.updated", data: 1 }]);
    expect(other).toEqual([]);
    one.stop();
    two.stop();
  });
});

describe("the bridge's agent calls", () => {
  it("opens the agent beside the page when there is one", () => {
    const base: BridgeDeps = { theme: () => ({ tokens: {}, scheme: "dark", theme: "", lang: "en", phone: false }), toast: () => undefined, navigate: () => undefined, openFile: () => undefined };
    expect(answer({ daedalus: PROTOCOL, id: 1, method: "agent.open" }, { ...base, openAgent: () => true })).toEqual({ result: true });
    expect(answer({ daedalus: PROTOCOL, id: 2, method: "agent.open" }, { ...base, openAgent: () => false }).error).toMatch(/no agent/);
    expect(answer({ daedalus: PROTOCOL, id: 3, method: "agent.open" }, base).error).toMatch(/no agent/);
  });
});

type Listener = (e: { data: unknown; source: unknown; origin: string }) => void;

function loadKit(parent: { postMessage: (m: unknown, origin: string) => void } | null) {
  const listeners: Listener[] = [];
  const page: Record<string, unknown> = {
    location: { origin: "https://daedalus.example", pathname: "/pages/cad/", href: "" },
    addEventListener: (type: string, fn: Listener) => {
      if (type === "message") listeners.push(fn);
    },
  };
  page.parent = parent ?? page;
  new Function("window", "document", kitSource)(page, document);
  const deliver = (data: unknown, source: unknown = parent) => listeners.forEach((fn) => fn({ data, source, origin: "https://daedalus.example" }));
  return { page, daedalus: page.Daedalus as any, deliver };
}

afterEach(() => vi.unstubAllGlobals());

describe("the page's side of its agent", () => {
  it("asks the agent with the page's own address, files and the silent mark included", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ run_id: "r1", session_id: "s1" }), { headers: { "Content-Type": "application/json" } });
    });
    const { daedalus } = loadKit(null);
    expect(daedalus.version).toBe("1.1.0");
    await expect(daedalus.agent.ask("Optimise this mesh", { attachments: [new File(["solid"], "part.stl")], silent: true })).resolves.toEqual({ run_id: "r1", session_id: "s1" });
    expect(calls[0].url).toBe("/api/pages/cad/ask");
    expect(calls[0].init.method).toBe("POST");
    const form = calls[0].init.body as FormData;
    expect(form.get("text")).toBe("Optimise this mesh");
    expect(form.get("silent")).toBe("1");
    expect(form.get("follow_up")).toBeNull();
    expect((form.get("files") as File).name).toBe("part.stl");
    // No content type of its own: the browser writes the form's boundary.
    expect(new Headers(calls[0].init.headers).has("Content-Type")).toBe(false);
  });

  it("hears the agent's replies and runs as the app forwards them", () => {
    const sent: any[] = [];
    const { daedalus, deliver } = loadKit({ postMessage: (m) => sent.push(m) });
    const replies: unknown[] = [];
    const runs: unknown[] = [];
    const off = daedalus.agent.on("message", (data: unknown) => replies.push(data));
    daedalus.agent.on("run", (data: unknown) => runs.push(data));
    deliver({ daedalus: PROTOCOL, event: "agent.message", data: { text: "Optimised." } });
    deliver({ daedalus: PROTOCOL, event: "agent.run", data: { status: "running" } });
    deliver({ daedalus: PROTOCOL, event: "agent.message", data: { text: "from a stranger" } }, {});
    off();
    deliver({ daedalus: PROTOCOL, event: "agent.message", data: { text: "after" } });
    expect(replies).toEqual([{ text: "Optimised." }]);
    expect(runs).toEqual([{ status: "running" }]);
    expect(() => daedalus.agent.on("thoughts", () => undefined)).toThrow(/message, run or event/);
    daedalus.agent.open();
    expect(sent.find((m) => m.method === "agent.open")).toMatchObject({ daedalus: PROTOCOL, params: {} });
  });
});

describe("a page's message in the chat", () => {
  it("is the operator's bubble when they asked, a note when the page reported", () => {
    expect(systemNote({ role: "user", text: "Optimise the mesh", origin: "page:cad", silent: false })).toBeNull();
    expect(systemNote({ role: "user", text: "Selected part 12", origin: "page:cad", silent: true })).toEqual({ kind: "page", origin: "cad", body: "Selected part 12" });
    // The operator's answers to side questions, carried into the session as one message, fold into a line.
    expect(systemNote({ role: "user", text: "Answers to your side questions", origin: "side_questions" })).toEqual({ kind: "answers", origin: "side_questions", body: "Answers to your side questions" });
  });
});
