import { afterEach, describe, expect, it } from "vitest";
import { DICT, setLang } from "./i18n";
import { describeIntegration, INTEGRATION_WORDS, KIND_WORDS, type IntegrationRow } from "./integrationHealth";

const row = (over: Partial<IntegrationRow>): IntegrationRow => ({ kind: "mcp", name: "notes", state: "connected", severity: "ok", detail: "", ...over });

describe("the integration rows", () => {
  afterEach(() => setLang("en"));

  it("have words in both languages for every state the host sends", () => {
    const keys = [...Object.values(INTEGRATION_WORDS).flat(), ...Object.values(KIND_WORDS), "health.int.unknown", "health.int.title", "health.int.details"];
    for (const key of keys) {
      if (key === null) continue;
      expect(DICT[key]?.en, key).toBeTruthy();
      expect(DICT[key]?.ru, key).toBeTruthy();
    }
  });

  it("give every failing state one line of what to do, and a healthy one none", () => {
    const failing = describeIntegration(row({ state: "auth_required", severity: "fail" }));
    expect(failing.state).toBe("Sign-in required");
    expect(failing.fix).toContain("sign in");
    expect(describeIntegration(row({})).fix).toBe("");
    expect(describeIntegration(row({ kind: "provider", state: "no_key", severity: "fail" })).fix).toContain("Settings → Models");
    setLang("ru");
    expect(describeIntegration(row({ kind: "github", name: "GitHub", state: "token_rejected", severity: "fail" })).state).toBe("Токен отклонён");
  });

  it("name a state it does not know instead of showing a key", () => {
    const unknown = describeIntegration(row({ state: "sleeping" }));
    expect(unknown.state).toContain("sleeping");
    expect(unknown.state).not.toContain("[");
  });
});
