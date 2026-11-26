// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { installFocusOrigin } from "./focusorigin";

let stop: () => void = () => {};
afterEach(() => {
  stop();
  document.body.innerHTML = "";
});

function region(): HTMLElement {
  const el = document.createElement("div");
  el.tabIndex = -1;
  document.body.append(el);
  return el;
}

describe("installFocusOrigin", () => {
  it("marks the element Tab moves the focus to, and unmarks it when it loses the focus", () => {
    stop = installFocusOrigin();
    const target = region();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
    target.focus();
    expect(target.hasAttribute("data-tabbed")).toBe(true);
    target.blur();
    expect(target.hasAttribute("data-tabbed")).toBe(false);
  });

  it("leaves a region a script focuses after Enter unmarked", () => {
    stop = installFocusOrigin();
    const target = region();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    target.focus();
    expect(target.hasAttribute("data-tabbed")).toBe(false);
  });

  it("does not carry a Tab that moved nothing over to a later focus", async () => {
    stop = installFocusOrigin();
    const target = region();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
    await new Promise((resolve) => setTimeout(resolve, 5));
    target.focus();
    expect(target.hasAttribute("data-tabbed")).toBe(false);
  });

  it("forgets the Tab once the pointer is pressed", () => {
    stop = installFocusOrigin();
    const target = region();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
    document.dispatchEvent(new Event("pointerdown"));
    target.focus();
    expect(target.hasAttribute("data-tabbed")).toBe(false);
  });
});
