// @vitest-environment jsdom
// The model picker opened from somewhere other than its own pill: it hangs from that control, not
// from the composer at the far side of the window, and the keyboard goes back to that control on
// Escape. And the two Anthropic routes are told apart by name.

import { act, useState } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { ModelSelect } from "./modelselect";
import { providerName } from "./ui/provider-mark";

vi.mock("./api", async (original) => {
  const real = await original<typeof import("./api")>();
  return { ...real, api: { ...real.api, get: vi.fn(() => new Promise(() => {})) } };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the provider names", () => {
  it("tells the subscription route from the key route in both languages", () => {
    setLang("en");
    expect(providerName("claude")).toBe("Anthropic · subscription");
    expect(providerName("anthropic")).toBe("Anthropic · API key");
    setLang("ru");
    expect(providerName("claude")).toBe("Anthropic · подписка");
    expect(providerName("anthropic")).toBe("Anthropic · API-ключ");
    // A name the operator gave the provider still wins.
    expect(providerName("claude", { kind: "anthropic", name: "Work", base_url: "", timeout_seconds: 0 })).toBe("Work");
    setLang("en");
  });
});

describe("the anchor", () => {
  let host: HTMLDivElement;
  let root: Root;
  let chip: HTMLButtonElement;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
    // The panel strip's chip: high on the right of the window, the way the inspector puts it.
    chip = document.createElement("button");
    chip.getBoundingClientRect = () => ({ left: 700, right: 840, top: 50, bottom: 74, width: 140, height: 24, x: 700, y: 50, toJSON() {} });
    document.body.appendChild(chip);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    chip.remove();
  });

  function Harness({ anchor }: { anchor: HTMLElement | null }) {
    const [open, setOpen] = useState(true);
    return <ModelSelect model="claude-opus-5" fallback={null} open={open} onOpenChange={setOpen} onChoose={() => {}} sheet={false} anchor={anchor} pill={false} />;
  }

  it("hangs below the chip that opened it, from its left edge", async () => {
    await act(async () => root.render(<Harness anchor={chip} />));
    const menu = document.querySelector<HTMLElement>(".model-menu")!;
    expect(menu).not.toBeNull();
    expect(menu.style.top).toBe("80px");
    expect(menu.style.left).toBe("700px");
    // No pill was asked for, so none is drawn: a running phone composer has no room for one.
    expect(host.querySelector(".model-select")).toBeNull();
  });

  it("gives the keyboard back to that chip on Escape", async () => {
    await act(async () => root.render(<Harness anchor={chip} />));
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(document.querySelector(".model-menu")).toBeNull();
    expect(document.activeElement).toBe(chip);
  });
});
