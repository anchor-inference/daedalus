// @vitest-environment jsdom
// The phone's "where it runs" chip: a chat of its own picks the container or the host in its sheet,
// with the host greyed out while its daemon is down; with a project picked the chip shows the side of
// the project's folder and offers no choice the server would refuse, only the way to the settings.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Project } from "./api";
import { setLang } from "./i18n";
import { RunOn, RunOnChip } from "./runon";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function runOn(over: Partial<RunOn> = {}): RunOn {
  return { docker: true, offered: true, hostReady: true, env: "container", pick: vi.fn(), body: {}, ...over };
}

const project = (env: "container" | "host"): Project => ({
  id: "p1", name: "Bakery", created_at: "", settings: { snapshots: true }, sessions: [],
  folders: [{ id: "f1", path: "/home/you/bakery", label: "", env, is_git: true, readonly: false, position: 0, managed: false, reachable: true, writable: true }],
});

describe("the run-on chip", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    document.body.innerHTML = "";
  });
  const chip = () => document.querySelector<HTMLButtonElement>(".runon-chip");

  it("picks the host for a chat of its own", () => {
    const run = runOn();
    act(() => root.render(<RunOnChip run={run} />));
    expect(chip()?.dataset.runonChip).toBe("container");
    act(() => chip()!.click());
    const hostRow = document.querySelector<HTMLButtonElement>(".runon-sheet [data-runon='host']")!;
    expect(hostRow.disabled).toBe(false);
    act(() => hostRow.click());
    expect(run.pick).toHaveBeenCalledWith("host");
    expect(document.querySelector(".runon-sheet")).toBeNull();
  });

  it("greys the host out while its daemon is down", () => {
    act(() => root.render(<RunOnChip run={runOn({ hostReady: false })} />));
    act(() => chip()!.click());
    expect(document.querySelector<HTMLButtonElement>(".runon-sheet [data-runon='host']")!.disabled).toBe(true);
  });

  it("shows a project's folder side and leads to its settings instead of a choice", () => {
    const settings = vi.fn();
    act(() => root.render(<RunOnChip run={runOn({ offered: false })} project={project("host")} onSettings={settings} />));
    expect(chip()?.dataset.runonChip).toBe("host");
    act(() => chip()!.click());
    expect(document.querySelector(".runon-sheet [data-runon]")).toBeNull();
    expect(document.querySelector(".runon-sheet")?.textContent).toContain("/home/you/bakery");
    act(() => [...document.querySelectorAll<HTMLButtonElement>(".runon-sheet button")].find((b) => b.textContent?.includes("Project settings"))!.click());
    expect(settings).toHaveBeenCalled();
  });

  it("is not there natively, where everything runs on the host", () => {
    act(() => root.render(<RunOnChip run={runOn({ docker: false, offered: false })} />));
    expect(chip()).toBeNull();
  });
});
