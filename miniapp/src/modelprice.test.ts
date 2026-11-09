import { describe, expect, it } from "vitest";
import type { PriceView } from "./api";
import { priceBody, priceChanged, priceDraft } from "./models";

const view: PriceView = {
  input: 0.25, output: 1, cache_hit: null, manual: true, plan: false, plan_default: false, plan_source: "", plan_locked: false, states_bill: false,
  default: { input: 0.3, output: 1.2, cache_hit: 0.006, source: "builtin" },
};

describe("a model's price", () => {
  it("shows what is saved and counts only a real change as one", () => {
    const draft = priceDraft(view);
    expect(draft).toEqual({ input: "0.25", output: "1", cache_hit: "" });
    expect(priceChanged(draft, view)).toBe(false);
    expect(priceChanged({ ...draft, output: "1.0" }, view)).toBe(false);
    expect(priceChanged({ ...draft, cache_hit: "0.01" }, view)).toBe(true);
    expect(priceDraft(undefined)).toEqual({ input: "", output: "", cache_hit: "" });
  });

  it("refuses a negative or missing rate per token, and leaves an empty cache rate out", () => {
    expect(priceBody({ input: "1", output: "2", cache_hit: "" }, false)).toEqual({ body: { input: 1, output: 2, billing: "metered" }, error: null });
    expect(priceBody({ input: "-1", output: "2", cache_hit: "" }, false).error).toBe("rates");
    expect(priceBody({ input: "", output: "2", cache_hit: "" }, false).error).toBe("rates");
    expect(priceBody({ input: "1", output: "2", cache_hit: "x" }, false).error).toBe("rates");
    expect(priceBody({ input: "1", output: "2", cache_hit: "0.1" }, false).body?.cache_hit).toBe(0.1);
  });

  it("saves a plan whatever the rates, keeping the ones typed", () => {
    expect(priceBody({ input: "", output: "", cache_hit: "" }, true)).toEqual({ body: { input: 0, output: 0, billing: "subscription" }, error: null });
    expect(priceBody({ input: "3", output: "15", cache_hit: "0.3" }, true).body).toEqual({ input: 3, output: 15, cache_hit: 0.3, billing: "subscription" });
  });
});
