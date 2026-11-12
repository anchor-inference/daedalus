// @vitest-environment jsdom
// The line under a new agent link or a contact list: shown only where closing the window really takes
// the agent offline — a native installation whose desktop shell quits on close — and gone once the
// operator turns the background on from it.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BackgroundNote, backgroundNoteDue } from "./backgroundnote";
import { setLang, t } from "./i18n";
import { ADDRESS } from "./publicaddress";
import type { DeskBridge, DeskInfo } from "./quick/bridge";
import { invalidate, prime } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function info(background: boolean | undefined): DeskInfo {
  const settings = { quickChat: "Alt+Space", appshot: "Alt+Shift+Space", region: "Alt+Shift+S", voice: "Alt+Shift+V", background: null };
  return { settings, defaults: settings, report: {}, errors: {}, platform: "linux", wayland: false, tray: { ready: false, note: "gnome" }, screen: "granted", ...(background === undefined ? {} : { background }) };
}

/** A desktop shell whose closing does `background`, and records what the page asked of it. */
function shell(background: boolean | undefined) {
  let now = info(background);
  const patches: unknown[] = [];
  const desk: DeskBridge = {
    settings: async () => now,
    setSettings: async (patch) => {
      patches.push(patch);
      if (typeof patch.background === "boolean") now = { ...now, background: patch.background, settings: { ...now.settings, background: patch.background } };
      return now;
    },
    status: () => undefined,
    openScreenSettings: () => undefined,
    onAction: () => () => undefined,
    onNavigate: () => () => undefined,
  };
  (window as unknown as { daedalus?: unknown }).daedalus = { desk };
  return { patches };
}

async function draw(native: boolean) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ native }), { status: 200, headers: { "content-type": "application/json" } })));
  prime(ADDRESS, native ? { native: true, available: false } : { native: false });
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  await act(async () => root.render(<BackgroundNote />));
  await act(async () => undefined);
  return { box, done: () => (act(() => root.unmount()), box.remove()) };
}

afterEach(() => {
  delete (window as unknown as { daedalus?: unknown }).daedalus;
  invalidate(ADDRESS);
  vi.unstubAllGlobals();
  setLang("en");
});

describe("the background note", () => {
  it("is due only on a native installation whose shell quits on close", () => {
    expect(backgroundNoteDue(true, info(false))).toBe(true);
    expect(backgroundNoteDue(true, info(true))).toBe(false);
    expect(backgroundNoteDue(false, info(false))).toBe(false);
    expect(backgroundNoteDue(undefined, info(false))).toBe(false);
    expect(backgroundNoteDue(true, null)).toBe(false);
    // A shell older than the setting: no switch it would not hear.
    expect(backgroundNoteDue(true, info(undefined))).toBe(false);
  });

  it("offers the background, and is gone once it is turned on", async () => {
    const { patches } = shell(false);
    const { box, done } = await draw(true);
    expect(box.querySelector("[data-background-note]")?.textContent).toContain(t("background.note"));
    await act(async () => box.querySelector<HTMLButtonElement>("[data-background-on]")!.click());
    expect(patches).toEqual([{ background: true }]);
    expect(box.querySelector("[data-background-note]")).toBeNull();
    done();
  });

  it("says nothing in Docker mode, nor where closing already keeps the agent running", async () => {
    shell(false);
    const docker = await draw(false);
    expect(docker.box.querySelector("[data-background-note]")).toBeNull();
    docker.done();
    shell(true);
    const kept = await draw(true);
    expect(kept.box.querySelector("[data-background-note]")).toBeNull();
    kept.done();
  });

  it("says nothing outside the desktop application", async () => {
    const { box, done } = await draw(true);
    expect(box.innerHTML).toBe("");
    done();
  });

  it("is worded in both languages", async () => {
    setLang("ru");
    shell(false);
    const { box, done } = await draw(true);
    expect(box.querySelector("[data-background-on]")?.textContent).toBe("Работать в фоне");
    done();
  });
});
