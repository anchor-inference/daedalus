// @vitest-environment jsdom
// The primitives as the keyboard meets them. One rule runs through all of it (ui/README.md): Escape
// closes the top layer only, and the focus goes back to the control that opened it.
import { act, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "../../i18n";
import { ConfirmHost, confirmDialog } from "./confirm";
import { Combobox } from "./combobox";
import { Button, Field, IconButton } from "./controls";
import { FullscreenLayer } from "./fullscreen";
import { layerOpen } from "./layers";
import { Menu, MenuRow, OverflowMenu, type MenuItem } from "./menu";
import { Popover } from "./popover";
import { Segmented } from "./segmented";
import { Select } from "./select";
import { Sheet } from "./sheet";
import { Switch } from "./switch";
import { Tabs } from "./tabs";
import { ToastHost, dismissToast, toast } from "./toast";

// Words of an invented bakery, held in constants: hardcoded.test.ts reads `label="…"` and the
// text between tags in any source as words a reader would see.
const SETTINGS = "Menu settings";
const MODEL_FOR = "Model for this chat";
const PAGE_ACTIONS = "Page actions";
const ROW_ACTIONS = "Row actions";
const FAST = "Fast mode";
const FIND = "Find a model";
const PRICES = "Prices from the sheet";
const FOLDER = "Server folder";
const WINDOW = "The shop window";
const ALLOW = "Allow once";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  setLang("en");
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 });
});

/** Base UI moves the focus and finishes its transitions a moment after a popup mounts or goes. */
const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });
const key = (name: string, extra: KeyboardEventInit = {}) => act(async () => {
  (document.activeElement ?? document.body).dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...extra }));
});
const press = (el: Element) => act(async () => { (el as HTMLElement).click(); });
const active = () => document.activeElement as HTMLElement | null;

describe("Sheet", () => {
  function Harness({ onClosed }: { onClosed?: () => void }) {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" className="opener" onClick={() => setOpen(true)}>Open</button>
        {open && (
          <Sheet title={SETTINGS} onClose={() => { setOpen(false); onClosed?.(); }} footer={<button type="button" className="save">Save</button>}>
            <button type="button" className="ask" onClick={() => void confirmDialog({ title: "Delete?" })}>Ask</button>
          </Sheet>
        )}
        <ConfirmHost />
      </>
    );
  }

  it("takes the focus, joins the layers, and gives the focus back on Escape", async () => {
    await act(async () => root.render(<Harness />));
    const opener = host.querySelector<HTMLButtonElement>(".opener")!;
    opener.focus();
    await press(opener);
    await settle();
    const sheet = document.querySelector(".sheet-backdrop .sheet")!;
    expect(sheet).not.toBeNull();
    expect(sheet.getAttribute("role")).toBe("dialog");
    expect(sheet.getAttribute("aria-label")).toBe("Menu settings");
    expect(sheet.querySelector(".sheet-body .sheet-foot .save")).not.toBeNull();
    expect(sheet.contains(active())).toBe(true);
    expect(layerOpen()).toBe(true);
    expect(document.querySelector("body > .overlay-root .sheet")).toBe(sheet);
    await key("Escape");
    await settle();
    expect(document.querySelector(".sheet")).toBeNull();
    expect(layerOpen()).toBe(false);
    expect(active()).toBe(opener);
  });

  it("closes only the confirmation over it on Escape, and a press in the confirmation is not a press outside the sheet", async () => {
    const closed = vi.fn();
    await act(async () => root.render(<Harness onClosed={closed} />));
    await press(host.querySelector(".opener")!);
    await settle();
    await press(document.querySelector(".sheet .ask")!);
    await settle();
    const dialog = document.querySelector(".sheet-backdrop.confirm .dialog")!;
    expect(dialog.getAttribute("role")).toBe("alertdialog");
    await act(async () => {
      dialog.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
      dialog.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      dialog.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await settle();
    expect(document.querySelector(".sheet")).not.toBeNull();
    await key("Escape");
    await settle();
    expect(document.querySelector(".dialog")).toBeNull();
    expect(document.querySelector(".sheet")).not.toBeNull();
    expect(closed).not.toHaveBeenCalled();
    await key("Escape");
    await settle();
    expect(document.querySelector(".sheet")).toBeNull();
    expect(closed).toHaveBeenCalledOnce();
  });

  it("keeps a press inside it from reaching the row that declared it", async () => {
    const row = vi.fn();
    function Declared() {
      const [open, setOpen] = useState(true);
      return <div onClick={row}>{open && <Sheet title="Inside" onClose={() => setOpen(false)}><button type="button" className="inner">Inner</button></Sheet>}</div>;
    }
    await act(async () => root.render(<Declared />));
    await settle();
    await press(document.querySelector(".sheet .inner")!);
    expect(row).not.toHaveBeenCalled();
  });
});

describe("confirmDialog", () => {
  it("answers yes, no, and with no host no", async () => {
    expect(await confirmDialog({ title: "Nobody listening" })).toBe(false);
    await act(async () => root.render(<ConfirmHost />));
    let answer: Promise<boolean> = Promise.resolve(false);
    await act(async () => { answer = confirmDialog({ title: "Remove the photo?", action: "Remove", danger: true }); });
    await settle();
    const buttons = document.querySelectorAll<HTMLButtonElement>(".dialog-actions button");
    // The safe button takes the focus when the action destroys something.
    expect(active()).toBe(buttons[0]);
    await press(buttons[1]);
    expect(await answer).toBe(true);
    await act(async () => { answer = confirmDialog({ title: "Again?" }); });
    await settle();
    await key("Escape");
    expect(await answer).toBe(false);
  });

  it("asks a three-way question and answers with the value pressed", async () => {
    await act(async () => root.render(<ConfirmHost />));
    let answer: Promise<string | false> = Promise.resolve(false);
    await act(async () => { answer = confirmDialog({ title: "Leave the note?", choices: [{ label: "Discard", value: "discard", danger: true }, { label: "Save", value: "save" }] }); });
    await settle();
    const labels = [...document.querySelectorAll(".dialog-actions button")].map((b) => b.textContent);
    expect(labels).toEqual([t("common.cancel"), "Discard", "Save"]);
    await press([...document.querySelectorAll<HTMLButtonElement>(".dialog-actions button")].find((b) => b.textContent === "Save")!);
    expect(await answer).toBe("save");
  });
});

describe("Popover", () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    const anchor = useRef<HTMLButtonElement>(null);
    return (
      <>
        <button type="button" ref={anchor} className="anchor" onClick={() => setOpen((o) => !o)}>Model</button>
        {open && (
          <Popover anchor={anchor.current} onClose={() => setOpen(false)} label={MODEL_FOR} className="model-menu">
            <button type="button">Opus</button>
            <button type="button" aria-checked="true" role="menuitemradio">Sonnet</button>
            <button type="button">Haiku</button>
          </Popover>
        )}
      </>
    );
  }

  it("starts on its first control, walks its buttons with the arrows, and gives the focus back on Escape", async () => {
    await act(async () => root.render(<Harness />));
    const anchor = host.querySelector<HTMLButtonElement>(".anchor")!;
    anchor.focus();
    await press(anchor);
    await settle();
    const box = document.querySelector(".menu.pop.model-menu")!;
    expect(box.getAttribute("role")).toBe("menu");
    expect(active()?.textContent).toBe("Opus");
    await key("ArrowDown");
    expect(active()?.textContent).toBe("Sonnet");
    await key("End");
    expect(active()?.textContent).toBe("Haiku");
    await key("Home");
    expect(active()?.textContent).toBe("Opus");
    await key("Escape");
    await settle();
    expect(document.querySelector(".model-menu")).toBeNull();
    expect(active()).toBe(anchor);
  });

  it("is a dialog, not a menu, when it holds a field", async () => {
    function Field() {
      const anchor = useRef<HTMLButtonElement>(null);
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" ref={anchor} className="anchor" onClick={() => setOpen(true)}>Model</button>
          {open && <Popover anchor={anchor.current} onClose={() => setOpen(false)} label={MODEL_FOR} role="dialog" offset={20} className="field-box"><input aria-label={FIND} /></Popover>}
        </>
      );
    }
    await act(async () => root.render(<Field />));
    await press(host.querySelector(".anchor")!);
    await settle();
    const box = document.querySelector<HTMLElement>(".menu.pop.field-box")!;
    expect(box.getAttribute("role")).toBe("dialog");
    expect(box.getAttribute("aria-label")).toBe(MODEL_FOR);
    expect(active()?.tagName).toBe("INPUT");
  });

  it("is a sheet below 1024 px", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
    await act(async () => root.render(<Harness />));
    await press(host.querySelector(".anchor")!);
    await settle();
    expect(document.querySelector(".sheet-backdrop .sheet .menu.pop.in-sheet.model-menu")).not.toBeNull();
    expect(document.querySelector(".sheet")!.getAttribute("aria-label")).toBe("Model for this chat");
  });
});

describe("Menu", () => {
  const items = (spy: (what: string) => void, checked = false): MenuItem[] => [
    { group: "The page" },
    { label: "Rename", icon: "pen", kbd: "F2", onSelect: () => spy("rename") },
    { label: "Copy link", icon: "copy", ticks: true, onSelect: () => { spy("copy"); return true; } },
    { label: "Prices from the sheet", checked, onSelect: () => spy("prices") },
    { label: "Move to", icon: "folder", submenu: [{ label: "Bakery", onSelect: () => spy("bakery") }] },
    "-",
    { label: "Delete", icon: "trash", danger: true, onSelect: () => spy("delete") },
  ];

  it("opens on the ⋮ with the keyboard on the first command, rings on Tab, and returns to the ⋮ on Escape", async () => {
    const spy = vi.fn();
    await act(async () => root.render(<OverflowMenu items={items(spy)} label={PAGE_ACTIONS} />));
    const trigger = host.querySelector<HTMLButtonElement>("button[aria-haspopup='menu']")!;
    trigger.focus();
    await press(trigger);
    await settle();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const menu = document.querySelector(".menu[role='menu']")!;
    expect(menu.querySelector(".menu-group")?.textContent).toBe("The page");
    expect(menu.querySelector(".kbd")?.textContent).toBe("F2");
    expect(active()?.textContent).toContain("Rename");
    await key("End");
    expect(active()?.textContent).toContain("Delete");
    await key("Tab");
    expect(active()?.textContent).toContain("Rename");
    await key("Escape");
    await settle();
    expect(document.querySelector(".menu[role='menu']")).toBeNull();
    expect(active()).toBe(trigger);
    expect(spy).not.toHaveBeenCalled();
  });

  it("runs a command and closes, keeps a checkbox open, and ticks the ⋮ after a copy", async () => {
    const spy = vi.fn();
    await act(async () => root.render(<OverflowMenu items={items(spy)} label={PAGE_ACTIONS} />));
    const trigger = host.querySelector<HTMLButtonElement>("button[aria-haspopup='menu']")!;
    await press(trigger);
    await settle();
    await press([...document.querySelectorAll("[role='menuitemcheckbox']")][0]);
    expect(spy).toHaveBeenLastCalledWith("prices");
    expect(document.querySelector(".menu[role='menu']")).not.toBeNull();
    await press([...document.querySelectorAll<HTMLElement>("[role='menuitem']")].find((b) => b.textContent?.includes("Copy link"))!);
    await settle();
    expect(spy).toHaveBeenLastCalledWith("copy");
    expect(document.querySelector(".menu[role='menu']")).toBeNull();
    expect(trigger.querySelector(".ic-check")).not.toBeNull();
  });

  it("is a sheet of rows below 1024 px, destructive last, a submenu's commands under its name", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const spy = vi.fn();
    await act(async () => root.render(<Menu items={[{ label: "Delete", danger: true, onSelect: () => spy("delete") }, ...items(spy).slice(1, 2), ...items(spy).slice(4, 5)]} label={ROW_ACTIONS} preview={{ title: "Checkout page" }} onClose={() => {}} />));
    await settle();
    const sheet = document.querySelector(".sheet.menu-sheet")!;
    expect(sheet.querySelector(".sheet-preview")?.textContent).toBe("Checkout page");
    const rows = [...sheet.querySelectorAll(".menu.in-sheet .menu-row")].map((r) => r.textContent);
    expect(rows).toEqual(["RenameF2", "Bakery", "Delete"]);
    expect(sheet.querySelector(".menu-group")?.textContent).toBe("Move to");
  });
});

describe("MenuRow", () => {
  it("is a choice with a tick, or a setting with a switch", async () => {
    const flip = vi.fn();
    await act(async () => root.render(<><MenuRow label="Container" checked onSelect={() => {}} /><MenuRow label={FAST} toggle={{ checked: false, onChange: flip }} /></>));
    const [choice, setting] = host.querySelectorAll("button.menu-row");
    expect(choice.getAttribute("role")).toBe("menuitemradio");
    expect(choice.getAttribute("aria-checked")).toBe("true");
    expect(choice.querySelector(".menu-row-check .ic-check")).not.toBeNull();
    expect(setting.getAttribute("role")).toBe("menuitemcheckbox");
    await press(setting);
    expect(flip).toHaveBeenCalledWith(true);
  });
});

describe("Select", () => {
  it("opens over the page, picks with the keyboard, and closes on Escape without closing the sheet round it", async () => {
    const pick = vi.fn();
    const closed = vi.fn();
    await act(async () => root.render(
      <Sheet title="Voice" onClose={closed}>
        <Select label="Language" value="en" onChange={pick} options={[{ id: "en", label: "English" }, { id: "ru", label: "Russian", hint: "The owner's choice" }]} />
      </Sheet>,
    ));
    await settle();
    const trigger = document.querySelector<HTMLButtonElement>(".dropdown-btn")!;
    expect(trigger.getAttribute("aria-label")).toBe("Language: English");
    expect(trigger.textContent).toBe("English");
    trigger.focus();
    await key("ArrowDown");
    await settle();
    expect(document.querySelector(".dropdown-list")).not.toBeNull();
    await key("Escape");
    await settle();
    expect(document.querySelector(".dropdown-list")).toBeNull();
    expect(document.querySelector(".sheet")).not.toBeNull();
    expect(closed).not.toHaveBeenCalled();
    await press(trigger);
    await settle();
    await press(document.querySelector("[role='option'][data-value='ru']")!);
    expect(pick).toHaveBeenCalledWith("ru");
  });
});

describe("Combobox", () => {
  it("narrows by the caller's query, walks the matches from the field and takes one on Enter", async () => {
    const choose = vi.fn();
    function Harness() {
      const [query, setQuery] = useState("");
      const all = ["Opus", "Sonnet", "Haiku"];
      return <Combobox inline label={FIND} items={all.filter((m) => m.toLowerCase().includes(query.toLowerCase()))} query={query} onQuery={setQuery} onChoose={choose} />;
    }
    await act(async () => root.render(<Harness />));
    const field = host.querySelector<HTMLInputElement>(".combo-input")!;
    expect(field.getAttribute("aria-label")).toBe("Find a model");
    expect(host.querySelectorAll("[role='option']")).toHaveLength(3);
    field.focus();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "o");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect([...host.querySelectorAll("[role='option']")].map((o) => o.textContent)).toEqual(["Opus", "Sonnet"]);
    await key("ArrowDown");
    await key("Enter");
    expect(choose).toHaveBeenCalled();
  });
});

describe("Combobox rows and field", () => {
  it("hands the press that chose a row to the caller, and draws what is put beside the field", async () => {
    const choose = vi.fn();
    await act(async () => root.render(
      <Combobox inline label={FIND} items={["Opus", "Sonnet"]} query="" onQuery={() => {}} onChoose={choose} lead={<i className="lead" />} trail={<kbd className="trail">Esc</kbd>} itemClassName="pick" />,
    ));
    expect(host.querySelector(".combo-bar .lead")).not.toBeNull();
    expect(host.querySelector(".combo-bar .trail")?.textContent).toBe("Esc");
    expect(host.querySelectorAll(".combo-item.pick")).toHaveLength(2);
    await act(async () => {
      host.querySelectorAll<HTMLElement>(".combo-item")[1].dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }));
    });
    expect(choose).toHaveBeenCalledTimes(1);
    expect(choose.mock.calls[0][0]).toBe("Sonnet");
    expect((choose.mock.calls[0][1] as MouseEvent).ctrlKey).toBe(true);
  });

  it("labels each group, with a note at its far end where the caller has one", async () => {
    await act(async () => root.render(
      <Combobox inline label={FIND} items={[]} groups={[{ label: "Chats", items: ["Opus"], note: "searching" }, { label: "Actions", items: ["Sonnet"] }]} query="" onQuery={() => {}} onChoose={() => {}} />,
    ));
    expect([...host.querySelectorAll(".combo-group-label")].map((el) => el.textContent)).toEqual(["Chatssearching", "Actions"]);
    expect(host.querySelector(".combo-group-note")?.textContent).toBe("searching");
  });
});

describe("Tabs", () => {
  it("moves along with the arrows, opens on a press, and closes a tab on Delete", async () => {
    const change = vi.fn();
    const close = vi.fn();
    await act(async () => root.render(<Tabs variant="panel" label="Panel" value="overview" onChange={change}
      tabs={[{ id: "overview", label: "Overview", icon: "grid" }, { id: "changes", label: "Changes", count: 2 }, { id: "details", label: "Details", onClose: close }]} />));
    const tabs = host.querySelectorAll<HTMLElement>("[role='tab']");
    expect(tabs).toHaveLength(3);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[1].querySelector(".tab-count")?.textContent).toBe("2");
    await press(tabs[1]);
    expect(change).toHaveBeenCalledWith("changes");
    tabs[2].focus();
    await key("Delete");
    expect(close).toHaveBeenCalledOnce();
  });
});

describe("Segmented", () => {
  it("is a radio group whose arrows move between the choices, and a press on the chosen one changes nothing", async () => {
    const change = vi.fn();
    await act(async () => root.render(<Segmented label="View" value="list" onChange={change} options={[{ id: "list", label: "List" }, { id: "board", label: "Board", badge: <b>2</b> }]} />));
    const group = host.querySelector("[role='radiogroup']")!;
    expect(group.getAttribute("aria-label")).toBe("View");
    const radios = group.querySelectorAll<HTMLElement>("[role='radio']");
    expect(radios[0].getAttribute("aria-checked")).toBe("true");
    expect(radios[0].hasAttribute("aria-pressed")).toBe(false);
    await press(radios[0]);
    expect(change).not.toHaveBeenCalled();
    await press(radios[1]);
    expect(change).toHaveBeenCalledWith("board");
  });
});

describe("Switch", () => {
  it("is a switch button that says its state and flips on a press", async () => {
    const flip = vi.fn();
    await act(async () => root.render(<Switch checked={false} onChange={flip} label={PRICES} />));
    const control = host.querySelector<HTMLElement>("[role='switch']")!;
    expect(control.tagName).toBe("BUTTON");
    expect(control.classList.contains("switch")).toBe(true);
    expect(control.getAttribute("aria-checked")).toBe("false");
    expect(control.getAttribute("aria-label")).toBe("Prices from the sheet");
    await press(control);
    expect(flip).toHaveBeenCalledWith(true);
  });
});

describe("toast", () => {
  it("shows one line at a time, offers Undo, and can be taken away", async () => {
    expect(toast("Nobody listening")).toBe(0);
    await act(async () => root.render(<ToastHost />));
    const undo = vi.fn();
    await act(async () => { toast("First"); });
    let id = 0;
    await act(async () => { id = toast("The menu page was not published", { undo }); });
    await settle();
    const shown = document.querySelectorAll(".toast");
    expect(shown).toHaveLength(1);
    expect(shown[0].textContent).toContain("The menu page was not published");
    await press(shown[0].querySelector("button")!);
    expect(undo).toHaveBeenCalledOnce();
    await settle();
    await act(async () => { id = toast("Again"); });
    await settle();
    expect(document.querySelector(".toast")?.textContent).toContain("Again");
    await act(async () => dismissToast(id));
    await settle();
    expect(document.querySelector(".toast")).toBeNull();
  });
});

describe("Button and IconButton", () => {
  it("draw the classes every screen styles, and a busy button takes no second press", async () => {
    const press1 = vi.fn();
    await act(async () => root.render(<>
      <Button variant="primary" kbd="Enter" onClick={press1}>{ALLOW}</Button>
      <Button size="sm" busy onClick={press1}>Publishing</Button>
      <IconButton icon="bell" label="Inbox" badge={120} />
    </>));
    const [primary, busy] = host.querySelectorAll("button.btn");
    expect(primary.className).toBe("btn primary");
    expect(primary.querySelector(".kbd")?.textContent).toBe("Enter");
    expect(busy.className).toContain("small");
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    const icon = host.querySelector(".iconbtn")!;
    expect(icon.getAttribute("aria-label")).toBe("Inbox");
    expect(icon.querySelector(".iconbtn-badge")?.textContent).toBe("99+");
  });
});

describe("Field", () => {
  it("ties its label to the field and says what is wrong under it", async () => {
    await act(async () => root.render(<Field label={FOLDER} error="The server did not answer" defaultValue="/var/www" />));
    const input = host.querySelector("input")!;
    expect(host.querySelector("label")!.htmlFor).toBe(input.id);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(document.getElementById(input.getAttribute("aria-describedby")!)?.textContent).toBe("The server did not answer");
  });
});

describe("FullscreenLayer", () => {
  it("covers the window, and Escape gives the focus back", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return <><button type="button" className="opener" onClick={() => setOpen(true)}>Photo</button>{open && <FullscreenLayer label={WINDOW} onClose={() => setOpen(false)}><img alt="" /></FullscreenLayer>}</>;
    }
    await act(async () => root.render(<Harness />));
    const opener = host.querySelector<HTMLButtonElement>(".opener")!;
    opener.focus();
    await press(opener);
    await settle();
    expect(document.querySelector(".fullscreen-layer .fullscreen-title")?.textContent).toBe("The shop window");
    await key("Escape");
    await settle();
    expect(document.querySelector(".fullscreen-layer")).toBeNull();
    expect(active()).toBe(opener);
  });
});
