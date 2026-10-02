// @vitest-environment jsdom
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ContextMenuHost, useContextActions } from "./context-menu";
import { t } from "../i18n";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let mount: HTMLDivElement;
beforeEach(() => { vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} }); mount = document.createElement("div"); document.body.append(mount); root = createRoot(mount); });
afterEach(() => { act(() => root.unmount()); mount.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const fire = (target: HTMLElement, event: Event) => act(() => { target.dispatchEvent(event); });

it("right click runs the row command and Escape restores keyboard focus without activating it", () => {
  const rename = vi.fn();
  function Row() {
    const ref = useRef<HTMLButtonElement>(null);
    useContextActions(ref, [{ label:"Rename", onSelect:rename }, { label:"Remove", disabled:true, onSelect:vi.fn() }]);
    return <button ref={ref}>Conversation</button>;
  }
  act(() => root.render(<><Row /><ContextMenuHost /></>));
  const row = mount.querySelector("button")!;
  row.focus();
  const right = new MouseEvent("contextmenu", { bubbles:true, cancelable:true, clientX:1000, clientY:700 });
  fire(row, right);
  expect(right.defaultPrevented).toBe(true);
  expect(document.activeElement?.textContent).toBe("Rename");
  fire(document.activeElement as HTMLElement, new KeyboardEvent("keydown", { key:"Escape", bubbles:true }));
  expect(document.querySelector('[role="menu"]')).toBeNull();
  expect(document.activeElement).toBe(row);
  expect(rename).not.toHaveBeenCalled();
  fire(row, new KeyboardEvent("keydown", { key:"F10", shiftKey:true, bubbles:true, cancelable:true }));
  fire(document.querySelector('[role="menuitem"]')!, new MouseEvent("click", { bubbles:true }));
  expect(rename).toHaveBeenCalledOnce();
});

it("text selection gets editing commands while password selection never gets copy or cut", () => {
  act(() => root.render(<><input defaultValue="draft" /><input type="password" defaultValue="secret" /><ContextMenuHost /></>));
  const inputs = mount.querySelectorAll("input");
  inputs[0].setSelectionRange(0, 5);
  fire(inputs[0], new MouseEvent("contextmenu", { bubbles:true, cancelable:true }));
  const labels = () => Array.from(document.querySelectorAll('[role="menuitem"]')).map((node) => node.textContent);
  expect(labels()).toContain(t("common.copy"));
  expect(labels()).toContain(t("context.cut"));
  fire(document.activeElement as HTMLElement, new KeyboardEvent("keydown", { key:"Escape", bubbles:true }));
  inputs[1].setSelectionRange(0, 6);
  fire(inputs[1], new MouseEvent("contextmenu", { bubbles:true, cancelable:true }));
  expect(labels()).not.toContain(t("common.copy"));
  expect(labels()).not.toContain(t("context.cut"));
  expect(labels()).toContain(t("context.paste"));
});
