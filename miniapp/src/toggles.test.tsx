// @vitest-environment jsdom
// The access chip in the composer: what it says (the access, then whatever else is on), the switches
// in its menu — full access, YAGNI, autopilot — and the agent's language a row further down, whose
// languages open beside it and hand the screen a choice: "follow the default" is null, "off" is "".

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { AccessSelect, accessTitle, autopilotHint } from "./toggles";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the words", () => {
  beforeEach(() => setLang("en"));

  it("says the access first, then each switch that is on", () => {
    expect(accessTitle(false, false)).toBe("With checks");
    expect(accessTitle(true, true)).toBe("With checks · YAGNI · Autopilot");
    expect(accessTitle(false, false, true)).toBe("Full access");
    setLang("ru");
    expect(accessTitle(false, true, true)).toBe("Полный доступ · Автопилот");
  });
});

describe("the chip and its menu", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    // jsdom has no layout; the popover measures itself.
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  const chip = () => host.querySelector<HTMLButtonElement>(".composer-access")!;
  const rows = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".access-menu [data-toggle]"));
  const translation = { agent: "en", operator: "ru", active: true, explicit: true };

  it("holds the switches the session offers, in order, and is not drawn where it offers none", async () => {
    const status = { on: false, enabled: true, eligible: true, used: 0, allowed: 3 };
    await act(async () => root.render(<AccessSelect yagni={false} onYagni={() => {}} autopilot={status} onAutopilot={() => {}} bypass={false} onBypass={() => {}} />));
    expect(chip().querySelector(".access-word")?.textContent).toBe("With checks");
    await act(async () => chip().click());
    expect(document.querySelector(".menu.access-menu")).not.toBeNull();
    expect(rows().map((r) => r.dataset.toggle)).toEqual(["bypass", "yagni", "autopilot"]);
    expect(rows().map((r) => r.getAttribute("role"))).toEqual(["menuitemcheckbox", "menuitemcheckbox", "menuitemcheckbox"]);
    await act(async () => root.render(<AccessSelect />));
    expect(host.querySelector(".composer-access")).toBeNull();
  });

  it("switches YAGNI on at once, keeps the menu open, and says so on the chip", async () => {
    const onYagni = vi.fn();
    await act(async () => root.render(<AccessSelect yagni={false} onYagni={onYagni} />));
    await act(async () => chip().click());
    const toggle = document.querySelector<HTMLButtonElement>(".yagni-row")!;
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await act(async () => toggle.click());
    expect(onYagni).toHaveBeenCalledWith(true);
    expect(document.querySelector(".access-menu")).not.toBeNull();
    expect(document.querySelector(".yagni-row")!.getAttribute("aria-checked")).toBe("true");
    expect(chip().hasAttribute("data-yagni")).toBe(true);
    expect(chip().title).toBe("With checks · YAGNI");
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector(".access-menu")).toBeNull();
  });

  it("offers autopilot where the session may have it, and says how many turns it took", async () => {
    const onAutopilot = vi.fn();
    const status = { on: true, enabled: true, eligible: true, used: 2, allowed: 3 };
    await act(async () => root.render(<AccessSelect yagni={false} onYagni={() => {}} autopilot={status} onAutopilot={onAutopilot} />));
    expect(chip().hasAttribute("data-autopilot")).toBe(true);
    await act(async () => chip().click());
    const row = document.querySelector<HTMLButtonElement>(".autopilot-row")!;
    expect(row.getAttribute("aria-checked")).toBe("true");
    expect(row.textContent).toContain("2 of 3 turns used");
    await act(async () => row.click());
    expect(onAutopilot).toHaveBeenCalledWith(false);
    expect(autopilotHint({ ...status, enabled: false }, true)).toContain("Settings → Chat");
    // A session with a role of its own (an orchestrator, staff) is not offered the switch at all.
    await act(async () => root.render(<AccessSelect yagni={false} onYagni={() => {}} autopilot={{ ...status, eligible: false }} onAutopilot={onAutopilot} />));
    expect(document.querySelector(".autopilot-row")).toBeNull();
  });

  it("gives full access with one press and no confirmation, says what still holds, and wears the attention colour", async () => {
    const onBypass = vi.fn();
    await act(async () => root.render(<AccessSelect yagni={false} onYagni={() => {}} bypass={false} onBypass={onBypass} />));
    await act(async () => chip().click());
    const row = document.querySelector<HTMLButtonElement>(".bypass-row")!;
    expect(row.getAttribute("aria-checked")).toBe("false");
    expect(row.querySelector(".menu-row-t")?.textContent).toBe("Full access");
    // The line under the switch names what stays refused; nothing hides it behind a tooltip.
    const hint = row.querySelector(".menu-row-sub")!.textContent!;
    expect(hint).toContain("containers");
    expect(hint).toContain("sealed ports");
    await act(async () => row.click());
    expect(onBypass).toHaveBeenCalledWith(true);
    expect(document.querySelector(".confirm, .dialog")).toBeNull();
    expect(chip().classList.contains("bypass")).toBe(true);
    expect(chip().querySelector(".access-word")?.textContent).toBe("Full access");
  });

  it("names the agent's language, lists the default, off and every language but the operator's, and hands the choice back", async () => {
    const onAgentLang = vi.fn();
    await act(async () => root.render(<AccessSelect onAgentLang={onAgentLang} translation={translation} translationDefault="" />));
    // Translating shows on the chip as the pair.
    expect(chip().querySelector(".access-lang")?.textContent).toBe("EN ⇄ RU");
    await act(async () => chip().click());
    const entry = document.querySelector<HTMLButtonElement>(".access-menu [data-action='language']")!;
    expect(entry.textContent).toContain("Agent language");
    expect(entry.textContent).toContain("EN ⇄ RU");
    await act(async () => entry.click());
    const langs = Array.from(document.querySelectorAll<HTMLButtonElement>(".agentlang-menu .agentlang-row")).map((r) => r.dataset.lang);
    expect(langs.slice(0, 3)).toEqual(["default", "off", "en"]);
    expect(langs).not.toContain("ru");
    expect(document.querySelector(".agentlang-row[data-lang='en']")!.getAttribute("aria-checked")).toBe("true");
    await act(async () => document.querySelector<HTMLButtonElement>(".agentlang-row[data-lang='off']")!.click());
    expect(onAgentLang).toHaveBeenCalledWith({ agent: "" });
    expect(document.querySelector(".access-menu")).toBeNull();
  });

  it("follows the default with null, named after the settings' language", async () => {
    const onAgentLang = vi.fn();
    await act(async () => root.render(<AccessSelect onAgentLang={onAgentLang} translation={{ agent: "", operator: "ru", active: false, explicit: true }} translationDefault="en" />));
    await act(async () => chip().click());
    await act(async () => document.querySelector<HTMLButtonElement>(".access-menu [data-action='language']")!.click());
    const follow = document.querySelector<HTMLButtonElement>(".agentlang-row[data-lang='default']")!;
    expect(follow.textContent).toContain("As in Settings: English");
    await act(async () => follow.click());
    expect(onAgentLang).toHaveBeenCalledWith({ agent: null });
  });

  it("is a sheet on a phone, with the shield alone on the chip, in Russian too", async () => {
    setLang("ru");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    await act(async () => root.render(<AccessSelect yagni={false} onYagni={() => {}} compact />));
    expect(chip().querySelector(".access-word")).toBeNull();
    await act(async () => chip().click());
    expect(document.querySelector(".sheet .menu.access-menu")).not.toBeNull();
    expect(document.querySelector(".yagni-row")!.textContent).toContain("Минимальное изменение");
  });
});
