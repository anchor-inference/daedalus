import { describe, expect, it } from "vitest";
import type { Preset } from "./api";
import { effortBody, effortOf, presetEffort } from "./starteffort";

const preset = (over: Partial<Preset>): Preset => ({ provider: "p", model: "m", label: "", thinking: false, reasoning_effort: "", images: false, context_window: 0, max_output_tokens: 0, ...over });

describe("the effort a new chat starts with", () => {
  it("shows the preset's own until something is picked", () => {
    expect(presetEffort(preset({ thinking: true, reasoning_effort: "high" }))).toEqual({ thinking: true, effort: "high" });
    expect(presetEffort(preset({ thinking: true }))).toEqual({ thinking: true, effort: "medium" });
    expect(presetEffort(preset({ thinking: false, reasoning_effort: "high" }))).toEqual({ thinking: false });
    expect(presetEffort(undefined)).toEqual({ thinking: false });
  });

  it("sends what was picked the way the session's model route takes it", () => {
    expect(effortBody(effortOf("low"))).toEqual({ thinking: true, reasoning_effort: "low" });
    expect(effortBody(effortOf("off"))).toEqual({ thinking: false });
  });
});
