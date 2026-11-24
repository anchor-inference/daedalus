// @vitest-environment jsdom
// The in-place selection list: search narrows it, a group or everything found is taken at once, a
// shift-click takes a range, the chosen list removes and reorders, a missing id stays visible, the
// default state offers its way back, and the single variant shows its choice and picks in place.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { SelectionList, SelectionSingle, matchItems, rangeIds, setMany, type SelectionItem } from "./selection";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ITEMS: SelectionItem[] = [
  { id: "opus", label: "Claude Opus", detail: "Anthropic · claude-opus", facts: ["200k context"], group: "anthropic" },
  { id: "sonnet", label: "Claude Sonnet", detail: "Anthropic · claude-sonnet", group: "anthropic" },
  { id: "flash", label: "DeepSeek Flash", detail: "DeepSeek · deepseek-v4.1-flash", group: "deepseek" },
  { id: "pro", label: "DeepSeek Pro", detail: "DeepSeek · deepseek-v4.1-pro", group: "deepseek" },
  { id: "grok", label: "Grok 4.7", detail: "xAI · grok-4.7", group: "xai", disabled: true, hint: "no key" },
];
const GROUPS = [{ id: "anthropic", label: "Anthropic" }, { id: "deepseek", label: "DeepSeek" }, { id: "xai", label: "xAI" }];

describe("the helpers", () => {
  it("match every word of the query anywhere in an item's words", () => {
    expect(matchItems(ITEMS, "deepseek").map((i) => i.id)).toEqual(["flash", "pro"]);
    expect(matchItems(ITEMS, "claude son").map((i) => i.id)).toEqual(["sonnet"]);
    expect(matchItems(ITEMS, "200k").map((i) => i.id)).toEqual(["opus"]);
    expect(matchItems(ITEMS, "  ").length).toBe(ITEMS.length);
  });

  it("add after what is chosen, keep the order, and take out", () => {
    expect(setMany(["pro"], ["opus", "pro"], true)).toEqual(["pro", "opus"]);
    expect(setMany(["pro", "opus"], ["pro"], false)).toEqual(["opus"]);
  });

  it("take a range in either direction, both ends included", () => {
    expect(rangeIds(ITEMS, 1, 3)).toEqual(["sonnet", "flash", "pro"]);
    expect(rangeIds(ITEMS, 3, 1)).toEqual(["sonnet", "flash", "pro"]);
  });
});

describe("the list", () => {
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

  function render(value: string[], onChange = vi.fn(), extra: Partial<Parameters<typeof SelectionList>[0]> = {}) {
    act(() => root.render(<SelectionList label="Models" items={ITEMS} groups={GROUPS} value={value} onChange={onChange} {...extra} />));
    return onChange;
  }
  const row = (id: string) => host.querySelector<HTMLElement>(`.sel-list [data-id="${id}"]`)!;
  const search = () => host.querySelector<HTMLInputElement>(".sel-search-field")!;
  function type(text: string) {
    const input = search();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => { setter.call(input, text); input.dispatchEvent(new Event("input", { bubbles: true })); });
  }
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === text)!;
  const named = (name: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.getAttribute("aria-label") === name)!;

  it("draws rows with their detail and facts under their groups, and ticks the chosen", () => {
    render(["flash"]);
    expect([...host.querySelectorAll(".sel-group-name")].map((g) => g.textContent)).toEqual(["Anthropic", "DeepSeek", "xAI"]);
    expect(row("opus").textContent).toContain("Anthropic · claude-opus");
    expect(row("opus").textContent).toContain("200k context");
    expect(row("flash").getAttribute("aria-selected")).toBe("true");
    expect(row("opus").getAttribute("aria-selected")).toBe("false");
    expect(host.querySelector(".sel-count")!.textContent).toBe("1 of 5 chosen");
  });

  it("narrows to the search and selects or clears only what it found", () => {
    const onChange = render(["opus"]);
    type("deepseek");
    expect([...host.querySelectorAll(".sel-list [role=option]")].map((o) => (o as HTMLElement).dataset.id)).toEqual(["flash", "pro"]);
    expect(host.querySelector(".sel-count")!.textContent).toBe("2 of 5 match");
    act(() => button("Select all 2 found").click());
    expect(onChange).toHaveBeenLastCalledWith(["opus", "flash", "pro"]);
    type("zzz");
    expect(host.querySelector(".sel-list")!.textContent).toContain("Nothing matches “zzz”");
  });

  it("selects everything choosable, leaving out a disabled row, and clears", () => {
    const onChange = render([]);
    act(() => button("Select all").click());
    expect(onChange).toHaveBeenLastCalledWith(["opus", "sonnet", "flash", "pro"]);
    render(["opus", "pro"], onChange);
    act(() => button("Clear").click());
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("takes and drops a whole group", () => {
    const onChange = render(["opus"]);
    const group = host.querySelectorAll<HTMLElement>(".sel-group")[1];
    act(() => group.querySelector<HTMLButtonElement>("button")!.click());
    expect(onChange).toHaveBeenLastCalledWith(["opus", "flash", "pro"]);
    render(["opus", "flash", "pro"], onChange);
    const full = host.querySelectorAll<HTMLElement>(".sel-group")[1];
    expect(full.textContent).toContain("2 of 2");
    act(() => full.querySelector<HTMLButtonElement>("button")!.click());
    expect(onChange).toHaveBeenLastCalledWith(["opus"]);
  });

  it("toggles a row on click and takes a range on shift-click, skipping a disabled row", () => {
    const onChange = render([]);
    act(() => row("opus").click());
    expect(onChange).toHaveBeenLastCalledWith(["opus"]);
    // The parent saves; the list is drawn again with the new value, then the range is shift-clicked.
    render(["opus"], onChange);
    act(() => row("grok").dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true })));
    expect(onChange).toHaveBeenLastCalledWith(["opus", "sonnet", "flash", "pro"]);
  });

  it("does not take a disabled row on its own", () => {
    const onChange = render([]);
    act(() => row("grok").click());
    expect(onChange).not.toHaveBeenCalled();
    expect(row("grok").getAttribute("aria-disabled")).toBe("true");
  });

  it("answers the keyboard: down from the search, space toggles, shift+down extends, ctrl+a takes all", () => {
    const onChange = render([]);
    act(() => search().dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    const list = host.querySelector<HTMLElement>(".sel-list")!;
    expect(document.activeElement).toBe(list);
    expect(list.getAttribute("aria-activedescendant")).toBe(row("opus").id);
    act(() => list.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true })));
    expect(onChange).toHaveBeenLastCalledWith(["opus"]);
    render(["opus"], onChange);
    act(() => list.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", shiftKey: true, bubbles: true })));
    expect(onChange).toHaveBeenLastCalledWith(["opus", "sonnet"]);
    act(() => list.dispatchEvent(new KeyboardEvent("keydown", { key: "a", ctrlKey: true, bubbles: true })));
    expect(onChange).toHaveBeenLastCalledWith(["opus", "sonnet", "flash", "pro"]);
  });

  it("lists the chosen below with a remove for each, and keeps a missing id visible", () => {
    const onChange = render(["pro", "gone"]);
    const chosen = [...host.querySelectorAll<HTMLElement>(".sel-chosen-list > li")];
    expect(chosen.map((li) => li.dataset.id)).toEqual(["pro", "gone"]);
    expect(chosen[1].textContent).toContain("No longer configured");
    act(() => named("Remove gone").click());
    expect(onChange).toHaveBeenLastCalledWith(["pro"]);
  });

  it("says when nothing is chosen", () => {
    render([], vi.fn(), { emptyChosen: "No model is allowed" });
    expect(host.querySelector(".sel-chosen")!.textContent).toContain("No model is allowed");
  });

  it("reorders an ordered choice", () => {
    const onChange = render(["opus", "flash", "pro"], vi.fn(), { ordered: true });
    expect([...host.querySelectorAll(".sel-order")].map((o) => o.textContent)).toEqual(["1", "2", "3"]);
    act(() => named("Move DeepSeek Flash up").click());
    expect(onChange).toHaveBeenLastCalledWith(["flash", "opus", "pro"]);
    act(() => named("Move DeepSeek Flash down").click());
    expect(onChange).toHaveBeenLastCalledWith(["opus", "pro", "flash"]);
  });

  it("shows the default state and offers the way back to it", () => {
    const onUse = vi.fn();
    render(["opus"], vi.fn(), { inherit: { active: false, label: "Default from Settings: All models", onUse } });
    expect(host.querySelector(".sel-inherit")!.textContent).toContain("Has its own choice");
    act(() => button("Use the default").click());
    expect(onUse).toHaveBeenCalled();
    render([], vi.fn(), { inherit: { active: true, label: "Default from Settings: All models", onUse } });
    expect(host.querySelector(".sel-inherit")!.textContent).toContain("Uses the default");
    expect(button("Use the default")).toBeUndefined();
  });
});

describe("the single choice", () => {
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

  it("shows the choice as its full row, opens the list in place, and picks with the keyboard", () => {
    const onChange = vi.fn();
    act(() => root.render(<SelectionSingle label="Model" items={ITEMS} groups={GROUPS} value="flash" onChange={onChange} defaultOption={{ label: "Automatic" }} />));
    expect(host.querySelector(".sel-current")!.textContent).toContain("DeepSeek Flash");
    expect(host.querySelector(".sel-current")!.textContent).toContain("deepseek-v4.1-flash");
    expect(host.querySelector(".sel-list")).toBeNull();
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const options = [...host.querySelectorAll<HTMLElement>("[role=option]")];
    expect(options[0].dataset.id).toBe("");
    expect(options.find((o) => o.dataset.id === "flash")!.getAttribute("aria-selected")).toBe("true");
    const input = host.querySelector<HTMLInputElement>(".sel-search-field")!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => { setter.call(input, "sonnet"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    const list = host.querySelector<HTMLElement>(".sel-list")!;
    act(() => list.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(onChange).toHaveBeenCalledWith("sonnet");
    expect(host.querySelector(".sel-list")).toBeNull();
  });

  it("puts the focus in the search on opening, and Escape closes the list", () => {
    act(() => root.render(<SelectionSingle label="Model" items={ITEMS} value="flash" onChange={vi.fn()} />));
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const input = host.querySelector<HTMLInputElement>(".sel-search-field")!;
    expect(document.activeElement).toBe(input);
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(host.querySelector(".sel-list")).toBeNull();
  });

  it("empties the search on the first Escape and closes the list on the second", () => {
    act(() => root.render(<SelectionSingle label="Model" items={ITEMS} value="flash" onChange={vi.fn()} />));
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const input = host.querySelector<HTMLInputElement>(".sel-search-field")!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => { setter.call(input, "sonnet"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(host.querySelector<HTMLInputElement>(".sel-search-field")!.value).toBe("");
    expect(host.querySelector(".sel-list")).not.toBeNull();
    act(() => input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(host.querySelector(".sel-list")).toBeNull();
  });

  it("leaves the current row unmarked when a press focuses the list, so the list does not scroll under the pointer", () => {
    const onChange = vi.fn();
    act(() => root.render(<SelectionSingle label="Model" items={ITEMS} value="pro" onChange={onChange} />));
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const list = host.querySelector<HTMLElement>(".sel-list")!;
    const row = host.querySelector<HTMLElement>('[role=option][data-id="opus"]')!;
    act(() => { row.dispatchEvent(new Event("pointerdown", { bubbles: true })); list.focus(); });
    expect(list.querySelector(".sel-row.active")).toBeNull();
    act(() => row.click());
    expect(onChange).toHaveBeenCalledWith("opus");
  });

  it("marks the current row when the keyboard brings the focus to the list", () => {
    act(() => root.render(<SelectionSingle label="Model" items={ITEMS} value="pro" onChange={vi.fn()} />));
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const list = host.querySelector<HTMLElement>(".sel-list")!;
    act(() => list.focus());
    expect(list.querySelector(".sel-row.active")!.getAttribute("data-id")).toBe("pro");
  });

  it("picks the default row as the empty value", () => {
    const onChange = vi.fn();
    act(() => root.render(<SelectionSingle label="Model" items={ITEMS} value="flash" onChange={onChange} defaultOption={{ label: "Automatic" }} />));
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    act(() => host.querySelector<HTMLElement>('[role=option][data-id=""]')!.click());
    expect(onChange).toHaveBeenCalledWith("");
  });
});
