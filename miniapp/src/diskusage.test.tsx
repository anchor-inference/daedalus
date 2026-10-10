// @vitest-environment jsdom
// The panel asks for nothing until its section is opened, shows the folders, and a clean-up ticks the
// throwaway ones, needs a second consent for tracked ones, and reports what it freed and refused.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiskUsage } from "./diskusage";
import type { DiskDetail } from "./diskmodel";
import { setLang } from "./i18n";
import { prime } from "./store";
import { api } from "./api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GB = 1024 ** 3;
const detail: DiskDetail = {
  limit_bytes: 20 * GB, free_bytes: 40 * GB, total_bytes: 200 * GB, low_disk: false, outside: [],
  workspaces: [{
    name: "ws1", label: "Session home", bytes: 23 * GB, truncated: false, over: 1,
    entries: [
      { path: "_scratch", bytes: 15 * GB, files: 900, newest: null, throwaway: true, tracked: false, protected: false, children: [] },
      { path: "src", bytes: 2 * GB, files: 90, newest: null, throwaway: false, tracked: true, protected: false, children: [] },
      { path: ".checkpoints", bytes: GB, files: 4, newest: null, throwaway: false, tracked: false, protected: true, children: [] },
    ],
  }],
};

let host: HTMLDivElement;
let root: Root;
beforeEach(() => { setLang("en"); host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); document.body.innerHTML = ""; vi.restoreAllMocks(); });

function mount(open: boolean) {
  prime("/api/disk/session/s1", detail);
  act(() => root.render(<details open={open}><summary>x</summary><DiskUsage kind="session" id="s1" toast={() => {}} /></details>));
}
const click = (el: Element | null) => act(() => { (el as HTMLElement).click(); });

describe("DiskUsage", () => {
  it("shows nothing until the enclosing section is open", () => {
    mount(false);
    expect(host.querySelector("[data-disk-workspace]")).toBeNull();
  });

  it("shows the size, the limit and the folders once open", () => {
    mount(true);
    expect(host.querySelector("[data-disk-total]")?.textContent).toBe("23.0 GB");
    expect(host.textContent).toContain("of 20.0 GB");
    expect(host.querySelector('[data-disk-entry="_scratch"]')?.textContent).toContain("throwaway");
    expect(host.querySelector('[data-disk-entry="src"]')?.textContent).toContain("tracked");
  });

  it("cleans the preselected folder and reports what was freed and refused", async () => {
    mount(true);
    const post = vi.spyOn(api, "post").mockResolvedValue({ freed_bytes: 15 * GB, removed: ["_scratch"], refused: [{ path: "src", reason: "tracked" }], bytes: 8 * GB });
    click(host.querySelector(".disk-workspace > button"));
    const pick = (path: string) => document.body.querySelector<HTMLInputElement>(`[data-disk-pick="${path}"] input`)!;
    expect(pick("_scratch").checked).toBe(true);
    expect(pick("src").checked).toBe(false);
    expect(pick("src").disabled).toBe(true);
    expect(pick(".checkpoints").disabled).toBe(true);
    await act(async () => { click(document.body.querySelector("[data-disk-confirm]")); });
    expect(post).toHaveBeenCalledWith("/api/disk/cleanup", { workspace: "ws1", paths: ["_scratch"], include_tracked: false });
    expect(document.body.querySelector("[data-disk-freed]")?.textContent).toBe("Freed 15.0 GB");
    expect(document.body.querySelector('[data-disk-refused="src"]')?.textContent).toContain("tracked by git");
  });
});
