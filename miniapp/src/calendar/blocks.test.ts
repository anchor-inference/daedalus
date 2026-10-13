import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import {
  aggregateParts, aggregateText, applyOps, blockProgress, cellText, columnTotal, diffBlocks, eventAsText, eventProgress, inverseCheck, isKnown, linesToItems, renderChip, statusFlow, swipeActions, tableCards, typedFields,
  type Block, type ChecklistBlock, type Column, type StepsBlock, type TableBlock, type TypeDef,
} from "./blocks";
import { eventItem } from "./items";
import type { Occurrence } from "./types";

afterEach(() => setLang("en"));

const NBSP = " ";

function basket(): TableBlock {
  return {
    id: "b1", kind: "table", title: "Basket",
    columns: [
      { key: "name", label: "Item", type: "text" },
      { key: "qty", label: "Qty", type: "text" },
      { key: "price", label: "₸", type: "money", aggregate: "sum" },
      { key: "got", label: "✓", type: "checkbox", aggregate: "count" },
    ],
    rows: [
      { id: "r1", cells: { name: "Buckwheat 1 kg", qty: "1 pc", price: 435, got: true } },
      { id: "r2", cells: { name: "Oats", qty: "1 pc", price: 690, got: false } },
      { id: "r3", cells: { name: "Milk", qty: "2 pc", price: "1180", got: false } },
    ],
  };
}

function checklist(n = 3, ticked = 1): ChecklistBlock {
  return { id: "c1", kind: "checklist", title: "Ingredients", items: Array.from({ length: n }, (_, i) => ({ id: `i${i}`, text: `item ${i}`, checked: i < ticked })) };
}

const meal: TypeDef = {
  id: "meal", name: "Meal", icon: "utensils", color: "#2bb673",
  fields_schema: { kcal: { type: "number", unit: "kcal" }, protein_g: { type: "number", unit: "g" }, meal: { type: "enum", values: ["Breakfast", "Lunch"] } },
  display: { chip: "{meal} · {kcal} kcal", badge: "progress:checklist" },
  quick_actions: ["mark_done"],
};

describe("progress", () => {
  it("counts ticked items, finished steps and ticked rows", () => {
    expect(blockProgress(checklist(50, 12))).toEqual({ done: 12, total: 50 });
    const steps: StepsBlock = { id: "s", kind: "steps", items: [{ id: "a", text: "a", done: true }, { id: "b", text: "b", done: false }] };
    expect(blockProgress(steps)).toEqual({ done: 1, total: 2 });
    expect(blockProgress(basket())).toEqual({ done: 1, total: 3 });
    expect(blockProgress({ id: "m", kind: "markdown", text: "x" } as Block)).toBeNull();
  });

  it("follows the type's badge, and otherwise takes the first checklist, then a table", () => {
    const blocks: Block[] = [basket(), checklist(4, 2)];
    expect(eventProgress(blocks)).toEqual({ done: 2, total: 4 });
    expect(eventProgress(blocks, "progress:table")).toEqual({ done: 1, total: 3 });
    expect(eventProgress([{ id: "m", kind: "markdown", text: "" } as Block])).toBeNull();
  });
});

describe("a table's totals and cells", () => {
  it("sums a money column with the symbol its head carries, and counts the ticked rows", () => {
    const table = basket();
    expect(columnTotal(table.columns[2], table.rows)).toMatchObject({ value: 2305, text: `2,305${NBSP}₸` });
    expect(columnTotal(table.columns[3], table.rows)).toMatchObject({ value: 1, of: 3, text: "1/3" });
    expect(columnTotal(table.columns[0], table.rows)).toBeNull();
  });

  it("writes numbers the reader's way", () => {
    setLang("ru");
    const table = basket();
    expect(columnTotal(table.columns[2], table.rows)?.text).toBe(`2${NBSP}305${NBSP}₸`);
    expect(cellText({ key: "k", label: "Ккал", type: "number", unit: "ккал" }, 2028)).toBe(`2${NBSP}028${NBSP}ккал`);
  });

  it("averages when asked", () => {
    const column: Column = { key: "price", label: "₸", type: "money", aggregate: "avg" };
    expect(columnTotal(column, basket().rows)?.value).toBeCloseTo(768.33, 1);
  });
});

describe("a table as cards on a phone", () => {
  it("makes the first text column the title, the checkbox the card's, the rest its lines", () => {
    const cards = tableCards(basket());
    expect(cards).toHaveLength(3);
    expect(cards[0]).toMatchObject({ id: "r1", title: "Buckwheat 1 kg", checked: true });
    expect(cards[0].check?.key).toBe("got");
    expect(cards[0].lines.map((l) => [l.label, l.text])).toEqual([["Qty", "1 pc"], ["₸", `435${NBSP}₸`]]);
  });

  it("drops an empty cell rather than printing a label with nothing after it", () => {
    const table = basket();
    table.rows[1].cells.qty = "";
    expect(tableCards(table)[1].lines.map((l) => l.key)).toEqual(["price"]);
  });
});

describe("a day's totals", () => {
  it("marks a sum inside the target with a tick, and one outside with an arrow", () => {
    const base = { type: "meal", field: "kcal", op: "sum", unit: "kcal", target: [1900, 2100] as [number, number] };
    expect(aggregateParts({ ...base, value: 2028 })).toEqual({ symbol: "Σ", value: `2,028${NBSP}kcal`, state: "ok" });
    expect(aggregateParts({ ...base, value: 1830 }).state).toBe("low");
    expect(aggregateParts({ ...base, value: 2510 }).state).toBe("high");
    expect(aggregateText({ ...base, value: 2028 })).toBe(`Σ 2,028${NBSP}kcal ✓`);
    expect(aggregateText({ type: "shopping", field: "total", op: "sum", value: 24085.4, unit: "₸" })).toBe(`Σ 24,085${NBSP}₸`);
  });

  it("reads Russian digits on a Russian page", () => {
    setLang("ru");
    expect(aggregateText({ type: "meal", field: "kcal", op: "sum", value: 2028, unit: "ккал", target: [1900, 2100] })).toBe(`Σ 2${NBSP}028${NBSP}ккал ✓`);
  });
});

describe("the type's chip", () => {
  it("fills the template from the fields and the title", () => {
    expect(renderChip("{meal} · {kcal} kcal", "Oatmeal", { meal: "Breakfast", kcal: 518 })).toBe("Breakfast · 518 kcal");
    expect(renderChip("{title}", "Oatmeal", {})).toBe("Oatmeal");
  });

  it("drops a part whose field is missing, with its separator", () => {
    expect(renderChip("{meal} · {kcal} kcal", "Oatmeal", { meal: "Breakfast" })).toBe("Breakfast");
    expect(renderChip("{meal} · {kcal} kcal", "Oatmeal", {})).toBeNull();
    expect(renderChip(undefined, "Oatmeal", {})).toBeNull();
  });

  it("is carried by the event's item, with the type's colour for the month's marks", () => {
    const event = { id: "e", event_id: "e", calendar_id: "c", title: "Oatmeal", description: "", location: "", start_at: "2026-10-13T06:00:00Z", end_at: "2026-10-13T06:30:00Z", all_day: false, timezone: "UTC", color: "#4f8ff7", recurrence: "", recurring: false, occurrence_start: null, reminders: [], version: 1, writable: true, pending_sync: false, type: "meal", fields: { meal: "Breakfast", kcal: 518 }, status: "done" } as Occurrence;
    const item = eventItem(event, "UTC", meal);
    expect(item).toMatchObject({ chip: "Breakfast · 518 kcal", typeColor: "#2bb673", color: "#2bb673", done: true });
    expect(eventItem({ ...event, chip: "From the host" }, "UTC", meal).chip).toBe("From the host");
    expect(eventItem({ ...event, type: null }, "UTC").color).toBe("#4f8ff7");
  });
});

describe("pasting a list", () => {
  it("makes an item of every line, reading bullets, numbers and boxes", () => {
    const items = linesToItems("- Buckwheat\n2. Oats\n[x] Milk\n☑ Eggs\n☐ Bread\n\n   \n* [ ] Salt");
    expect(items.map((i) => [i.text, i.checked])).toEqual([["Buckwheat", false], ["Oats", false], ["Milk", true], ["Eggs", true], ["Bread", false], ["Salt", false]]);
    expect(new Set(items.map((i) => i.id)).size).toBe(items.length);
  });
});

describe("a patch applied here before the host answers", () => {
  const blocks = (): Block[] => [checklist(3, 0), basket(), { id: "s1", kind: "steps", items: [{ id: "a", text: "Boil", done: false, duration_min: 15 }] } as StepsBlock, { id: "x", kind: "poll", options: ["a", "b"] }];

  it("ticks a checklist item, a step and a table cell, and leaves an unknown block as it was", () => {
    const next = applyOps(blocks(), [
      { op: "set_checked", block_id: "c1", item_id: "i1", checked: true },
      { op: "set_checked", block_id: "s1", item_id: "a", checked: true },
      { op: "set_checked", block_id: "b1", row_id: "r2", column: "got", checked: true },
    ]);
    expect((next[0] as ChecklistBlock).items[1].checked).toBe(true);
    expect((next[2] as StepsBlock).items[0].done).toBe(true);
    expect((next[1] as TableBlock).rows[1].cells.got).toBe(true);
    expect(next[3]).toEqual(blocks()[3]);
    expect(isKnown(next[3])).toBe(false);
  });

  it("is taken back by its inverse: the undo of a tick", () => {
    const tick = { op: "set_checked" as const, block_id: "c1", item_id: "i0", checked: true };
    const ticked = applyOps(blocks(), [tick]);
    expect(blockProgress(ticked[0])).toEqual({ done: 1, total: 3 });
    const back = applyOps(ticked, [inverseCheck(tick)]);
    expect(back[0]).toEqual(blocks()[0]);
  });

  it("adds, edits, removes and reorders items, rows and blocks", () => {
    const next = applyOps(blocks(), [
      { op: "add_item", block_id: "c1", item: { id: "new", text: "Salt", checked: false }, index: 0 },
      { op: "update_item", block_id: "c1", item_id: "i2", patch: { text: "Pepper" } },
      { op: "remove_item", block_id: "c1", item_id: "i1" },
      { op: "update_cell", block_id: "b1", row_id: "r3", column: "price", value: 990 },
      { op: "remove_row", block_id: "b1", row_id: "r1" },
      { op: "reorder", block_id: "b1", ids: ["r3", "r2"] },
      { op: "reorder_blocks", ids: ["x", "s1"] },
      { op: "remove_block", block_id: "c1" },
    ]);
    expect(next.map((b) => b.id)).toEqual(["x", "s1", "b1"]);
    expect((next[2] as TableBlock).rows.map((r) => [r.id, r.cells.price])).toEqual([["r3", 990], ["r2", 690]]);
    const list = applyOps(blocks(), [{ op: "add_item", block_id: "c1", item: { id: "new", text: "Salt", checked: false }, index: 0 }, { op: "update_item", block_id: "c1", item_id: "i2", patch: { text: "Pepper" } }])[0] as ChecklistBlock;
    expect(list.items.map((i) => i.text)).toEqual(["Salt", "item 0", "item 1", "Pepper"]);
  });

  it("changes nothing for an op that names something that is not there", () => {
    expect(applyOps(blocks(), [{ op: "set_checked", block_id: "nope", item_id: "i0", checked: true }])).toEqual(blocks());
  });
});

describe("the editor's save as point changes", () => {
  it("sends only what changed, and the order when it moved", () => {
    const before: Block[] = [checklist(2, 0), basket(), { id: "x", kind: "poll" }];
    const after: Block[] = [basket(), { ...checklist(2, 0), title: "Shopping" }, { id: "x", kind: "poll" }, { id: "n", kind: "markdown", text: "note" } as Block];
    const ops = diffBlocks(before, after);
    expect(ops).toEqual([
      { op: "update_block", block_id: "c1", patch: { title: "Shopping", items: checklist(2, 0).items } },
      { op: "add_block", block: after[3], index: 3 },
      { op: "reorder_blocks", ids: ["b1", "c1", "x", "n"] },
    ]);
    expect(diffBlocks(before, before)).toEqual([]);
    expect(diffBlocks(before, before.slice(1))).toEqual([{ op: "remove_block", block_id: "c1" }]);
  });
});

describe("copy as text", () => {
  const event = { title: "Grocery order #1", when: "Today · 10:00 – 10:30", description: "Leave at the door", fields: { store: "Small Turan" }, blocks: [basket(), checklist(2, 1)], tags: ["food"] };

  it("writes a checklist as boxes and a table as lines with its totals", () => {
    const text = eventAsText(event, undefined, false);
    expect(text).toContain("Grocery order #1\nToday · 10:00 – 10:30\n#food");
    expect(text).toContain("☑ Buckwheat 1 kg · 1 pc · 435");
    expect(text).toContain("☐ item 1");
    expect(text).toContain(`Total: ₸: 2,305${NBSP}₸, ✓: 1/3`);
    expect(text.trimEnd().endsWith("Leave at the door")).toBe(true);
  });

  it("writes Markdown with a heading, task boxes and a real table", () => {
    const text = eventAsText(event, undefined, true);
    expect(text.startsWith("# Grocery order #1")).toBe(true);
    expect(text).toContain("## Basket\n| Item | Qty | ₸ | ✓ |\n| --- | --- | ---: | --- |");
    expect(text).toContain("| Buckwheat 1 kg | 1 pc | 435");
    expect(text).toContain("- [x] item 0\n- [ ] item 1");
  });
});

describe("a type's fields", () => {
  it("labels them from the schema and keeps a value the schema dropped, marked stale", () => {
    const fields = typedFields(meal, { kcal: 518, meal: "Breakfast", fiber_g: 6 });
    expect(fields.map((f) => [f.key, f.text, f.stale])).toEqual([["kcal", `518${NBSP}kcal`, false], ["meal", "Breakfast", false], ["fiber_g", "6", true]]);
    expect(typedFields(undefined, { a: 1 })[0].stale).toBe(false);
  });

  it("reads the swipes and the statuses a type allows", () => {
    expect(swipeActions(meal)).toEqual({ right: "done", left: null });
    expect(swipeActions(undefined)).toEqual({ right: "done", left: "skipped" });
    expect(statusFlow({ ...meal, status_flow: ["done", "skipped"] })).toEqual(["planned", "done", "skipped"]);
  });
});
