import { describe, expect, it } from "vitest";
import type { BrowserStep } from "../api";
import { blocker, editableSteps, initialRow, isSecretRef, paramsOf, skillRequest, skillSlug } from "./skilldraft";

const step = (n: number, action: BrowserStep["action"], extra: Partial<BrowserStep> = {}): BrowserStep => ({ n, at: 0, tab: "t1", url: "https://shop.test/", action, ...extra });

const steps: BrowserStep[] = [
  step(1, "type", { element: { role: "textbox", name: "Period" }, slot: "period", value: "2026-10", submit: true }),
  step(2, "tab", { to: "https://shop.test/other" }),
  step(3, "handoff", { reason: "login", fields: [{ role: "textbox", name: "Email", secret: "name" }, { role: "textbox", name: "Password", secret: "password" }] }),
  step(4, "click", { element: { role: "button", name: "Export" } }),
  step(5, "type", { element: { role: "textbox", name: "Note" }, slot: "note" }),
];

describe("the step editor's rules", () => {
  it("leaves out tab switches and starts each step from what was recorded", () => {
    const shown = editableSteps(steps);
    expect(shown.map((s) => s.n)).toEqual([1, 3, 4, 5]);
    const rows = shown.map(initialRow);
    expect(rows[0].text).toBe("2026-10");
    expect(rows[3].text).toBe("{note}");
    expect(rows[1].secrets).toEqual(["", ""]);
    expect(paramsOf(rows, steps)).toEqual(["note"]);
  });

  it("names parameters from what the operator wrote and drops deleted steps from the request", () => {
    const rows = editableSteps(steps).map(initialRow);
    rows[0].text = "{month}-{year}";
    rows[3].keep = false;
    rows[1].signin = "secrets";
    rows[1].secrets = ["«secret:shop_user»", " «secret:shop_password» "];
    expect(paramsOf(rows, steps)).toEqual(["month", "year"]);
    const body = skillRequest(rows, steps, "Monthly Report!", "  Download   the monthly report  ");
    expect(body.name).toBe("monthly-report");
    expect(body.description).toBe("Download the monthly report");
    expect(body.steps).toEqual([
      { n: 1, text: "{month}-{year}" },
      { n: 3, signin: "secrets", secrets: ["«secret:shop_user»", "«secret:shop_password»"] },
      { n: 4 },
    ]);
  });

  it("holds Save back until the skill is named, described, does something and binds secrets as placeholders", () => {
    const rows = editableSteps(steps).map(initialRow);
    expect(blocker(rows, steps, "", "Download the monthly report")).toBe("browser.skill.need.name");
    expect(blocker(rows, steps, "report", "short")).toBe("browser.skill.need.description");
    rows[1].signin = "secrets";
    rows[1].secrets = ["hunter2", "«secret:pw»"];
    expect(blocker(rows, steps, "report", "Download the monthly report")).toBe("browser.skill.need.secret");
    rows[1].secrets = ["«secret:user»", "«secret:pw»"];
    expect(blocker(rows, steps, "report", "Download the monthly report")).toBe("");
    for (const r of rows) r.keep = false;
    expect(blocker(rows, steps, "report", "Download the monthly report")).toBe("browser.skill.need.step");
  });

  it("knows a secret's placeholder and a skill's name", () => {
    expect(isSecretRef("«secret:router_admin»")).toBe(true);
    expect(isSecretRef("«secret:Router»")).toBe(false);
    expect(isSecretRef("password")).toBe(false);
    expect(skillSlug("  Ship it now! ")).toBe("ship-it-now");
  });
});
