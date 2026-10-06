// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { freshnessText } from "./ReviewPanel";

const fork = "4f2a9c1d0b7e6a5f4c3b2a1d0e9f8a7b6c5d4e3f";

describe("freshnessText", () => {
  afterEach(() => setLang("en"));

  it("says nothing while neither the base nor its upstream has moved", () => {
    expect(freshnessText(null)).toBe("");
    expect(freshnessText({ base_sha: fork, base: "main", behind: 0, upstream: "origin/main", upstream_behind: 0 })).toBe("");
    // A tracking ref never fetched cannot be counted and is not guessed at.
    expect(freshnessText({ base_sha: fork, base: "main", behind: 0, upstream: "origin/main", upstream_behind: null })).toBe("");
  });

  it("names the fork point and how far each ref moved past it", () => {
    setLang("en");
    expect(freshnessText({ base_sha: fork, base: "main", behind: 3, upstream: "", upstream_behind: null }))
      .toBe("Based on 4f2a9c1 · 3 commits behind main");
    expect(freshnessText({ base_sha: fork, base: "main", behind: 1, upstream: "origin/main", upstream_behind: 4 }))
      .toBe("Based on 4f2a9c1 · 1 commit behind main · 4 commits behind origin/main as last fetched");
    setLang("ru");
    expect(freshnessText({ base_sha: fork, base: "main", behind: 2, upstream: "", upstream_behind: null }))
      .toBe("Основано на 4f2a9c1 · main впереди на 2 коммита");
  });
});
