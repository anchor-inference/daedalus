// @vitest-environment jsdom
// The summary model picker in Settings → Limits & budget: the session's own model first, every
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

  it("shows the effective value, offers the session's own model first, saves a new choice, and marks a missing one", () => {
    const onSave = vi.fn();
    act(() => root.render(<CompactionModelSelect catalogue={{ presets: PRESETS }} value="flash" onSave={onSave} />));
    expect(host.querySelector(".sel-current .sel-name")!.textContent).toBe("deepseek/deepseek-flash");
    expect(host.textContent).toContain("seconds instead of minutes");
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const options = [...host.querySelectorAll<HTMLElement>("[role=option]")];
    expect(options.map((o) => o.dataset.id)).toEqual(["", "strong", "flash"]);
    expect(options[0].textContent).toContain("The session's own model");
    act(() => options[0].click());
    expect(onSave).toHaveBeenCalledWith("");

    act(() => root.render(<CompactionModelSelect catalogue={{ presets: PRESETS }} value="gone" onSave={onSave} />));
    expect(host.querySelector(".sel-current")!.classList.contains("missing")).toBe(true);
    expect(host.querySelector(".sel-current")!.textContent).toContain("No longer configured");
    expect(host.textContent).toContain("uses the session's own model");
  });
});
