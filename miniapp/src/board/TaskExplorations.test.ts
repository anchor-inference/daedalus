// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { attemptCost } from "./TaskExplorations";

describe("what a competing attempt cost", () => {
  const attempt = { slot: 1, state: "reported", staff_name: "Ada-1", harness: "daedalus", model: "", task_id: "t1", forked: true, usd: 0.4234, unpriced: false };

  it("shows dollars to the cent", () => {
    setLang("en");
    expect(attemptCost(attempt)).toBe("$0.42");
  });

  it("says when part of it was never priced, in either language", () => {
    setLang("en");
    expect(attemptCost({ ...attempt, unpriced: true })).toBe("$0.42 and unpriced use");
    setLang("ru");
    expect(attemptCost({ ...attempt, unpriced: true })).toContain("неоценённый");
    setLang("en");
  });
});
