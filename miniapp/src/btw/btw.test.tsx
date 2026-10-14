// @vitest-environment jsdom
// The side question (/btw) in the composer: the line that asks one, the palette's entry, the helpers'
// menu that holds it beside the prompt coach, and that a /btw sent while the agent works opens the
// window instead of becoming a steer.

import { act, createRef } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer, type ComposerHandle, PlusSheet } from "../composerbox";
import { setLang } from "../i18n";
import { btwCommand, btwQuestion } from "./btw";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the line that asks a side question", () => {
  it("takes what follows /btw as the question, over several lines too", () => {
    expect(btwQuestion("/btw where did the prices go?")).toBe("where did the prices go?");
    expect(btwQuestion("  /BTW   why?  ")).toBe("why?");
    expect(btwQuestion("/btw first line\nsecond line")).toBe("first line\nsecond line");
  });

  it("opens an empty window for /btw alone and leaves every other line to the chat", () => {
    expect(btwQuestion("/btw")).toBe("");
    expect(btwQuestion("/btw   ")).toBe("");
    expect(btwQuestion("/btwx why")).toBeNull();
    expect(btwQuestion("by the way /btw")).toBeNull();
    expect(btwQuestion("/compact")).toBeNull();
  });

  it("is offered in the palette in the reader's language", () => {
    setLang("ru");
    expect(btwCommand()).toMatchObject({ name: "btw", args: "<вопрос>", confirm: false });
    setLang("en");
    expect(btwCommand().description).toContain("without interrupting");
  });
});

describe("the composer", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    localStorage.clear();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    document.body.innerHTML = "";
  });

  function draw(over: Partial<Parameters<typeof Composer>[0]> = {}) {
    const onSend = vi.fn(async () => "sent" as const);
    const onBtw = vi.fn();
    const onCoach = vi.fn();
    const onStop = vi.fn();
    const ref = createRef<ComposerHandle>();
    act(() => root.render(
      <Composer ref={ref} sessionId="chat-1" status="idle" onSend={onSend} onStop={onStop} commands={[{ name: "compact", args: "", description: "compact", scope: "session", confirm: false }]}
        onCommand={async () => undefined} model="m" fallback={null} onChooseModel={() => undefined} queued={[]} phone={false} toast={() => undefined}
        onBtw={onBtw} onCoach={onCoach} {...over} />,
    ));
    return { onSend, onBtw, onCoach, onStop, ref };
  }

  async function type(text: string) {
    const field = host.querySelector("textarea")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(field, text);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }

  // Ctrl+Enter sends on every device; jsdom reads as a touch screen, where a bare Enter is a new line.
  async function enter() {
    const field = host.querySelector("textarea")!;
    await act(async () => { field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true, cancelable: true })); });
  }

  it("opens the window for /btw instead of sending, and while the agent works never steers with it", async () => {
    for (const status of ["idle", "running"] as const) {
      const { onSend, onBtw } = draw({ status });
      await type("/btw what is it doing now?");
      await enter();
      expect(onBtw).toHaveBeenCalledWith("what is it doing now?");
      expect(onSend).not.toHaveBeenCalled();
      expect(host.querySelector("textarea")!.value).toBe("");
      // The round button sends the same way.
      await type("/btw and why?");
      await act(async () => { (host.querySelector(".roundbtn.primary") as HTMLButtonElement).click(); });
      expect(onBtw).toHaveBeenLastCalledWith("and why?");
      expect(onSend).not.toHaveBeenCalled();
    }
  });

  it("sends an ordinary message that only mentions /btw", async () => {
    const { onSend, onBtw } = draw();
    await type("remember the /btw command");
    await enter();
    expect(onSend).toHaveBeenCalled();
    expect(onBtw).not.toHaveBeenCalled();
  });

  it("lists /btw in the palette beside the host's commands", async () => {
    draw();
    await type("/b");
    const items = [...host.querySelectorAll(".palette-item")].map((item) => item.textContent ?? "");
    expect(items).toHaveLength(1);
    expect(items[0]).toContain("/btw");
    await type("/");
    expect(host.querySelectorAll(".palette-item")).toHaveLength(2);
  });

  it("keeps the palette to the host's commands where there is no side question (inside a window)", async () => {
    const { onSend } = draw({ onBtw: undefined });
    await type("/b");
    expect(host.querySelectorAll(".palette-item")).toHaveLength(0);
    await type("/btw why");
    await enter();
    expect(onSend).toHaveBeenCalled();
  });

  it("holds the coach and the side question in one button's menu", async () => {
    const { onBtw, onCoach } = draw();
    const buttons = host.querySelectorAll(".coach-open");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute("aria-haspopup")).toBe("menu");
    await type("fix the menu");
    await act(async () => { (buttons[0] as HTMLButtonElement).click(); });
    const coach = document.querySelector<HTMLButtonElement>(".helpers-menu [data-action='coach']")!;
    const btw = document.querySelector<HTMLButtonElement>(".helpers-menu [data-action='btw']")!;
    expect(coach.textContent).toContain("Improve prompt");
    expect(btw.textContent).toContain("Quick question");
    await act(async () => { coach.click(); });
    expect(onCoach).toHaveBeenCalledWith("fix the menu");
    expect(document.querySelector(".helpers-menu")).toBeNull();
    await act(async () => { (host.querySelector(".coach-open") as HTMLButtonElement).click(); });
    await act(async () => { document.querySelector<HTMLButtonElement>(".helpers-menu [data-action='btw']")!.click(); });
    expect(onBtw).toHaveBeenCalledWith("");
  });

  it("opens the coach straight away where it is the only helper", async () => {
    const { onCoach } = draw({ onBtw: undefined });
    const button = host.querySelector<HTMLButtonElement>(".coach-open")!;
    expect(button.getAttribute("aria-haspopup")).toBeNull();
    expect(button.getAttribute("aria-label")).toBe("Improve prompt");
    await act(async () => { button.click(); });
    expect(onCoach).toHaveBeenCalled();
    expect(document.querySelector(".helpers-menu")).toBeNull();
  });

  it("leaves a press of Escape that a layer above already took", async () => {
    draw();
    await type("/");
    const field = host.querySelector("textarea")!;
    const taken = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    taken.preventDefault();
    await act(async () => { field.dispatchEvent(taken); });
    expect(field.value).toBe("/");
    await act(async () => { field.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
    expect(field.value).toBe("");
  });
});

describe("the phone's + sheet", () => {
  it("offers the side question as a row beside the coach", () => {
    setLang("ru");
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const onBtw = vi.fn();
    act(() => root.render(<PlusSheet onClose={() => undefined} onPhoto={() => undefined} onFiles={() => undefined} onCoach={() => undefined} onBtw={onBtw} />));
    const row = document.querySelector<HTMLElement>("[data-action='btw']")!;
    expect(row.textContent).toContain("Вопрос по ходу");
    expect(document.querySelector("[data-action='coach']")).not.toBeNull();
    act(() => row.click());
    expect(onBtw).toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
    setLang("en");
  });
});
