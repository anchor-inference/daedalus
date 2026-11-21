import { describe, expect, it } from "vitest";
import type { Preset } from "./api";
import { effortBody, effortOf, presetEffort, startUpstream } from "./starteffort";

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

describe("the subscription a new chat's account is picked from", () => {
  const upstreams = [{ upstream: "claude", providers: ["claude", "claude-2"], default_account: "default" }];
  it("is the one the preset's client goes through, with the preset's own account as the default", () => {
    expect(startUpstream(preset({ provider: "claude-2" }), upstreams)).toEqual({ upstream: "claude", fallback: "default" });
    expect(startUpstream(preset({ provider: "claude", account: "second" }), upstreams)).toEqual({ upstream: "claude", fallback: "second" });
  });
  it("is none for a client with no accounts or no preset at all", () => {
    expect(startUpstream(preset({ provider: "openrouter" }), upstreams)).toBeNull();
    expect(startUpstream(undefined, upstreams)).toBeNull();
  });
});
