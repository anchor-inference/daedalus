// @vitest-environment jsdom
// Settings → Limits → Memory for host commands: the setting's three kinds, what the host's terminal
// daemon reports beside it, and the reasons it can report nothing.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HostMemoryCap, Settings } from "../api";

const answer = vi.hoisted(() => ({ view: null as unknown }));
vi.mock("../api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api")>();
  return { ...real, api: { ...real.api, get: vi.fn(async () => answer.view) } };
});

import { formatCap, HostMemoryRow, parseCap } from "./HostMemory";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GB = 2 ** 30;

function view(status: Partial<NonNullable<HostMemoryCap["status"]>> | null, extra: Partial<HostMemoryCap> = {}): HostMemoryCap {
  return {
    env: "host", setting: "60%", available: status !== null, reason: "", error: "",
    status: status === null ? null : { supported: true, mode: "jobs", machine_bytes: 64 * GB, max_bytes: 38.4 * GB, high_bytes: null, swap_max_bytes: 2 * GB, current_bytes: 5 * GB, oom_kills: 0, ...status },
    ...extra,
  };
}

async function draw(cap: string | undefined, save = vi.fn(async () => undefined)) {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  const s = { terminals: { running_cap: 20, ...(cap === undefined ? {} : { host_memory_cap: cap }) } } as unknown as Settings;
  await act(async () => root.render(<HostMemoryRow s={s} save={save} />));
  await act(async () => undefined);
  return { box, save, done: () => (act(() => root.unmount()), box.remove()) };
}

afterEach(() => {
  answer.view = null;
});

describe("the host commands' memory cap", () => {
  it("reads and writes the three kinds", () => {
    expect(parseCap("60%")).toEqual({ kind: "percent", amount: 60 });
    expect(parseCap("24G")).toEqual({ kind: "gb", amount: 24 });
    expect(parseCap("none")).toEqual({ kind: "none", amount: 0 });
    expect(formatCap("percent", 45)).toBe("45%");
    expect(formatCap("gb", 0.5)).toBe("0.5G");
    expect(formatCap("none", 7)).toBe("none");
  });

  it("shows the machine, the use and the cap in force, and switches to no limit", async () => {
    answer.view = view({ oom_kills: 2 });
    const { box, save, done } = await draw(undefined);
    const row = box.querySelector("[data-setting='terminals.host_memory_cap']")!;
    const state = row.querySelector(".settings-hostmem-state")!;
    expect(state.getAttribute("data-state")).toBe("ok");
    expect(state.textContent).toContain("64.0 GB");
    expect(state.textContent).toContain("5.0 GB");
    expect(state.textContent).toContain("38.4 GB");
    expect(state.textContent).toContain(": 2.");
    expect(row.querySelector("[data-changed]")).toBeNull();
    expect((row.querySelector("input") as HTMLInputElement).value).toBe("60");
    const none = [...row.querySelectorAll("[role=radio]")].find((b) => b.getAttribute("aria-checked") === "false" && b.textContent === "No limit") as HTMLButtonElement;
    await act(async () => none.click());
    expect(save).toHaveBeenCalledWith({ terminals: { running_cap: 20, host_memory_cap: "none" } });
    done();
  });

  it("says why nothing is in force", async () => {
    answer.view = view(null, { reason: "outdated" });
    let drawn = await draw("24G");
    expect(drawn.box.querySelector(".settings-hostmem-state")!.getAttribute("data-state")).toBe("outdated");
    expect(drawn.box.querySelector("[data-changed]")).not.toBeNull();
    drawn.done();
    answer.view = view({ supported: false, mode: "", reason: "the daemon shares its control group with other programs" });
    drawn = await draw("none");
    expect(drawn.box.querySelector(".settings-hostmem-state")!.textContent).toContain("shares its control group");
    expect(drawn.box.querySelector("input")).toBeNull();
    drawn.done();
    answer.view = view({ mode: "own" });
    drawn = await draw("60%");
    expect(drawn.box.querySelector(".settings-hostmem-state")!.textContent).toContain("host-terminal.sh install");
    drawn.done();
  });
});
