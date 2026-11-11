// @vitest-environment jsdom
// Files and tasks over an agent link: how a task's state reads, how a picture differs from any other file, and
// what the operator's ticks send when files wait to go.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentTalk, AgentTalkMessage, HeldGroup, LinkFile, LinkTask } from "./api";
import { setLang, t } from "./i18n";

const world = vi.hoisted(() => ({ posted: [] as [string, unknown][], fetched: [] as string[] }));
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  api: {
    get: async () => ({}),
    post: async (path: string, body?: unknown) => {
      world.posted.push([path, body]);
      return {};
    },
    fetch: async (path: string) => {
      world.fetched.push(path);
      return new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" }));
    },
  },
}));

import { TalkApproval, decision, filesOf } from "./agenttalk";
import { doorFiles } from "./guestlinks";
import { GuestHeldApproval, LinkFileRow, TaskCard, cancellable, fileWord, kindWord, shortHash, taskPhase } from "./linkfiles";

const SHA = "3f2a9c0d1e4b5a6978877665544332211000ffeeddccbbaa9988776655443322";
const file = (over: Partial<LinkFile> = {}): LinkFile => ({
  id: 1, direction: "in", name: "report.pdf", size: 2048, sha256: SHA, mime: "application/pdf", state: "received", task: "", note: "", at: "", image: false, downloadable: true,
  message_id: 2, batch: "", remote_id: "", ...over,
});
const task = (over: Partial<LinkTask> = {}): LinkTask => ({ task: "k1", direction: "mine", text: "Find the menu", state: "working", result: "", created_at: "", updated_at: "", ...over });
const TALK = {
  id: "t1", session_id: "s1", host: "agents.example.test", topic: "", mode: "direct", state: "open", reason: "", waiting_for: "", quiet: false, review: "first", sends: 1, sends_limit: 30,
  wake_turns: 0, wake_usd: 0, created_at: "", last_at: "", closed_at: null, spent_usd: 0, replies: 0, unread: 0, held: 1, file_mb: 50, day_mb: 500, tasks: [],
} satisfies AgentTalk;
const HELD: AgentTalkMessage = { id: 3, role: "us", text: "Here are the two files.", state: "held", note: "first", at: "", kind: "file", task: "", files: [5, 6] };

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  world.posted = [];
  world.fetched = [];
  URL.createObjectURL = vi.fn(() => "blob:thumb");
  URL.revokeObjectURL = vi.fn();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

async function draw(node: React.ReactNode) {
  await act(async () => root.render(node));
  await act(async () => {});
}
const press = async (selector: string) => act(async () => host.querySelector<HTMLElement>(selector)!.click());

describe("a task", () => {
  it("names every state and gives each the tone it is drawn in", () => {
    const phases = (["accepted", "working", "needs_input", "done", "refused", "cancelled"] as const).map(taskPhase);
    expect(phases).toEqual(["standby", "working", "question", "done", "failed", "stopped"]);
    expect(t("link.task.state.needs_input")).toBe("Needs input");
  });

  it("can be cancelled only by the side that waits on it, and only while it is open", () => {
    expect(cancellable(task({ state: "working" }), "mine")).toBe(true);
    expect(cancellable(task({ state: "needs_input", direction: "theirs" }), "theirs")).toBe(true);
    expect(cancellable(task({ state: "working" }), "theirs")).toBe(false);
    expect(cancellable(task({ state: "done" }), "mine")).toBe(false);
    expect(cancellable(task({ state: "cancelled" }), "mine")).toBe(false);
  });

  it("shows the result of a finished task and no Cancel on it", async () => {
    await draw(<TaskCard task={task({ state: "done", result: "Opens at eight." })} side="mine" onCancel={() => {}} />);
    expect(host.querySelector("[data-link-task-state]")?.textContent).toBe("Done");
    expect(host.querySelector("[data-link-task-result]")?.textContent).toContain("Opens at eight.");
    expect(host.querySelector("[data-link-task-cancel]")).toBeNull();
  });

  it("offers Cancel on an open one", async () => {
    const cancelled: string[] = [];
    await draw(<TaskCard task={task()} side="mine" onCancel={(one) => cancelled.push(one.task)} />);
    expect(host.querySelector("[data-link-task-state]")?.textContent).toBe("Working");
    await press("[data-link-task-cancel]");
    expect(cancelled).toEqual(["k1"]);
  });
});

describe("a message's kind", () => {
  it("is a label for anything but words, and a status says the state it reports", () => {
    expect(kindWord("text")).toBe("");
    expect(kindWord(undefined)).toBe("");
    expect(kindWord("task")).toBe("Task");
    expect(kindWord("status", "done")).toBe("Status · Done");
    expect(kindWord("status", "bogus")).toBe("Status");
  });
});

describe("a file", () => {
  it("draws a picture from bytes fetched with the app's credential", async () => {
    await draw(<LinkFileRow file={file({ id: 7, name: "shot.png", mime: "image/png", image: true })} toast={() => {}} />);
    expect(world.fetched).toEqual(["/api/link-files/7"]);
    expect(host.querySelector<HTMLImageElement>("[data-link-file-thumb] img")?.getAttribute("src")).toBe("blob:thumb");
  });

  it("is a name, a size, the start of its hash and Download when it is not a picture", async () => {
    await draw(<LinkFileRow file={file()} toast={() => {}} />);
    expect(world.fetched).toEqual([]);
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector(".link-file-name")?.textContent).toBe("report.pdf");
    expect(host.textContent).toContain("2 KB");
    const hash = host.querySelector("[data-link-file-hash]")!;
    expect(hash.textContent).toBe(shortHash(SHA));
    expect(hash.getAttribute("title")).toBe(SHA);
    expect(host.querySelector("[data-link-file-save]")).not.toBeNull();
  });

  it("says its state only where it is worth saying", async () => {
    expect(fileWord("received")).toBe("");
    expect(fileWord("sent")).toBe("");
    expect(fileWord("dropped")).toBe("Held back");
    await draw(<LinkFileRow file={file({ state: "pending", downloadable: false })} toast={() => {}} />);
    expect(host.querySelector("[data-link-file-word='pending']")?.textContent).toBe("Not here yet");
    expect(host.querySelector("[data-link-file-save]")).toBeNull();
  });

  it("is found by the ids a message names, on either side", () => {
    const files = [file({ id: 5, remote_id: "f-a" }), file({ id: 6, remote_id: "f-b" }), file({ id: 9, remote_id: "" })];
    expect(filesOf({ files: [6] }, files).map((one) => one.id)).toEqual([6]);
    expect(doorFiles({ files: ["f-a", "f-missing"] }, files).map((one) => one.id)).toEqual([5]);
    expect(doorFiles({}, files)).toEqual([]);
  });
});

describe("files that wait to go with a message", () => {
  const files = [file({ id: 5, name: "a.png", image: true, mime: "image/png", state: "held", direction: "out" }), file({ id: 6, name: "b.pdf", state: "held", direction: "out" })];

  it("sends nothing extra when everything goes, and the unticked ids when some stay", () => {
    expect(decision(undefined, new Set())).toBeUndefined();
    expect(decision(undefined, new Set([6, 5]))).toEqual({ drop: [5, 6] });
    expect(decision("New words", new Set([6]))).toEqual({ text: "New words", drop: [6] });
  });

  it("posts the unticked file as dropped", async () => {
    await draw(<TalkApproval talk={TALK} message={HELD} files={files} toast={() => {}} />);
    const boxes = host.querySelectorAll<HTMLInputElement>("[data-link-picks] input[type='checkbox']");
    expect(boxes).toHaveLength(2);
    expect([...boxes].every((box) => box.checked)).toBe(true);
    await act(async () => boxes[1].click());
    await press("[data-action='send']");
    expect(world.posted).toEqual([["/api/agent-talks/t1/messages/3/send", { drop: [6] }]]);
  });

  it("waits with every file unticked and no words, and Don't send still answers", async () => {
    await draw(<TalkApproval talk={TALK} message={{ ...HELD, text: "" }} files={files} toast={() => {}} />);
    for (const box of host.querySelectorAll<HTMLInputElement>("[data-link-picks] input")) await act(async () => box.click());
    expect(host.querySelector<HTMLButtonElement>("[data-action='send']")!.disabled).toBe(true);
    expect(host.querySelector("[data-talk-nothing]")?.textContent).toBe(t("link.held.none"));
    await press("[data-action='refuse']");
    expect(world.posted).toEqual([["/api/agent-talks/t1/messages/3/refuse", undefined]]);
  });
});

describe("files a guest's turn wants to send", () => {
  const group: HeldGroup = {
    batch: "b1", link_id: "1a2b", label: "Mira's assistant", task: "", note: "The menu you asked for.",
    files: [file({ id: 11, name: "menu.pdf", state: "held", direction: "out" }), file({ id: 12, name: "cake.png", image: true, mime: "image/png", state: "held", direction: "out" })],
  };

  it("names the link, and posts what was unticked to the link's outbox", async () => {
    let done = 0;
    await draw(<GuestHeldApproval group={group} toast={() => {}} onDone={() => (done += 1)} />);
    expect(host.querySelector("[data-guest-held='b1']")?.getAttribute("aria-label")).toBe("Files for Mira's assistant");
    expect(host.textContent).toContain("The menu you asked for.");
    await act(async () => host.querySelectorAll<HTMLInputElement>("[data-link-picks] input")[0].click());
    await press("[data-action='send']");
    expect(world.posted).toEqual([["/api/guest/links/1a2b/outbox/b1/send", { drop: [11] }]]);
    expect(done).toBe(1);
  });

  it("refuses them all with Don't send, and will not send an empty set", async () => {
    await draw(<GuestHeldApproval group={group} toast={() => {}} onDone={() => {}} />);
    for (const box of host.querySelectorAll<HTMLInputElement>("[data-link-picks] input")) await act(async () => box.click());
    expect(host.querySelector<HTMLButtonElement>("[data-action='send']")!.disabled).toBe(true);
    await press("[data-action='refuse']");
    expect(world.posted).toEqual([["/api/guest/links/1a2b/outbox/b1/refuse", undefined]]);
  });
});
