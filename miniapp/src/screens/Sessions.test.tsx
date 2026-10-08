// @vitest-environment jsdom
import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectFolder, SessionSummary } from "../api";
import { arrange } from "../grouping";
import { setLang } from "../i18n";
import { FolderSection, SessionsScreen } from "./Sessions";

const listing = vi.hoisted(() => ({ data: { sessions: [] as SessionSummary[], projects: [] as ProjectFolder[] } }));
vi.mock("../store", () => ({ useQuery: () => ({ data: listing.data, loading: false }) }));
const environments = vi.hoisted(() => ({ docker: true }));
vi.mock("../projects", () => ({ ProjectChip: () => null, useProjects: () => ({ data: [] }), useEnvironments: () => ({ data: { local: environments.docker ? "container" : "host", docker: environments.docker, host_bridge: true, available: [] } }), ProjectSettingsSheet: () => <div data-settings /> }));
vi.mock("../shell", () => ({ PageHeader: ({ children }: { children: React.ReactNode }) => <header>{children}</header>, screenTitle: () => "Agents" }));

const project: ProjectFolder = { id: "p", name: "Garden", created_at: "2026-01-01T00:00:00Z", settings: { snapshots: false }, folders: [{ id: "f-p", path: "/projects/garden", label: "", env: "container", is_git: false, readonly: false, position: 0, managed: false, reachable: true, writable: true }], total: 1, members: 1, active: 0, loops: 0, last_message_at: "2026-09-18T00:00:00Z" };
const agent: SessionSummary = { id: "a", title: "Plan the planting", project_id: "p", project: "Garden", model: "Local model", status: "waiting", created_at: project.created_at, last_message_at: project.last_message_at, run_id: null };
let host: HTMLDivElement;
let root: Root;
const onOpen = vi.fn();
const toast = vi.fn();

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  setLang("en");
  listing.data = { sessions: [agent], projects: [project] };
  environments.docker = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  onOpen.mockClear();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });

async function folder(sessions = [agent], p = project, compact = false) {
  const arranged = arrange(sessions, [p]);
  await act(async () => root.render(<FolderSection folder={arranged.folders[0]} onOpen={onOpen} toast={toast} filtered={false} compact={compact} />));
}
async function click(selector: string) { await act(async () => (host.querySelector(selector) as HTMLElement).click()); }

describe("project conversations", () => {
  it("marks a session that runs on the host from the container, in the sidebar and the list, and nothing natively", async () => {
    const onHost = { ...agent, env: "host" as const };
    for (const compact of [false, true]) {
      await folder([onHost, { ...agent, id: "b", title: "Water", env: "container" }], { ...project, total: 2, members: 2 }, compact);
      if (!host.querySelector(".erow")) await click(".folder-head");
      const marks = Array.from(host.querySelectorAll(".erow")).map((row) => !!row.querySelector(".host-mark .term-env.host"));
      expect(marks.sort()).toEqual([false, true]);
    }
    environments.docker = false;
    await folder([onHost, { ...agent, id: "b", title: "Water" }], { ...project, total: 2, members: 2 }, false);
    expect(host.querySelectorAll(".erow")).toHaveLength(2);
    expect(host.querySelector(".host-mark")).toBeNull();
  });
  it("opens a single agent directly, with model, status, activity and project controls", async () => {
    await folder();
    expect(host.querySelector(".folder.single")).not.toBeNull();
    expect(host.textContent).toContain("Garden");
    expect(host.textContent).toContain("Local model");
    expect(host.textContent).toContain("Needs you");
    expect(host.querySelector(".erow-time")?.getAttribute("title")).toBeTruthy();
    await click('[role="link"]');
    expect(onOpen).toHaveBeenCalledWith("a");
    await click(".folder-expand");
    expect(host.textContent).toContain("/projects/garden");
    expect(host.querySelectorAll(".erow")).toHaveLength(1);
    await click(".session-row-menu button");
    await act(async () => Array.from(document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')).find((button) => button.textContent?.includes("Garden"))!.click());
    expect(host.querySelector("[data-settings]")).not.toBeNull();
  });
  it("separates a sidebar project header from its only session and its actions", async () => {
    await folder([agent], project, true);
    expect(host.querySelector(".folder.single")).toBeNull();
    expect(host.querySelector(".folder-head")?.textContent).toContain("Garden");
    await click(".folder-head");
    expect(host.querySelector(".erow-title")?.textContent).toBe("Plan the planting");
    expect(host.querySelector(".erow")?.getAttribute("title")).toContain("Local model");
    await click(".session-row-menu button");
    expect(onOpen).not.toHaveBeenCalled();
  });
  it.each([false, true])("keeps a single Voice agent behind its mode link and disclosure (compact: %s)", async (compact) => {
    await folder([agent], { ...project, name: "Voice", system: "voice" }, compact);
    expect(host.querySelector(".folder.single")).toBeNull();
    expect(host.querySelector(".folder-go")?.getAttribute("href")).toBe("/app/voice");
    expect(host.querySelector(".folder-go")?.textContent).toContain("Voice");
    expect(host.querySelector(".folder-disclose")?.getAttribute("aria-label")).toContain("Voice");
    expect(host.querySelector(".folder-disclose")?.getAttribute("aria-expanded")).toBe("false");
    expect(host.querySelectorAll(".erow")).toHaveLength(0);
    await click(".folder-disclose");
    expect(host.querySelector(".folder-disclose")?.getAttribute("aria-expanded")).toBe("true");
    expect(host.querySelectorAll(".erow")).toHaveLength(1);
    await click(".erow");
    expect(onOpen).toHaveBeenCalledWith("a");
    await click(".folder-disclose");
    expect(host.querySelectorAll(".erow")).toHaveLength(0);
    await click(".folder-actions");
    expect(host.querySelector("[data-settings]")).not.toBeNull();
  });
  it("becomes an open folder in the same section when the second agent appears", async () => {
    await folder();
    const section = host.querySelector("section");
    (host.querySelector(".erow") as HTMLElement).focus();
    await folder([agent, { ...agent, id: "b", title: "Watering", last_message_at: "2026-09-19T00:00:00Z" }], { ...project, total: 2, members: 2 });
    expect(host.querySelector("section")).toBe(section);
    expect(host.querySelector(".folder.single")).toBeNull();
    expect(host.querySelectorAll(".erow")).toHaveLength(2);
    expect((document.activeElement as HTMLElement).dataset.session).toBe("a");
    expect(host.querySelector(".erow-title")?.textContent).toBe("Watering");
  });
  it("offers project actions and creation in an empty project", async () => {
    await folder([], { ...project, total: 0, members: 0 });
    await click(".folder-head");
    expect(host.querySelector(".folder-empty")).not.toBeNull();
    expect(host.querySelector(".folder-add")).not.toBeNull();
    expect(host.querySelector(".folder-actions")).not.toBeNull();
  });
  it("does not turn one search hit from a larger project into a single-agent project", async () => {
    const found = arrange([{ ...agent, match: { snippet: "The best passage", score: 1 } }], [{ ...project, total: 2, members: 2 }], { results: true });
    await act(async () => root.render(<FolderSection folder={found.folders[0]} onOpen={onOpen} toast={toast} filtered />));
    expect(host.querySelector(".folder.single")).toBeNull();
    expect(host.querySelector(".search-passage")?.textContent).toBe("The best passage");
  });
  it("replaces the old chips with a real debounced search and shows exact-only fallback", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ sessions: [{ ...agent, match: { snippet: "Grow tomatoes on the balcony", score: 1 } }], projects: [project], semantic: false, reason: "off", partial: false, indexing: false })));
    await act(async () => root.render(<SessionsScreen onOpen={onOpen} toast={toast} />));
    expect(host.querySelectorAll(".agent-filters .chip")).toHaveLength(4);
    const input = host.querySelector('input[type="search"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "vegetables outside");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/api/sessions/search?q=vegetables%20outside"), expect.anything());
    expect(host.textContent).toContain("Exact search only");
    expect(host.textContent).toContain("Grow tomatoes on the balcony");
  });
});
