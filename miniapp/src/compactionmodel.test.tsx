// @vitest-environment jsdom
// The summary model picker in Settings → Context: the session's own model first, every
// preset with its provider, and a preset deleted since it was chosen shown as missing.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Preset } from "./api";
import { CompactionModelSelect, compactionMissing } from "./compactionmodel";
import { BLANK } from "./models";
import { setLang } from "./i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PRESETS: Record<string, Preset> = {
  strong: { ...BLANK, provider: "anthropic", model: "claude-opus", label: "Claude Opus" },
  flash: { ...BLANK, provider: "deepseek", model: "deepseek-flash", label: "" },
};

describe("a missing choice", () => {
  it("is one that names no configured preset", () => {
    expect(compactionMissing(PRESETS, "")).toBe(false);
    expect(compactionMissing(PRESETS, "flash")).toBe(false);
    expect(compactionMissing(PRESETS, "gone")).toBe(true);
    expect(compactionMissing(undefined, "gone")).toBe(true);
  });
});

describe("the picker", () => {
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
  });

  const press = (el: Element) => act(async () => { (el as HTMLElement).click(); });

  it("shows the effective value, offers the session's own model first, saves a new choice, and marks a missing one", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    const onSave = vi.fn();
    await act(async () => root.render(<CompactionModelSelect catalogue={{ presets: PRESETS }} value="flash" onSave={onSave} onEffort={() => undefined} />));
    const trigger = host.querySelector<HTMLButtonElement>(".dropdown-btn")!;
    expect(trigger.textContent).toBe("deepseek/deepseek-flash");
    expect(host.textContent).toContain("seconds instead of minutes");
    await press(trigger);
    const options = [...document.querySelectorAll<HTMLElement>("[role=option]")];
    expect(options.map((o) => o.dataset.value)).toEqual(["", "strong", "flash"]);
    expect(options[0].textContent).toContain("The session's own model");
    await press(options[0]);
    expect(onSave).toHaveBeenCalledWith("");

    await act(async () => root.render(<CompactionModelSelect catalogue={{ presets: PRESETS }} value="gone" onSave={onSave} onEffort={() => undefined} />));
    expect(host.querySelector(".dropdown-btn")!.textContent).toContain("No longer configured");
    expect(host.textContent).toContain("uses the session's own model");
    vi.unstubAllGlobals();
  });
});
