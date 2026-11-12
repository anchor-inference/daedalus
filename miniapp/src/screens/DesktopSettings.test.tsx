// @vitest-environment jsdom
// Settings → Desktop app: the background switch shows what closing the window does now, tray or no
// tray, and sends the operator's choice; Quit asks first and then quits the application.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DeskBridge, DeskInfo } from "../quick/bridge";

const confirm = vi.hoisted(() => ({ answer: true }));
vi.mock("../ui", async (importOriginal) => ({ ...(await importOriginal<typeof import("../ui")>()), confirmAsync: vi.fn(async () => confirm.answer) }));

import { DesktopPanel } from "./DesktopSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function info(background: boolean | undefined, tray: DeskInfo["tray"]): DeskInfo {
  const settings = { quickChat: "Alt+Space", appshot: "Alt+Shift+Space", region: "Alt+Shift+S", voice: "Alt+Shift+V", background: null };
  return { settings, defaults: settings, report: {}, errors: {}, platform: "linux", wayland: false, tray, screen: "granted", ...(background === undefined ? {} : { background }) };
}

function shell(start: DeskInfo, withQuit = true) {
  let now = start;
  const patches: unknown[] = [];
  const quit = vi.fn();
  const desk: DeskBridge = {
    settings: async () => now,
    setSettings: async (patch) => {
      patches.push(patch);
      if (typeof patch.background === "boolean") now = { ...now, background: patch.background };
      return now;
    },
    status: () => undefined,
    openScreenSettings: () => undefined,
    onAction: () => () => undefined,
    onNavigate: () => () => undefined,
    ...(withQuit ? { quit } : {}),
  };
  (window as unknown as { daedalus?: unknown }).daedalus = { desk };
  return { patches, quit };
}

async function draw() {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  await act(async () => root.render(<DesktopPanel />));
  await act(async () => undefined);
  return { box, done: () => (act(() => root.unmount()), box.remove()) };
}

afterEach(() => {
  delete (window as unknown as { daedalus?: unknown }).daedalus;
  confirm.answer = true;
});

describe("the desktop panel", () => {
  it("lets the background be turned on where there is no tray, and says how to quit there", async () => {
    const { patches } = shell(info(false, { ready: false, note: "gnome" }));
    const { box, done } = await draw();
    const row = box.querySelector("[data-setting='desktop.background']")!;
    expect(row.textContent).toContain("AppIndicator");
    const toggle = row.querySelector<HTMLButtonElement>("[role=switch]")!;
    expect(toggle.disabled).toBe(false);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await act(async () => toggle.click());
    expect(patches).toEqual([{ background: true }]);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    done();
  });

  it("shows the switch on where the tray keeps the application by default", async () => {
    shell(info(true, { ready: true, note: "" }));
    const { box, done } = await draw();
    expect(box.querySelector("[data-setting='desktop.background'] [role=switch]")!.getAttribute("aria-checked")).toBe("true");
    done();
  });

  it("draws no switch for a shell older than the setting, and no Quit for one without it", async () => {
    shell(info(undefined, { ready: true, note: "" }), false);
    const { box, done } = await draw();
    expect(box.querySelector("[data-setting='desktop.background']")).toBeNull();
    expect(box.querySelector("[data-desktop-quit]")).toBeNull();
    done();
  });

  it("quits only once the operator confirms", async () => {
    const { quit } = shell(info(true, { ready: true, note: "" }));
    const { box, done } = await draw();
    const button = box.querySelector<HTMLButtonElement>("[data-desktop-quit]")!;
    confirm.answer = false;
    await act(async () => button.click());
    expect(quit).not.toHaveBeenCalled();
    confirm.answer = true;
    await act(async () => button.click());
    expect(quit).toHaveBeenCalledTimes(1);
    done();
  });
});
