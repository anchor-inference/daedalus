// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { evidenceCoverage } from "./EvidenceReview";

describe("review evidence coverage", () => {
  const verified = (criterion_id: string) => ({ evidence_id: criterion_id, criterion_id, observation: "inspected", verification: "verified" as const, manifest_digest_before: "digest", manifest_digest_after: "digest", observed_at: "" });

  it("requires every check and a stable artifact digest", () => {
    const checks = [{ id: "C1" }, { id: "C2" }];
    expect(evidenceCoverage(checks, [verified("C1")])).toBe(false);
    expect(evidenceCoverage(checks, [verified("C1"), { ...verified("C2"), manifest_digest_after: "changed" }])).toBe(false);
    expect(evidenceCoverage(checks, [verified("C1"), verified("C2")])).toBe(true);
  });

  it("still needs one exact piece of evidence when the contract has no checks", () => {
    expect(evidenceCoverage([], [])).toBe(false);
    expect(evidenceCoverage([], [verified("result")])).toBe(true);
  });
});
