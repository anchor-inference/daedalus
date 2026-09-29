// @vitest-environment jsdom
// The summary model picker in Settings → Limits & budget: the session's own model first, every
// preset under the model picker's label, and a preset deleted since it was chosen shown as missing.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Preset } from "./api";
import { CompactionModelSelect, compactionOptions } from "./compactionmodel";
import { BLANK } from "./models";
import { setLang } from "./i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PRESETS: Record<string, Preset> = {
  strong: { ...BLANK, provider: "anthropic", model: "claude-opus", label: "Claude Opus" },
  flash: { ...BLANK, provider: "deepseek", model: "deepseek-flash", label: "" },
};

describe("the options", () => {
  beforeEach(() => setLang("en"));

  it("offers the session's own model first, then every preset as the model picker names it", () => {
    expect(compactionOptions(PRESETS, "")).toEqual([
      { value: "", label: "The session's own model" },
      { value: "strong", label: "Claude Opus" },
      { value: "flash", label: "deepseek/deepseek-flash" },
    ]);
  });

  it("keeps a deleted preset as a missing choice instead of pretending the first one is in use", () => {
    const options = compactionOptions(PRESETS, "gone");
    expect(options.at(-1)).toEqual({ value: "gone", label: "gone · missing", missing: true });
    expect(compactionOptions(undefined, "gone").map((o) => o.value)).toEqual(["", "gone"]);
    setLang("ru");
    expect(compactionOptions(PRESETS, "")[0].label).toBe("Модель самой сессии");
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

  it("shows the effective value, saves a new choice, and marks a missing one", () => {
    const onSave = vi.fn();
    act(() => root.render(<CompactionModelSelect presets={PRESETS} value="flash" onSave={onSave} />));
    const picker = host.querySelector<HTMLButtonElement>("#compaction-preset")!;
    expect(picker.dataset.value).toBe("flash");
    expect(picker.textContent).toBe("deepseek/deepseek-flash");
    expect(picker.getAttribute("aria-invalid")).toBeNull();
    expect(host.textContent).toContain("seconds instead of minutes");
    act(() => picker.click());
    const own = [...host.querySelectorAll<HTMLElement>("[role=option]")].find((o) => o.dataset.value === "")!;
    act(() => own.click());
    expect(onSave).toHaveBeenCalledWith("");

    act(() => root.render(<CompactionModelSelect presets={PRESETS} value="gone" onSave={onSave} />));
    const again = host.querySelector<HTMLButtonElement>("#compaction-preset")!;
    expect(again.dataset.value).toBe("gone");
    expect(again.getAttribute("aria-invalid")).toBe("true");
    expect(again.textContent).toContain("missing");
    expect(host.textContent).toContain("uses the session's own model");
  });
});
