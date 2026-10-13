// @vitest-environment jsdom
// The view sheet's blocks as drawn: a checklist's rows are real checkboxes that send a tick, a table
// becomes cards on a narrow sheet and keeps its totals, Markdown from an agent cannot carry markup,
// and a block of an unknown kind is shown as its JSON, folded, rather than dropped.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { BlockSection, MarkdownView, type BlockContext } from "./BlockView";
import type { Block, BlockOp } from "./blocks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let sent: { ops: BlockOp[]; label: string }[];

beforeEach(() => {
  setLang("en");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  sent = [];
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function ctx(narrow: boolean): BlockContext {
  return { readOnly: false, narrow, onOps: (ops, label) => sent.push({ ops, label }), onAction: () => undefined, onOpenRef: () => undefined, types: [] };
}

function draw(block: Block, narrow = true, folded = false) {
  act(() => root.render(<BlockSection block={block} ctx={ctx(narrow)} folded={folded} onFold={() => undefined} />));
}

const basket: Block = {
  id: "b1", kind: "table", title: "Basket",
  columns: [{ key: "name", label: "Item", type: "text" }, { key: "price", label: "₸", type: "money", aggregate: "sum" }, { key: "got", label: "Got", type: "checkbox", aggregate: "count" }],
  rows: [{ id: "r1", cells: { name: "Oats", price: 690, got: false } }, { id: "r2", cells: { name: "Milk", price: 1180, got: true } }],
};

describe("blocks in the view sheet", () => {
  it("ticks a checklist item with one tap on its row", () => {
    draw({ id: "c1", kind: "checklist", title: "Ingredients", items: [{ id: "i1", text: "Oats", checked: false }, { id: "i2", text: "Milk", checked: true }] });
    const boxes = host.querySelectorAll<HTMLButtonElement>('[role="checkbox"]');
    expect(boxes).toHaveLength(2);
    expect(boxes[1].getAttribute("aria-checked")).toBe("true");
    expect(host.querySelector(".cal-progress-text")?.textContent).toBe("1/2");
    act(() => boxes[0].click());
    expect(sent).toEqual([{ ops: [{ op: "set_checked", block_id: "c1", item_id: "i1", checked: true }], label: "Oats" }]);
  });

  it("draws a table as cards on a narrow sheet, with its totals, and ticks a row's cell", () => {
    draw(basket, true);
    expect(host.querySelector("table")).toBeNull();
    const cards = host.querySelectorAll(".cal-card");
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain("Oats");
    expect(host.querySelector(".cal-totals")?.textContent).toContain("1,870");
    expect(host.querySelector(".cal-totals")?.textContent).toContain("1/2");
    act(() => host.querySelector<HTMLButtonElement>(".cal-card-hit")!.click());
    expect(sent[0].ops).toEqual([{ op: "set_checked", block_id: "b1", row_id: "r1", column: "got", checked: true }]);
  });

  it("draws the same table as a table on a wide sheet, its rows headed by the first column", () => {
    draw(basket, false);
    expect(host.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(host.querySelector('tbody th[scope="row"]')?.textContent).toBe("Oats");
    expect(host.querySelector<HTMLButtonElement>('tbody [role="checkbox"]')?.getAttribute("aria-label")).toBe("Oats: Got");
  });

  it("folds under its head", () => {
    draw(basket, true, true);
    expect(host.querySelector(".cal-blk-head")?.getAttribute("aria-expanded")).toBe("false");
    expect(host.querySelector(".cal-cards")).toBeNull();
  });

  it("shows a block of an unknown kind as its JSON, under an unsupported head", () => {
    draw({ id: "x", kind: "poll", title: "Dinner vote", options: ["fish", "pasta"] }, true, false);
    expect(host.querySelector(".cal-blk-kind")?.textContent).toBe("poll");
    expect(host.querySelector(".cal-unknown")?.textContent).toContain("\"pasta\"");
  });

  it("numbers the steps and offers a timer for a step that takes time", () => {
    draw({ id: "s", kind: "steps", title: "Recipe", items: [{ id: "a", text: "Boil the water", done: false, duration_min: 15 }, { id: "b", text: "Serve", done: true }] });
    const numbers = host.querySelectorAll(".cal-step-n");
    expect(numbers[0].textContent).toBe("1");
    expect(host.querySelector(".cal-timer")?.textContent).toContain("15 min");
    expect(numbers[1].getAttribute("aria-checked")).toBe("true");
  });

  it("escapes markup in Markdown and still draws the Markdown", () => {
    act(() => root.render(<MarkdownView text={"**Buy** <img src=x onerror=alert(1)> [shop](https://example.org)"} />));
    expect(host.querySelector("b")?.textContent).toBe("Buy");
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector("a")?.getAttribute("href")).toBe("https://example.org");
  });
});
