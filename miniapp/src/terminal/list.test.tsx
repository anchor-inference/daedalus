// @vitest-environment jsdom
// The list column of the Terminals page: what a filter chip counts and shows, how the search narrows,
// that a group's end button is there only for a group that runs something, and that choosing several
// puts a checkbox on the rows that can be ended and takes the press off opening.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TerminalView } from "../api";
import { setLang } from "../i18n";
import { ListColumn } from "./list";
import { EMPTY } from "./workspacestate";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

const NOW = Date.now();
const row = (id: string, fields: Partial<TerminalView> = {}): TerminalView => ({
  id, env: "container", title: id, owner: { kind: "free", id: null }, project_id: null, profile: "shell", sandbox: false, cwd: "/work", status: "running",
  exit_code: null, exit_signal: null, created_at: new Date(NOW - 5 * 60_000).toISOString(), exited_at: null, last_output_at: null, last_input_at: null, cols: 80, rows: 24,
  live: { clients: 1, busy: false, keyboard: { owner: "auto", until: null }, size_owner: "human", alt_screen: false }, ...fields,
});

const rows = [
  row("tests", { project_id: "p1", owner: { kind: "session", id: "s", label: "Checkout" }, last_command: { command: "npm test", exit_code: 0, at: new Date(NOW).toISOString() } }),
  row("naya", { env: "host", project_id: "p1", owner: { kind: "staff", id: "st", label: "Naya" } }),
  row("htop", { env: "host" }),
  row("psql", { project_id: "p1", status: "exited", exit_code: 0, exited_at: new Date(NOW - 20 * 60_000).toISOString() }),
];

const show = (extra: Partial<Parameters<typeof ListColumn>[0]> = {}) =>
  act(async () => root.render(<ListColumn rows={rows} states={{}} ws={EMPTY} projects={[{ id: "p1", name: "Bakery" } as never]} load={null} onOpen={() => {}} toast={() => {}} {...extra} />));

const ids = () => [...host.querySelectorAll<HTMLElement>(".tp-row")].map((r) => r.dataset.terminal);
const press = (selector: string) => act(async () => { (host.querySelector(selector) as HTMLElement).click(); });

describe("the list column", () => {
  it("groups by project, then the terminals of none, and says each row's owner and where it runs", async () => {
    await show();
    expect([...host.querySelectorAll(".tp-label-name")].map((e) => e.textContent)).toEqual(["Bakery", "No project"]);
    expect(host.querySelector("[data-terminal=tests] .tp-row-owner")!.textContent).toBe("chat «Checkout»");
    expect(host.querySelector("[data-terminal=naya] .tp-row-owner")!.textContent).toBe("staff Naya · host");
    expect(host.querySelector("[data-terminal=htop] .tp-row-owner")!.textContent).toBe("you · host");
    expect(host.querySelector("[data-terminal=tests] .tp-row-cmd")!.textContent).toBe("npm test");
  });

  it("counts each filter and narrows to it: the environment and staff filters show what runs, Finished what ended", async () => {
    await show();
    const count = (f: string) => host.querySelector(`[data-filter=${f}] .tp-filter-n`)!.textContent;
    expect(["all", "container", "host", "staff", "finished"].map(count)).toEqual(["4", "1", "2", "1", "1"]);
    await press("[data-filter=host]");
    expect(ids().sort()).toEqual(["htop", "naya"]);
    await press("[data-filter=staff]");
    expect(ids()).toEqual(["naya"]);
    await press("[data-filter=finished]");
    expect(ids()).toEqual(["psql"]);
    expect(host.querySelector("[data-filter=finished]")!.getAttribute("aria-pressed")).toBe("true");
  });

  it("offers End for a group only when something in it runs", async () => {
    await show({ rows: [rows[3], rows[2]] });
    expect(host.querySelector("[data-group=p1] [data-action=end-group]")).toBeNull();
    expect(host.querySelector("[data-group=none] [data-action=end-group]")).not.toBeNull();
  });

  it("leaves the empty message to the page when the host knows no terminal, and says so when a filter leaves none", async () => {
    await show({ rows: [] });
    expect(host.querySelector(".tp-list-none")).toBeNull();
    await show({ rows: [rows[2]] });
    await press("[data-filter=finished]");
    expect(host.querySelector(".tp-list-none")!.textContent).toBe("Nothing under this filter");
  });

  it("marks the active pane's terminal as the chosen row", async () => {
    await show({ ws: { ...EMPTY, panes: ["htop", null, null, null] } });
    expect(host.querySelector("[data-terminal=htop]")!.classList.contains("on")).toBe(true);
    expect(host.querySelector("[data-terminal=tests]")!.classList.contains("on")).toBe(false);
  });
});
