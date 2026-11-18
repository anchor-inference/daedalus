// @vitest-environment jsdom
// Where a dragged file lands: the innermost registered element under the pointer, marked without a
// render, and nothing anywhere else; the mark leaves on drop, on Escape, after a dragleave that no
// dragover follows, and when the dragovers stop.
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LEAVE_MS, QUIET_MS, clear, marked, targetAt, useDropTarget } from "./filedrop";

let host: HTMLDivElement;
let root: Root;
let renders = 0;
const composerTook: File[][] = [];
const cardTook: File[][] = [];

function Screen({ cardTakes = true }: { cardTakes?: boolean }) {
  renders += 1;
  const composer = useRef<HTMLDivElement>(null);
  const card = useRef<HTMLElement>(null);
  useDropTarget(composer, (files) => composerTook.push(files));
  useDropTarget(card, cardTakes ? (files) => cardTook.push(files) : null);
  return (
    <div className="chat">
      <div className="transcript"><p className="turn">words</p></div>
      <div ref={composer} className="composer">
        <article ref={card} className="ask"><span className="option">A</span></article>
        <div className="field"><textarea /></div>
      </div>
    </div>
  );
}

function drag(type: string, at: Element | Window, files: File[] = [], extra: Partial<{ relatedTarget: Element | null }> = {}): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const data = { types: ["Files"], files, dropEffect: "none" };
  Object.defineProperty(event, "dataTransfer", { value: data });
  Object.defineProperty(event, "relatedTarget", { value: extra.relatedTarget ?? null });
  at.dispatchEvent(event);
  return event;
}

const el = (selector: string) => host.querySelector(selector)!;

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  renders = 0;
  composerTook.length = 0;
  cardTook.length = 0;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root.render(<Screen />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  clear();
  vi.useRealTimers();
});

describe("file drop targets", () => {
  it("marks the innermost target under the pointer, and nothing over the transcript", () => {
    drag("dragover", el(".field textarea"));
    expect(marked()).toBe(el(".composer"));
    expect(el(".composer").hasAttribute("data-drop-over")).toBe(true);
    drag("dragover", el(".ask .option"));
    expect(marked()).toBe(el(".ask"));
    expect(el(".composer").hasAttribute("data-drop-over")).toBe(false);
    const over = drag("dragover", el(".turn"));
    expect(marked()).toBeNull();
    // Taken, so the browser does not open the file there, with the refusing cursor.
    expect(over.defaultPrevented).toBe(true);
    expect((over as unknown as { dataTransfer: { dropEffect: string } }).dataTransfer.dropEffect).toBe("none");
  });

  it("does not render anything while the pointer moves", () => {
    const before = renders;
    for (let i = 0; i < 50; i++) drag("dragover", el(i % 2 ? ".turn" : ".field textarea"));
    expect(renders).toBe(before);
  });

  it("gives a dropped file to the target it was dropped on, and only to it", () => {
    const file = new File(["x"], "shot.png", { type: "image/png" });
    drag("dragover", el(".ask .option"));
    drag("drop", el(".ask .option"), [file]);
    expect(cardTook).toEqual([[file]]);
    expect(composerTook).toEqual([]);
    expect(marked()).toBeNull();
    drag("drop", el(".field textarea"), [file]);
    expect(composerTook).toEqual([[file]]);
    const nowhere = drag("drop", el(".turn"), [file]);
    expect(nowhere.defaultPrevented).toBe(true);
    expect(composerTook.length + cardTook.length).toBe(2);
  });

  it("passes a card that takes no files through to the target around it", async () => {
    await act(async () => root.render(<Screen cardTakes={false} />));
    expect(targetAt(el(".ask .option"))).toBe(el(".composer"));
    expect(el(".ask").hasAttribute("data-drop-target")).toBe(false);
  });

  it("leaves a drop zone of its own alone", () => {
    const other = document.createElement("div");
    host.append(other);
    other.addEventListener("dragover", (e) => e.preventDefault());
    drag("dragover", el(".field textarea"));
    drag("dragover", other);
    expect(marked()).toBeNull();
  });

  it("clears on Escape, after a dragleave with no dragover, and when dragovers stop", () => {
    drag("dragover", el(".field textarea"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(marked()).toBeNull();

    drag("dragover", el(".field textarea"));
    drag("dragleave", el(".field textarea"));
    vi.advanceTimersByTime(LEAVE_MS - 10);
    drag("dragover", el(".field textarea"));
    vi.advanceTimersByTime(LEAVE_MS);
    // A leave followed by a dragover was a move inside the page: the mark stays.
    expect(marked()).toBe(el(".composer"));
    drag("dragleave", el(".field textarea"));
    vi.advanceTimersByTime(LEAVE_MS + 1);
    expect(marked()).toBeNull();

    drag("dragover", el(".field textarea"));
    vi.advanceTimersByTime(QUIET_MS + 1);
    expect(marked()).toBeNull();
  });

  it("ignores a drag that carries no files", () => {
    const event = new Event("dragover", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "dataTransfer", { value: { types: ["text/plain"], files: [] } });
    el(".field textarea").dispatchEvent(event);
    expect(marked()).toBeNull();
    expect(event.defaultPrevented).toBe(false);
  });

  it("forgets an element that unmounts while marked", () => {
    drag("dragover", el(".ask .option"));
    act(() => root.unmount());
    expect(marked()).toBeNull();
    root = createRoot(host);
  });
});
