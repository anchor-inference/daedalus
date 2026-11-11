// @vitest-environment jsdom
// The panel asks for nothing until its section is opened, shows the folders, and a clean-up ticks the
// throwaway ones, needs a second consent for tracked ones, and reports what it freed and refused.
// The limit is changed from the panel itself: each choice is saved at once and the bar, the size
// against the limit and the "over" line redraw from what the host answered.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiskUsage } from "./diskusage";
import type { DiskDetail } from "./diskmodel";
import { setLang } from "./i18n";
import { prime } from "./store";
import { api } from "./api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GB = 1e9;
const detail: DiskDetail = {
  default_limit_bytes: 20 * GB, free_bytes: 40 * GB, total_bytes: 200 * GB, low_disk: false, outside: [],
  workspaces: [{
    name: "ws1", label: "Session home", bytes: 23 * GB, truncated: false, limit_bytes: 20 * GB, own_limit: false, over: 1,
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
    expect(host.querySelector("[data-disk-total]")?.textContent).toBe("23 GB");
    expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("of 20 GB");
    expect(host.querySelector("[data-disk-free]")?.textContent).toBe("40 GB free of 200 GB");
    expect(host.querySelector("[data-disk-over]")?.textContent).toBe("Over the size limit.");
    expect(host.querySelector('[data-disk-entry="_scratch"]')?.textContent).toContain("throwaway");
    expect(host.querySelector('[data-disk-entry="src"]')?.textContent).toContain("tracked");
  });

  describe("changing the limit", () => {
    const open = () => click(host.querySelector("[data-disk-limit-open]"));
    const choice = (id: string) => host.querySelector<HTMLButtonElement>(`[data-disk-limit-choice="${id}"]`)!;
    const answer = (limit_bytes: number, own_limit: boolean, over: 0 | 1 | 2) => ({ name: "ws1", limit_bytes, own_limit, default_limit_bytes: 20 * GB, over });

    it("opens under the bar with the quick choices, no limit and the default", () => {
      mount(true);
      expect(host.querySelector("[data-disk-limit-edit]")).toBeNull();
      open();
      expect(host.querySelector("[data-disk-limit-open]")?.getAttribute("aria-expanded")).toBe("true");
      expect([...host.querySelectorAll("[data-disk-limit-choice]")].map((el) => el.textContent)).toEqual(["+10 GB", "×2", "50 GB", "100 GB", "No limit", "Use default (20 GB)"]);
      expect(choice("default").getAttribute("aria-pressed")).toBe("true");
      expect(choice("none").getAttribute("aria-pressed")).toBe("false");
      expect(host.querySelector<HTMLInputElement>("[data-disk-limit-input]")?.value).toBe("20");
    });

    it("saves a quick choice at once and redraws the bar, the size against the limit and the over line", async () => {
      mount(true);
      const put = vi.spyOn(api, "put").mockResolvedValue(answer(30 * GB, true, 0));
      open();
      await act(async () => { click(choice("plus")); });
      expect(put).toHaveBeenCalledWith("/api/disk/limit/ws1", { limit_gb: 30 });
      expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("of 30 GB");
      expect(host.querySelector("[data-disk-over]")).toBeNull();
      expect(host.querySelector(".bar")?.className).toBe("bar ");
      expect(host.querySelector<HTMLElement>(".bar")?.style.getPropertyValue("--v")).toBe("77");
      expect(host.querySelector("[data-disk-source]")?.textContent).toBe("Limit set for this workspace");
      expect(host.querySelector("[data-disk-limit-edit]")).toBeNull();
    });

    it("doubles the limit in force and offers the choices again relative to the new one", async () => {
      mount(true);
      const put = vi.spyOn(api, "put").mockResolvedValue(answer(40 * GB, true, 0));
      open();
      await act(async () => { click(choice("double")); });
      expect(put).toHaveBeenCalledWith("/api/disk/limit/ws1", { limit_gb: 40 });
      open();
      expect([...host.querySelectorAll("[data-disk-limit-choice]")].map((el) => el.textContent)).toEqual(["+10 GB", "×2", "100 GB", "No limit", "Use default (20 GB)"]);
      expect(choice("default").getAttribute("aria-pressed")).toBe("false");
    });

    it("takes the limit off: no bar, no over line, and says so", async () => {
      mount(true);
      const put = vi.spyOn(api, "put").mockResolvedValue(answer(0, true, 0));
      open();
      await act(async () => { click(choice("none")); });
      expect(put).toHaveBeenCalledWith("/api/disk/limit/ws1", { limit_gb: 0 });
      expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("no limit");
      expect(host.querySelector(".bar")).toBeNull();
      expect(host.querySelector("[data-disk-over]")).toBeNull();
      open();
      // Nothing to add to: only the round numbers are left.
      expect([...host.querySelectorAll("[data-disk-limit-choice]")].map((el) => el.textContent)).toEqual(["50 GB", "100 GB", "No limit", "Use default (20 GB)"]);
      expect(choice("none").getAttribute("aria-pressed")).toBe("true");
      expect(host.querySelector<HTMLInputElement>("[data-disk-limit-input]")?.value).toBe("");
    });

    it("hands the workspace back to the default", async () => {
      prime("/api/disk/session/s1", { ...detail, workspaces: [{ ...detail.workspaces[0], limit_bytes: 50 * GB, own_limit: true, over: 0 }] });
      act(() => root.render(<details open><summary>x</summary><DiskUsage kind="session" id="s1" toast={() => {}} /></details>));
      const put = vi.spyOn(api, "put").mockResolvedValue(answer(20 * GB, false, 1));
      open();
      expect(choice("default").getAttribute("aria-pressed")).toBe("false");
      await act(async () => { click(choice("default")); });
      expect(put).toHaveBeenCalledWith("/api/disk/limit/ws1", { limit_gb: null });
      expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("of 20 GB");
      expect(host.querySelector("[data-disk-over]")?.textContent).toBe("Over the size limit.");
    });

    it("saves a typed number only when it is a positive one", async () => {
      mount(true);
      const put = vi.spyOn(api, "put").mockResolvedValue(answer(12.5 * GB, true, 1));
      open();
      const input = host.querySelector<HTMLInputElement>("[data-disk-limit-input]")!;
      const save = host.querySelector<HTMLButtonElement>("[data-disk-limit-set]")!;
      const type = (value: string) => act(() => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      for (const bad of ["", "0", "-3", "many"]) {
        type(bad);
        expect(save.disabled, bad).toBe(true);
      }
      type("12.5");
      expect(save.disabled).toBe(false);
      await act(async () => { click(save); });
      expect(put).toHaveBeenCalledWith("/api/disk/limit/ws1", { limit_gb: 12.5 });
      expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("of 12.5 GB");
    });

    it("says why a limit was refused and keeps what was shown", async () => {
      const toast = vi.fn();
      prime("/api/disk/session/s1", detail);
      act(() => root.render(<details open><summary>x</summary><DiskUsage kind="session" id="s1" toast={toast} /></details>));
      vi.spyOn(api, "put").mockRejectedValue(new Error("no such workspace"));
      open();
      await act(async () => { click(choice("none")); });
      expect(toast).toHaveBeenCalledTimes(1);
      expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("of 20 GB");
      expect(host.querySelector("[data-disk-limit-edit]")).not.toBeNull();
    });

    it("speaks Russian with the same units", () => {
      setLang("ru");
      mount(true);
      open();
      expect(host.querySelector("[data-disk-limit]")?.textContent).toBe("из 20 ГБ");
      expect([...host.querySelectorAll("[data-disk-limit-choice]")].map((el) => el.textContent)).toEqual(["+10 ГБ", "×2", "50 ГБ", "100 ГБ", "Без лимита", "По умолчанию (20 ГБ)"]);
    });
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
    expect(document.body.querySelector("[data-disk-freed]")?.textContent).toBe("Freed 15 GB");
    expect(document.body.querySelector('[data-disk-refused="src"]')?.textContent).toContain("tracked by git");
  });
});
