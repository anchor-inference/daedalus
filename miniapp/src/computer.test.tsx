// @vitest-environment jsdom
// The Computer control in each of its states: nothing outside the native application; the grant form
// when nobody holds the computer; the banner with Stop while this conversation does; Resume only while
// the operator's own hand has paused the agent; another conversation's grant said as such; the macOS
// permission rows with Ask and the settings link; and a refusal shown in its words, never its code.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { prime } from "./store";
import { COMPUTER, COMPUTER_APPS, ComputerBanner, ComputerButton, ComputerSheet, computerView, refusalText, timeLeft, useComputer, type ComputerState } from "./computer";
import { ApiError } from "./api";

const SESSION = "s1";

function state(extra: Partial<ComputerState> = {}): ComputerState {
  return {
    native: true, available: true, os: "linux", session_type: "x11",
    permissions: { screen: "not_applicable", accessibility: "not_applicable", input_monitoring: "not_applicable" },
    secure: { locked: false }, lease: { active: false }, human: { paused: false }, ...extra,
  };
}

function held(session: string, extra: Partial<ComputerState> = {}): ComputerState {
  return state({ lease: { active: true, remaining_ms: 12 * 60_000, lease: { id: "l1", session, scope: { kind: "window", window: "10" } } }, ...extra });
}

const APPS = {
  apps: [
    { id: "com.example.editor", name: "Editor", reachable: true, windows: [{ id: "10", title: "notes.txt" }, { id: "14", title: "<b>todo</b>" }] },
    { id: "pid:504", name: "setup", reachable: false, elevated: true, windows: [{ id: "13", title: "Administrator: setup" }] },
  ],
};

describe("where a conversation stands", () => {
  it("reads the state", () => {
    expect(computerView(undefined, SESSION)).toBe("hidden");
    expect(computerView(state({ native: false, available: false }), SESSION)).toBe("hidden");
    expect(computerView(state({ available: false, reason: "no daemon" }), SESSION)).toBe("unavailable");
    expect(computerView(state(), SESSION)).toBe("free");
    expect(computerView(held(SESSION), SESSION)).toBe("mine");
    expect(computerView(held(SESSION, { human: { paused: true } }), SESSION)).toBe("paused");
    expect(computerView(held("s2"), SESSION)).toBe("other");
  });

  it("says the time left rounded up, and a refusal by its explanation", () => {
    setLang("en");
    expect(timeLeft(61_000)).toBe("2 min left");
    expect(timeLeft(40_200)).toBe("41 s left");
    const refusal = new ApiError(409, "x", { error: { code: 1203, message: "protected", data: null, explanation: "That is one of Daedalus's own windows." } });
    expect(refusalText(refusal)).toBe("That is one of Daedalus's own windows.");
    expect(refusalText(new ApiError(503, "the desktop daemon is not running"))).toBe("The desktop daemon is not running");
  });

  it("says a refusal in the reader's language by its key, and in English only for a key it does not know", () => {
    setLang("ru");
    const words = (key: string, vars?: Record<string, number>) => new ApiError(409, "x", { error: { code: 1200, message: "", data: null, explanation: "The time ran out.", words: { key, vars } } });
    expect(refusalText(words("cu.refusal.no_lease.expired"))).toBe("Время, выданное этому разговору, истекло. Чтобы продолжить, дайте управление снова.");
    expect(refusalText(words("cu.refusal.unknown", { code: 4242 }))).toBe("Рабочий стол отказал в запросе (код 4242).");
    expect(refusalText(words("cu.refusal.from_a_newer_host"))).toBe("The time ran out.");
    setLang("en");
  });
});

describe("the control", () => {
  let host: HTMLDivElement;
  let root: Root;
  let current: ComputerState;
  let sent: { url: string; method: string; body: unknown }[];
  let refuse: Record<string, unknown>;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    sent = [];
    refuse = {};
    current = state();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const path = String(url);
      if (method !== "GET") sent.push({ url: path, method, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (refuse[path]) return new Response(JSON.stringify(refuse[path]), { status: 409, headers: { "Content-Type": "application/json" } });
      const reply = path.endsWith(COMPUTER_APPS) ? APPS : path.endsWith(COMPUTER) ? current : {};
      return new Response(JSON.stringify(reply), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  function Head() {
    const computer = useComputer(SESSION, false);
    return <ComputerButton view={computer.view} onOpen={() => undefined} />;
  }

  async function show(s: ComputerState, node: React.ReactNode) {
    current = s;
    prime(COMPUTER, s);
    prime(COMPUTER_APPS, APPS);
    await act(async () => root.render(<>{node}</>));
  }

  const sheet = () => document.body.querySelector<HTMLElement>(".computer-sheet");
  const button = (scope: ParentNode, text: string) => [...scope.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);

  it("draws nothing outside the native application", async () => {
    await show(state({ native: false, available: false, reason: "only in the desktop app" }), <><Head /><ComputerBanner sessionId={SESSION} /></>);
    expect(host.innerHTML).toBe("");
  });

  it("offers the grant form when nobody holds the computer, and grants one window", async () => {
    await show(state(), <><Head /><ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} /></>);
    expect(host.querySelector<HTMLElement>(".computer-button")?.dataset.computer).toBe("free");
    const box = sheet()!;
    expect(box.textContent).toContain("Give control");
    // A window's title is shown as plain text, whatever it contains.
    expect(box.textContent).toContain("<b>todo</b>");
    expect(box.querySelector("b b")).toBeNull();
    // An app the system will not let input reach cannot be chosen.
    const elevated = [...box.querySelectorAll<HTMLInputElement>("input[type=radio]")].find((r) => r.value === "13")!;
    expect(elevated.disabled).toBe(true);
    const grant = button(box, "Give control for 30 min")!;
    expect(grant.disabled).toBe(true);
    await act(async () => button(box, "10 min")!.click());
    const radio = [...box.querySelectorAll<HTMLInputElement>("input[type=radio]")].find((r) => r.value === "10")!;
    await act(async () => radio.click());
    await act(async () => button(box, "Give control for 10 min")!.click());
    expect(sent).toEqual([{ url: "/api/computer/grant", method: "POST", body: { session_id: SESSION, scope: { kind: "window", window: "10" }, minutes: 10 } }]);
  });

  it("refuses a number of minutes out of bounds before anything is sent", async () => {
    await show(state(), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    const box = sheet()!;
    await act(async () => button(box, "Whole desktop")!.click());
    const field = box.querySelector<HTMLInputElement>(".computer-minutes")!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setter.call(field, "500");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(box.textContent).toContain("Between 1 and 240 minutes.");
    expect([...box.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Give control for"))!.disabled).toBe(true);
    expect(sent).toEqual([]);
  });

  it("shows the banner while this conversation controls the computer, and stops it", async () => {
    await show(held(SESSION), <><Head /><ComputerBanner sessionId={SESSION} /></>);
    expect(host.querySelector<HTMLElement>(".computer-button")?.classList.contains("on")).toBe(true);
    const banner = host.querySelector<HTMLElement>(".computer-banner")!;
    expect(banner.dataset.computer).toBe("mine");
    expect(banner.textContent).toContain("The agent is controlling your computer");
    expect(banner.textContent).toContain("12 min left");
    expect(banner.textContent).toContain("moving the mouse or pressing a key pauses it");
    // No Resume while nothing is paused: there is no pause for the operator to lift.
    expect(button(banner, "Resume")).toBeUndefined();
    current = state();
    await act(async () => button(banner, "Stop")!.click());
    expect(sent).toEqual([{ url: "/api/computer/stop", method: "POST", body: {} }]);
  });

  it("offers Resume while the operator's hand has paused the agent", async () => {
    await show(held(SESSION, { human: { paused: true } }), <ComputerBanner sessionId={SESSION} />);
    const banner = host.querySelector<HTMLElement>(".computer-banner")!;
    expect(banner.dataset.computer).toBe("paused");
    expect(banner.textContent).toContain("The agent is paused: you are using the computer");
    await act(async () => button(banner, "Resume")!.click());
    expect(sent).toEqual([{ url: "/api/computer/resume", method: "POST", body: {} }]);
  });

  it("says when another conversation holds the computer, and can take it back", async () => {
    await show(held("s2"), <><ComputerBanner sessionId={SESSION} /><ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} /></>);
    expect(host.querySelector(".computer-banner")).toBeNull();
    const box = sheet()!;
    expect(box.querySelector<HTMLElement>("[data-computer=other]")?.textContent).toContain("Another conversation controls the computer");
    await act(async () => button(box, "Take it back")!.click());
    expect(sent).toEqual([{ url: "/api/computer/revoke", method: "POST", body: { session_id: "s2" } }]);
  });

  it("on macOS lists both permissions with Ask, the settings link and the warning about updates", async () => {
    const links = { screen: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture", accessibility: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" };
    await show(state({ os: "darwin", session_type: "quartz", permissions: { screen: "denied", accessibility: "granted" }, settings_links: links }), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    const box = sheet()!;
    const screen = box.querySelector<HTMLElement>("[data-permission=screen]")!;
    expect(screen.dataset.state).toBe("denied");
    expect(screen.textContent).toContain("Not granted");
    expect(screen.querySelector("a")?.getAttribute("href")).toBe(links.screen);
    const accessibility = box.querySelector<HTMLElement>("[data-permission=accessibility]")!;
    expect(accessibility.textContent).toContain("Granted");
    expect(button(accessibility, "Ask")).toBeUndefined();
    expect(box.textContent).toContain("macOS forgets these permissions after every update");
    expect(box.textContent).toContain("remove Daedalus from the list and add it again");
    await act(async () => button(screen, "Ask")!.click());
    expect(sent).toEqual([{ url: "/api/computer/permissions", method: "POST", body: { which: "screen" } }]);
  });

  it("names Windows' limits, and Linux's session type with the word on Wayland", async () => {
    await show(state({ os: "windows", session_type: "win32" }), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    expect(sheet()!.querySelector("[data-os=windows]")?.textContent).toContain("Programs running as administrator, UAC prompts and the lock screen cannot be driven");
    await show(state(), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    const linux = sheet()!.querySelector("[data-os=linux]")!;
    expect(linux.textContent).toContain("Session type: x11");
    expect(linux.textContent).toContain("Wayland is not supported yet");
  });

  it("shows a refusal in its words, never its code", async () => {
    refuse["/api/computer/grant"] = { detail: "That is one of Daedalus's own windows.", error: { code: 1203, message: "protected", data: { app: "com.example.daedalus" }, explanation: "That is one of Daedalus's own windows (or another protected app)." } };
    await show(state(), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    const box = sheet()!;
    await act(async () => button(box, "Whole desktop")!.click());
    await act(async () => button(box, "Give control for 30 min")!.click());
    const error = box.querySelector<HTMLElement>(".computer-error")!;
    expect(error.textContent).toBe("That is one of Daedalus's own windows (or another protected app).");
    expect(box.textContent).not.toContain("1203");
  });

  it("says why the computer cannot be driven, in Russian too", async () => {
    setLang("ru");
    await show(state({ available: false, reason: "The desktop launcher is not running." }), <><Head /><ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} /></>);
    expect(host.querySelector<HTMLElement>(".computer-button")?.dataset.computer).toBe("unavailable");
    expect(sheet()!.textContent).toContain("Управление этим компьютером");
    expect(sheet()!.textContent).toContain("The desktop launcher is not running.");
  });

  it("says why by its key in Russian, with the launcher's own reason as the detail", async () => {
    setLang("ru");
    await show(state({ available: false, reason: "this build carries no deskd; computer use is unavailable", reason_words: { key: "cu.reason.no_daemon" } }), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    const reason = sheet()!.querySelector<HTMLElement>(".computer-reason")!;
    expect(reason.firstChild?.textContent).toBe("Часть Daedalus, которая управляет рабочим столом, на этом компьютере не запущена.");
    expect(reason.querySelector(".computer-reason-detail")?.textContent).toBe("this build carries no deskd; computer use is unavailable");
    await show(state({ available: false, reason: "The desktop launcher is not running, so nothing can reach this computer's desktop.", reason_words: { key: "cu.reason.no_launcher" } }), <ComputerSheet sessionId={SESSION} toast={() => undefined} onClose={() => undefined} />);
    expect(sheet()!.querySelector(".computer-reason-detail")).toBeNull();
    expect(sheet()!.textContent).not.toContain("nothing can reach");
  });
});
