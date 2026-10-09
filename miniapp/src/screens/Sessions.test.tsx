// @vitest-environment jsdom
// The desktop's sidebar: the projects and the chats apart by the project rule, a collapsed project
// that still shows what asks for the operator, the two buttons under "New chat", and the keyboard.
import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectFolder, SessionSummary } from "../api";
import { setLang } from "../i18n";
import { SessionsScreen } from "./Sessions";

const listing = vi.hoisted(() => ({ data: { sessions: [] as SessionSummary[], projects: [] as ProjectFolder[] } }));
vi.mock("../store", () => ({ useQuery: () => ({ data: listing.data, loading: false }), invalidate: () => undefined }));
const environments = vi.hoisted(() => ({ docker: true }));
vi.mock("../projects", () => ({
  useProjects: () => ({ data: [] }),
  useEnvironments: () => ({ data: { local: environments.docker ? "container" : "host", docker: environments.docker, host_bridge: true, available: [] } }),
  ProjectSettingsSheet: () => <div data-settings />,
  MoveSessionSheet: () => <div data-move />,
  AddProjectSheet: () => <div data-add-project />,
}));
vi.mock("../events", () => ({ useStreamUp: () => true }));
const dialogs = vi.hoisted(() => ({ confirm: vi.fn(async () => false) }));
vi.mock("../ui/dialogs", async (original) => ({ ...(await original<typeof import("../ui/dialogs")>()), confirmDialog: dialogs.confirm }));

const NOW = Date.now();
const ago = (hours: number) => new Date(NOW - hours * 3_600_000).toISOString();
function project(id: string, name: string, extra: Partial<ProjectFolder> = {}): ProjectFolder {
  return { id, name, created_at: "2026-01-01T00:00:00Z", settings: { snapshots: false }, folders: [{ id: `f-${id}`, path: `/projects/${id}`, label: "", env: "container", is_git: false, readonly: false, position: 0, managed: false, reachable: true, writable: true }], total: 1, members: 1, active: 0, loops: 0, last_message_at: ago(1), ...extra };
}
function chat(id: string, projectId: string, hours: number, extra: Partial<SessionSummary> = {}): SessionSummary {
  return { id, title: `Chat ${id}`, project_id: projectId, project: projectId, model: "Local model", status: "idle", created_at: ago(hours + 1), last_message_at: ago(hours), run_id: null, ...extra };
}

let host: HTMLDivElement;
let root: Root;
const onOpen = vi.fn();
const toast = vi.fn();
const onProjects = vi.fn();

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  setLang("en");
  environments.docker = true;
  dialogs.confirm.mockClear();
  listing.data = {
    projects: [
      project("home", "Smart home", { total: 2, members: 2, last_message_at: ago(0.2) }),
      project("esp", "Firmware", { last_message_at: ago(40) }),
      project("voice", "Voice", { system: "voice", settings: { snapshots: false, system: "voice" } }),
      project("c1", "Bank statement", { settings: { snapshots: true, ephemeral: true } }),
      project("c2", "Docker build", { settings: { snapshots: true, ephemeral: true }, last_message_at: ago(30) }),
    ],
    sessions: [
      chat("h1", "home", 0.2, { status: "waiting", title: "Smart lock" }),
      chat("h2", "home", 5, { title: "Zigbee gateway" }),
      chat("e1", "esp", 40, { title: "Door sensor" }),
      chat("c1", "c1", 0.5, { title: "Bank statement", unread_result: true }),
      chat("c2", "c2", 30, { title: "Docker build", env: "host" }),
    ],
  };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  onOpen.mockClear();
  onProjects.mockClear();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });

async function render(current?: string) {
  await act(async () => root.render(<SessionsScreen onOpen={onOpen} toast={toast} current={current} onProjects={onProjects} />));
}
const projectRow = (id: string) => host.querySelector<HTMLElement>(`[data-project="${id}"] .sb-prow`)!;
const rowsOf = (id: string) => Array.from(host.querySelectorAll<HTMLElement>(`[data-project="${id}"] [data-session]`)).map((r) => r.dataset.session);
const key = (target: Element, init: KeyboardEventInit) => act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init })); });

describe("the sidebar", () => {
  it("lists projects and chats apart by the project rule, and the chats by day", async () => {
    await render();
    const sections = Array.from(host.querySelectorAll(".sb-sec")).map((s) => s.textContent);
    expect(sections).toEqual(["Projects3", "Chats2"]);
    // A project made by hand is a project with one chat in it; Voice is one whatever it holds.
    expect(Array.from(host.querySelectorAll("[data-project]")).map((s) => (s as HTMLElement).dataset.project).sort()).toEqual(["esp", "home", "voice"]);
    expect(Array.from(host.querySelectorAll(".sb-day")).map((d) => [(d as HTMLElement).dataset.day, Array.from(d.querySelectorAll("[data-session]")).map((r) => (r as HTMLElement).dataset.session)]))
      .toEqual([["today", ["c1"]], ["yesterday", ["c2"]]]);
  });

  it("starts projects collapsed, keeps a waiting chat in sight under one, and opens on a click", async () => {
    await render();
    expect(projectRow("home").getAttribute("aria-expanded")).toBe("false");
    expect(rowsOf("home")).toEqual(["h1"]);
    expect(projectRow("home").querySelector(".sb-st.waiting")).not.toBeNull();
    expect(rowsOf("esp")).toEqual([]);
    await act(async () => projectRow("home").click());
    expect(projectRow("home").getAttribute("aria-expanded")).toBe("true");
    expect(rowsOf("home")).toEqual(["h1", "h2"]);
    expect(localStorage.getItem("daedalus.folder.home")).toBe("1");
  });

  it("opens the project of the open chat without remembering it", async () => {
    await render("e1");
    expect(projectRow("esp").getAttribute("aria-expanded")).toBe("true");
    expect(host.querySelector('[data-session="e1"]')?.classList.contains("current")).toBe(true);
    expect(localStorage.getItem("daedalus.folder.esp")).toBeNull();
  });

  it("draws a chat in two lines: the state in words when it asks, the host, the model and the time", async () => {
    await render();
    const waiting = host.querySelector('[data-session="h1"]')!;
    expect(waiting.querySelector(".sb-word.waiting")?.textContent).toBe("Needs you");
    expect(waiting.querySelector(".sb-st.waiting")).not.toBeNull();
    const onHost = host.querySelector('[data-session="c2"]')!;
    expect(onHost.querySelector(".host-mark .term-env.host")).not.toBeNull();
    expect(onHost.querySelector(".sb-word")).toBeNull();
    expect(onHost.querySelector(".sb-ell")?.textContent).toContain("Local model");
    expect(onHost.querySelector(".sb-time")?.getAttribute("title")).toBeTruthy();
    const unread = host.querySelector('[data-session="c1"]')!;
    expect(unread.classList.contains("unread")).toBe(true);
    expect(unread.querySelector(".unread-dot")).not.toBeNull();
    // Natively every chat runs on the host, so nothing is marked. A fresh tree: a memoised row
    // hears of the environment from its query, which this fake does not publish.
    environments.docker = false;
    await act(async () => root.unmount());
    root = createRoot(host);
    await render();
    expect(host.querySelector(".host-mark")).toBeNull();
  });

  it("offers All projects and New project side by side under New chat", async () => {
    await render();
    const buttons = Array.from(host.querySelectorAll(".sb-pbtns button")).map((b) => b.textContent);
    expect(buttons).toEqual(["All projects", "New project"]);
    expect(host.querySelector(".sb-new")?.textContent).toContain("New chat");
    await act(async () => host.querySelector<HTMLButtonElement>(".sb-pbtns .project-chip")!.click());
    expect(onProjects).toHaveBeenCalled();
    await act(async () => host.querySelector<HTMLButtonElement>(".sb-newproject")!.click());
    expect(host.querySelector("[data-add-project]")).not.toBeNull();
  });

  it("is driven from the keyboard: slash, arrows, F2 and Delete", async () => {
    await render();
    await key(document.body, { key: "/" });
    expect(document.activeElement).toBe(host.querySelector(".sb-search input"));
    await key(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(projectRow("home"));
    await key(projectRow("home"), { key: "ArrowRight" });
    expect(projectRow("home").getAttribute("aria-expanded")).toBe("true");
    await key(projectRow("home"), { key: "ArrowRight" });
    expect((document.activeElement as HTMLElement).dataset.session).toBe("h1");
    await key(document.activeElement!, { key: "ArrowDown" });
    expect((document.activeElement as HTMLElement).dataset.session).toBe("h2");
    await key(document.activeElement!, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(projectRow("home"));
    await key(projectRow("home"), { key: "ArrowLeft" });
    expect(projectRow("home").getAttribute("aria-expanded")).toBe("false");
    const row = host.querySelector<HTMLElement>('[data-session="c1"]')!;
    row.focus();
    await key(row, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledWith("c1");
    await key(row, { key: "Delete" });
    expect(dialogs.confirm).toHaveBeenCalled();
    await key(row, { key: "F2" });
    expect(document.querySelector<HTMLInputElement>('input[aria-label="Rename"]')?.value).toBe("Bank statement");
  });

  it("puts the phone sheet's commands in the row menu, Delete last", async () => {
    await render();
    await act(async () => host.querySelector<HTMLButtonElement>('[data-session="c1"] .sb-acts button[aria-haspopup="menu"]')!.click());
    const words = Array.from(document.querySelectorAll('[role="menuitem"]')).map((b) => b.textContent ?? "");
    expect(words).toEqual(expect.arrayContaining(["Open beside", "Rename", "Move…", "Archive", "Delete"]));
    expect(words[words.length - 1]).toBe("Delete");
  });

  it("searches after a pause and says when the search is exact only", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ sessions: [{ ...chat("c1", "c1", 1), match: { snippet: "Grow tomatoes on the balcony", score: 1 } }], projects: listing.data.projects, semantic: false, reason: "off", partial: false, indexing: false })));
    await render();
    expect(Array.from(host.querySelectorAll(".sb-chips .sb-chip")).map((c) => c.textContent)).toEqual(["All5", "Waiting1", "Working0", ""]);
    const input = host.querySelector('input[type="search"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "vegetables outside");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/api/sessions/search?q=vegetables%20outside"), expect.anything());
    expect(host.textContent).toContain("Exact search only");
    expect(host.querySelector(".search-passage")?.textContent).toBe("Grow tomatoes on the balcony");
    expect(host.querySelector(".sb-chips")).toBeNull();
  });
});
