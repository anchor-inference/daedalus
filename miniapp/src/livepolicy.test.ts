import { describe, expect, it } from "vitest";
import { setLang } from "./i18n";
import { amount, billingsInUse, buildPolicy, fieldSection, formOf, freshForm, generatePassword, lockAll, summarize, toggleAllowed, toggleIn, type LiveOptions, type RolePreset } from "./livepolicy";
import { allowedEfforts, roleLocked, type VisitLimits } from "./visit";

const preset = (id: string, provider: string, billing: "subscription" | "metered", efforts: string[] = [], accounts: string[] = []): RolePreset => ({
  id, label: id, provider, provider_name: provider.toUpperCase(), model: id, billing, efforts, accounts: accounts.map((a) => ({ id: a, name: a })),
});
const PRESETS = [preset("opus", "claude", "subscription", ["low", "high"], ["work", "home"]), preset("sonnet", "claude", "subscription"), preset("flash", "gemini", "metered")];
const OPTIONS: LiveOptions = {
  roles: [
    { id: "main", label: "Conversation", pickable: true, current: { preset: "opus", effort: "high", account: "" }, presets: PRESETS },
    { id: "vision", label: "Vision", pickable: true, current: { preset: "flash", effort: "", account: "" }, presets: PRESETS },
    { id: "title", label: "Titles", pickable: false, current: { preset: "sonnet", effort: "", account: "" }, presets: PRESETS },
  ],
  place: "container",
  terminals: ["all", "container", "none"],
  withheld: [{ id: "memory", label: "Memory", why: "" }],
};

describe("a live link's policy", () => {
  it("starts with every role locked to its current value and no terminals", () => {
    expect(buildPolicy(freshForm(OPTIONS), OPTIONS)).toEqual({
      roles: {
        main: { preset: "opus", effort: "high", account: "", allowed: [] },
        vision: { preset: "flash", effort: "", account: "", allowed: [] },
        title: { preset: "sonnet", effort: "", account: "", allowed: [] },
      },
      budget_usd: null, budget_weekly_pct: null, terminals: "none",
    });
  });

  it("carries a set only for a role the visitor can pick, and only while the set is chosen", () => {
    const form = freshForm(OPTIONS);
    form.roles.main = { ...form.roles.main, mode: "set", allowed: [{ preset: "flash", accounts: [], efforts: [] }] };
    form.roles.title = { ...form.roles.title, mode: "set", allowed: [{ preset: "flash", accounts: [], efforts: [] }] };
    form.roles.vision = { ...form.roles.vision, mode: "locked", allowed: [{ preset: "opus", accounts: [], efforts: [] }] };
    const policy = buildPolicy(form, OPTIONS);
    expect(policy.roles?.main.allowed).toEqual([{ preset: "flash", accounts: [], efforts: [] }]);
    expect(policy.roles?.title.allowed).toEqual([]);
    expect(policy.roles?.vision.allowed).toEqual([]);
  });

  it("reads typed limits with a comma and refuses what is not a positive number", () => {
    expect(amount("")).toBeNull();
    expect(amount("7,5")).toBe(7.5);
    expect(amount("-1")).toBeNaN();
    expect(amount("abc")).toBeNaN();
    const form = { ...freshForm(OPTIONS), budgetUsd: "2,5", budgetPct: "oops" };
    expect(buildPolicy(form, OPTIONS)).toMatchObject({ budget_usd: 2.5, budget_weekly_pct: null });
  });

  it("opens an existing link's policy as the form it was saved from", () => {
    const form = formOf({ roles: { main: { preset: "sonnet", effort: "", account: "", allowed: [{ preset: "opus", accounts: ["work"], efforts: ["low"] }] } }, budget_usd: 3, terminals: "container" }, OPTIONS);
    expect(form.roles.main.mode).toBe("set");
    expect(form.roles.vision.mode).toBe("locked");
    expect(form.budgetUsd).toBe("3");
    expect(form.terminals).toBe("container");
    expect(buildPolicy(lockAll(form, OPTIONS), OPTIONS).roles?.main).toEqual({ preset: "opus", effort: "high", account: "", allowed: [] });
  });

  it("knows which way of paying the reachable models use", () => {
    const form = freshForm(OPTIONS);
    expect([...billingsInUse(form, OPTIONS)].sort()).toEqual(["metered", "subscription"]);
    form.roles.vision = { ...form.roles.vision, preset: "sonnet" };
    expect([...billingsInUse(form, OPTIONS)]).toEqual(["subscription"]);
  });

  it("toggles presets, efforts and accounts without duplicates", () => {
    const once = toggleAllowed([], "opus", true);
    expect(toggleAllowed(once, "opus", true)).toBe(once);
    expect(toggleAllowed(once, "opus", false)).toEqual([]);
    expect(toggleIn(["low"], "low", true)).toEqual(["low"]);
    expect(toggleIn(["low", "high"], "low", false)).toEqual(["high"]);
  });

  it("says in a sentence what the holder can do", () => {
    setLang("en");
    const form = { ...freshForm(OPTIONS), budgetUsd: "5", budgetPct: "3" };
    form.roles.main = { ...form.roles.main, mode: "set", allowed: [{ preset: "flash", accounts: [], efforts: [] }] };
    const said = summarize(form, OPTIONS, true);
    expect(said).toContain("Opens with a password.");
    expect(said).toContain("Chats with opus or one of 1 more models.");
    expect(said).toContain("$5.00");
    expect(said).toContain("3% of the week");
    expect(said).toContain("No terminals.");
    expect(said).toContain("loses 1 reach");
    expect(summarize(freshForm(OPTIONS), OPTIONS, false)).toContain("no password");
  });

  it("makes a password of four groups and names a refused field's section", () => {
    expect(generatePassword()).toMatch(/^[a-z2-9]{4}(-[a-z2-9]{4}){3}$/);
    expect(fieldSection("password")).toBe("access");
    expect(fieldSection("policy.roles.main")).toBe("models");
    expect(fieldSection("policy.budget_usd")).toBe("limits");
    expect(fieldSection("policy.terminals")).toBe("terminals");
  });
});

describe("a visitor's limits", () => {
  const limits: VisitLimits = {
    roles: {
      main: { preset: "opus", effort: "high", account: "", allowed: [{ preset: "flash", accounts: [], efforts: [] }, { preset: "sonnet", accounts: [], efforts: ["low"] }] },
      vision: { preset: "flash", effort: "", account: "", allowed: [] },
    },
    budget: { usd: null, weekly_pct: null },
    terminals: "none",
  };
  it("narrows efforts per preset and leaves an open one to any", () => {
    expect(allowedEfforts("main", "opus", limits)).toEqual(["high"]);
    expect(allowedEfforts("main", "flash", limits)).toBeNull();
    expect(allowedEfforts("main", "sonnet", limits)).toEqual(["low"]);
    expect(allowedEfforts("translation", "opus", limits)).toBeNull();
  });
  it("calls a role with no set locked", () => {
    expect(roleLocked("vision", limits)).toBe(true);
    expect(roleLocked("main", limits)).toBe(false);
    expect(roleLocked("translation", limits)).toBe(false);
  });
});
