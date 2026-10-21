// @vitest-environment jsdom
// The mode chip in the composer: the list of modes, the YAGNI switch beside them, the mark
// the chip wears while it is on, and the sheet a phone gets instead of the popover.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { ModeSelect, autopilotHint, modeChip, modeHint, modeName } from "./modeselect";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MODES = [
  { name: "plan", description: "read and plan only" },
  { name: "review", description: "an operator's own mode" },
];

describe("the words", () => {
  beforeEach(() => setLang("en"));

  it("names the plain agent, the built-in modes in the reader's language, and an operator's own as written", () => {
    expect(modeName("")).toBe("Agent");
    expect(modeName("plan")).toBe("Plan");
    expect(modeName("review")).toBe("review");
    setLang("ru");
    expect(modeName("")).toBe("Агент");
    expect(modeName("careful")).toBe("Осторожный");
  });

  it("describes a mode the app knows in its own words and any other in the host's", () => {
    expect(modeHint(null)).toContain("no extra limits");
    expect(modeHint({ name: "review", description: "an operator's own mode" })).toBe("an operator's own mode");
  });

  it("keeps the label to the mode's name and puts the switch in the tooltip", () => {
    expect(modeChip("plan", false)).toEqual({ label: "Plan", title: "Plan" });
    expect(modeChip("", true)).toEqual({ label: "Agent", title: "Agent · YAGNI" });
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

  it("lists the agent and every configured mode, marks the current one, and picks another", async () => {
    const onChooseMode = vi.fn();
    await act(async () => root.render(<ModeSelect mode="plan" modes={MODES} yagni={false} onChooseMode={onChooseMode} onYagni={() => {}} sheet={false} />));
    expect(chip().textContent).toBe("Plan");
    await act(async () => chip().click());
    expect(document.querySelector(".mode-menu")).not.toBeNull();
    const radios = rows().filter((r) => r.getAttribute("role") === "menuitemradio");
    // Auto is the host's own and always offered first, whatever the configured modes are.
    expect(radios.map((r) => r.querySelector(".truncate")?.textContent)).toEqual(["Auto", "Agent", "Plan", "review"]);
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["false", "false", "true", "false"]);
    await act(async () => radios[1].click());
    expect(onChooseMode).toHaveBeenCalledWith("");
    expect(document.querySelector(".mode-menu")).toBeNull();
  });

  it("names the auto mode on the chip and picks it", async () => {
    const onChooseMode = vi.fn();
    await act(async () => root.render(<ModeSelect mode="" modes={MODES} yagni={false} onChooseMode={onChooseMode} onYagni={() => {}} sheet={false} />));
    await act(async () => chip().click());
    await act(async () => rows()[0].click());
    expect(onChooseMode).toHaveBeenCalledWith("auto");
    await act(async () => root.render(<ModeSelect mode="auto" modes={MODES} yagni={false} onChooseMode={onChooseMode} onYagni={() => {}} sheet={false} />));
    expect(chip().textContent).toBe("Auto");
  });

  it("switches YAGNI on at once, keeps the menu open, and marks the chip", async () => {
    const onYagni = vi.fn();
    await act(async () => root.render(<ModeSelect mode="" modes={MODES} yagni={false} onChooseMode={() => {}} onYagni={onYagni} sheet={false} />));
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
    // Escape closes it, as every layer does.
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector(".mode-menu")).toBeNull();
  });

  it("reflects the host: a session that comes back with the switch on wears the mark", async () => {
    await act(async () => root.render(<ModeSelect mode="" modes={[]} yagni onChooseMode={() => {}} onYagni={() => {}} sheet={false} />));
    expect(chip().classList.contains("yagni")).toBe(true);
    expect(chip().querySelector(".mode-yagni")?.textContent).toBe("YAGNI");
  });

  it("offers autopilot beside YAGNI where the session may have it, and says how many turns it took", async () => {
    const onAutopilot = vi.fn();
    const status = { on: true, enabled: true, eligible: true, used: 2, allowed: 3 };
    await act(async () => root.render(<ModeSelect mode="" modes={[]} yagni={false} onChooseMode={() => {}} onYagni={() => {}} autopilot={status} onAutopilot={onAutopilot} sheet={false} />));
    await act(async () => chip().click());
    const row = document.querySelector<HTMLButtonElement>(".autopilot-row")!;
    expect(row.getAttribute("aria-checked")).toBe("true");
    expect(row.textContent).toContain("2 of 3 turns used");
    await act(async () => row.click());
    expect(onAutopilot).toHaveBeenCalledWith(false);
    expect(autopilotHint({ ...status, enabled: false }, true)).toContain("Settings → Chat");
    // A session with a role of its own (an orchestrator, staff) is not offered the switch at all.
    await act(async () => root.render(<ModeSelect mode="" modes={[]} yagni={false} onChooseMode={() => {}} onYagni={() => {}} autopilot={{ ...status, eligible: false }} onAutopilot={onAutopilot} sheet={false} />));
    expect(document.querySelector(".autopilot-row")).toBeNull();
  });

  it("opens a sheet on a phone, in Russian too", async () => {
    setLang("ru");
    await act(async () => root.render(<ModeSelect mode="" modes={MODES} yagni={false} onChooseMode={() => {}} onYagni={() => {}} sheet />));
    await act(async () => chip().click());
    expect(document.querySelector(".sheet.mode-sheet")).not.toBeNull();
    expect(document.querySelector(".mode-menu")).toBeNull();
    expect(document.querySelector(".yagni-row")!.textContent).toContain("Минимальное изменение");
  });
});
