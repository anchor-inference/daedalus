// @vitest-environment jsdom
// The question a change to a repeating event asks: the answer is the button pressed, and backing out
// answers nothing, so the caller sends nothing.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "../i18n";
import { ConfirmHost } from "../ui/primitives";
import { askScope } from "./scope";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  setLang("en");
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root.render(<ConfirmHost />));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });
const button = (name: string) => [...document.querySelectorAll<HTMLButtonElement>(".dialog-actions button")].find((b) => b.textContent === name)!;

describe("askScope", () => {
  it("answers with the series or the one event, as the button says", async () => {
    let answer: Promise<string | null> = Promise.resolve(null);
    await act(async () => { answer = askScope("change"); });
    await settle();
    expect(document.querySelector(".dialog h3")?.textContent).toBe(t("cal.scope.change"));
    await act(async () => button(t("cal.scope.this")).click());
    expect(await answer).toBe("this");
    await act(async () => { answer = askScope("delete"); });
    await settle();
    expect(document.querySelector(".dialog h3")?.textContent).toBe(t("cal.scope.delete"));
    await act(async () => button(t("cal.scope.all")).click());
    expect(await answer).toBe("all");
  });

  it("answers nothing when the reader backs out", async () => {
    let answer: Promise<string | null> = Promise.resolve("this");
    await act(async () => { answer = askScope("change"); });
    await settle();
    await act(async () => button(t("common.cancel")).click());
    expect(await answer).toBeNull();
  });
});
