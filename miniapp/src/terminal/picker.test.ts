// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import type { TerminalEnv } from "../api";
import { setLang } from "../i18n";
import { effectiveSandbox, loadLastPlace, placeBlocked, saveLastPlace } from "./picker";

const env = (name: "container" | "host", fields: Partial<TerminalEnv> = {}): TerminalEnv => ({ env: name, available: true, reason: "", version: "1", sandbox: "ok", shell: "/bin/bash", home: name === "host" ? "/home/someone" : "/root", port_range: "", ...fields });

describe("where a new terminal may go", () => {
  beforeEach(() => setLang("en"));

  it("offers every place when both environments answer", () => {
    const envs = [env("container"), env("host")];
    for (const choice of ["container", "host", "folder"] as const) expect(placeBlocked(choice, envs, false)).toBe("");
  });

  it("closes an environment that does not answer, with its reason", () => {
    const envs = [env("container"), env("host", { available: false, reason: "not_configured" })];
    expect(placeBlocked("host", envs, false)).toBe("Host terminal is not installed");
    expect(placeBlocked("container", envs, false)).toBe("");
    expect(placeBlocked("folder", envs, false)).toBe("");
  });

  it("with the sandbox on, closes an environment that cannot give one and says why", () => {
    const envs = [env("container"), env("host", { sandbox: "bubblewrap is not installed" })];
    expect(placeBlocked("host", envs, true)).toContain("bubblewrap is not installed");
    expect(placeBlocked("host", envs, false)).toBe("");
    expect(placeBlocked("container", envs, true)).toBe("");
  });

  it("closes the folder too when nothing answers", () => {
    const envs = [env("container", { available: false, reason: "not_installed" }), env("host", { available: false })];
    expect(placeBlocked("folder", envs, false)).toBe("The terminal service is not installed");
  });

  it("applies the sandbox only where some environment can give it", () => {
    expect(effectiveSandbox([env("container", { sandbox: "no" })], true)).toBe(false);
    expect(effectiveSandbox([env("container")], true)).toBe(true);
    expect(effectiveSandbox([env("container")], false)).toBe(false);
  });

  it("remembers the place chosen last on this device, to mark it", () => {
    localStorage.clear();
    expect(loadLastPlace()).toBeNull();
    saveLastPlace("host");
    expect(loadLastPlace()).toBe("host");
    localStorage.setItem("daedalus.term.lastPlace", "moon");
    expect(loadLastPlace()).toBeNull();
  });
});
