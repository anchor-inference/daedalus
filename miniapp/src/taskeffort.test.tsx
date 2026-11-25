// @vitest-environment jsdom
// The effort beside a task's model: the first row says which default the task has, the rest are the
// composer's own words for the efforts, and a pick is handed on as the stored word.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { TaskEffortPicker, effortDefaultLabel, taskEffortItems } from "./taskeffort";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the words", () => {
  afterEach(() => setLang("en"));

  it("names each default the way the task has it, in both languages", () => {
    setLang("ru");
    expect(effortDefaultLabel("off")).toBe("По умолчанию — без рассуждений");
    expect(effortDefaultLabel("model")).toBe("По умолчанию — как у модели");
    setLang("en");
    expect(effortDefaultLabel("off")).toBe("Default — no reasoning");
    expect(effortDefaultLabel({ settings: "High" })).toBe("Default — as in Settings (High)");
  });

  it("offers off and the efforts the session menu offers, with its hints", () => {
    setLang("en");
    const items = taskEffortItems();
    expect(items.map((item) => item.id)).toEqual(["none", "low", "medium", "high", "xhigh"]);
    expect(items[0].label).toBe("Off");
    expect(items[4].label).toBe("Max");
    expect(items.every((item) => !!item.detail)).toBe(true);
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

  it("hands on the stored word, and empty for the default", () => {
    const chosen = vi.fn();
    act(() => root.render(<TaskEffortPicker id="e" value="" onChange={chosen} fallback="model" />));
    expect(host.querySelector(".sel-current .sel-name")!.textContent).toBe("Default — as the model is set");
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    act(() => host.querySelector<HTMLElement>("[role=option][data-id='none']")!.click());
    expect(chosen).toHaveBeenCalledWith("none");
  });
});
