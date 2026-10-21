// @vitest-environment jsdom
// The toggles chip in the composer: the YAGNI and autopilot switches, the marks the chip wears
// while they are on, and the sheet a phone gets instead of the popover.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { TogglesSelect, autopilotHint, togglesTitle } from "./toggles";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the words", () => {
  beforeEach(() => setLang("en"));

  it("names the agent in the tooltip, then each switch that is on", () => {
    expect(togglesTitle(false, false)).toBe("Agent");
    expect(togglesTitle(true, true)).toBe("Agent · YAGNI · Autopilot");
    setLang("ru");
    expect(togglesTitle(false, true)).toBe("Агент · Автопилот");
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
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  const chip = () => host.querySelector<HTMLButtonElement>(".composer-mode")!;
  const rows = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".mode-row"));

  it("holds only the two switches and names the agent while both are off", async () => {
    const status = { on: false, enabled: true, eligible: true, used: 0, allowed: 3 };
    await act(async () => root.render(<TogglesSelect yagni={false} onYagni={() => {}} autopilot={status} onAutopilot={() => {}} sheet={false} />));
    expect(chip().textContent).toBe("Agent");
    await act(async () => chip().click());
    expect(document.querySelector(".mode-menu")).not.toBeNull();
    expect(rows().map((r) => r.getAttribute("role"))).toEqual(["menuitemcheckbox", "menuitemcheckbox"]);
    expect(rows().map((r) => r.querySelector(".truncate")?.textContent)).toEqual(["YAGNI", "Autopilot"]);
  });

  it("switches YAGNI on at once, keeps the menu open, and marks the chip", async () => {
    const onYagni = vi.fn();
    await act(async () => root.render(<TogglesSelect yagni={false} onYagni={onYagni} sheet={false} />));
    expect(chip().classList.contains("yagni")).toBe(false);
    await act(async () => chip().click());
    const toggle = document.querySelector<HTMLButtonElement>(".yagni-row")!;
    expect(toggle.getAttribute("role")).toBe("menuitemcheckbox");
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await act(async () => toggle.click());
    expect(onYagni).toHaveBeenCalledWith(true);
    expect(document.querySelector(".mode-menu")).not.toBeNull();
    expect(document.querySelector(".yagni-row")!.getAttribute("aria-checked")).toBe("true");
    expect(chip().classList.contains("yagni")).toBe(true);
    expect(chip().getAttribute("title")).toBe("Agent · YAGNI");
    expect(chip().textContent).toBe("YAGNI");
    // Escape closes it, as every layer does.
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector(".mode-menu")).toBeNull();
  });

  it("reflects the host: a session that comes back with the switch on wears the mark", async () => {
    await act(async () => root.render(<TogglesSelect yagni onYagni={() => {}} sheet={false} />));
    expect(chip().classList.contains("yagni")).toBe(true);
    expect(chip().querySelector(".mode-yagni")?.textContent).toBe("YAGNI");
  });

  it("offers autopilot beside YAGNI where the session may have it, and says how many turns it took", async () => {
    const onAutopilot = vi.fn();
    const status = { on: true, enabled: true, eligible: true, used: 2, allowed: 3 };
    await act(async () => root.render(<TogglesSelect yagni={false} onYagni={() => {}} autopilot={status} onAutopilot={onAutopilot} sheet={false} />));
    await act(async () => chip().click());
    const row = document.querySelector<HTMLButtonElement>(".autopilot-row")!;
    expect(row.getAttribute("aria-checked")).toBe("true");
    expect(row.textContent).toContain("2 of 3 turns used");
    expect(chip().classList.contains("autopilot")).toBe(true);
    expect(chip().querySelector(".mode-autopilot")?.textContent).toBe("Autopilot");
    await act(async () => row.click());
    expect(onAutopilot).toHaveBeenCalledWith(false);
    expect(autopilotHint({ ...status, enabled: false }, true)).toContain("Settings → Chat");
    // A session with a role of its own (an orchestrator, staff) is not offered the switch at all.
    await act(async () => root.render(<TogglesSelect yagni={false} onYagni={() => {}} autopilot={{ ...status, eligible: false }} onAutopilot={onAutopilot} sheet={false} />));
    expect(document.querySelector(".autopilot-row")).toBeNull();
  });

  it("opens a sheet on a phone, in Russian too", async () => {
    setLang("ru");
    await act(async () => root.render(<TogglesSelect yagni={false} onYagni={() => {}} sheet />));
    await act(async () => chip().click());
    expect(document.querySelector(".sheet.mode-sheet")).not.toBeNull();
    expect(document.querySelector(".mode-menu")).toBeNull();
    expect(document.querySelector(".yagni-row")!.textContent).toContain("Минимальное изменение");
  });
});
