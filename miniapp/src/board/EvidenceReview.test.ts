// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { evidenceCoverage, evidenceCurrent, firstUncovered, usableChecks } from "./EvidenceReview";

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

describe("a worker's check as evidence", () => {
  const head = "a".repeat(40);
  const receipt = (id: number, fields: Record<string, unknown> = {}) => ({ id, criterion: "tests pass", command: "pytest -q", exit_code: 0, passed: true, at: "", tree: head, ...fields });
  const check = (criterion_id: string, tree: string) => ({ evidence_id: criterion_id, criterion_id, observation: "verify: tests pass", verification: "verified" as const, manifest_digest_before: `tree:${tree}`, manifest_digest_after: `tree:${tree}`, observed_at: "" });

  it("offers only passing checks that ran on the reviewed commit with a clean tree", () => {
    const receipts = [receipt(1), receipt(2, { passed: false, exit_code: 1 }), receipt(3, { tree: `${head}+worktree` }), receipt(4, { tree: "b".repeat(40) }), receipt(5, { id: undefined })];
    expect(usableChecks(receipts, head).map((r) => r.id)).toEqual([1]);
    expect(usableChecks(receipts, null)).toEqual([]);
  });

  it("stops counting a check once the branch moves to another commit", () => {
    expect(evidenceCurrent(check("C1", head), head)).toBe(true);
    expect(evidenceCurrent(check("C1", head), "b".repeat(40))).toBe(false);
    expect(evidenceCurrent(check("C1", head), null)).toBe(false);
    expect(evidenceCurrent({ manifest_digest_before: "file-digest" }, null)).toBe(true);
    expect(evidenceCoverage([{ id: "C1" }], [check("C1", head)], head)).toBe(true);
    expect(evidenceCoverage([{ id: "C1" }], [check("C1", head)], "b".repeat(40))).toBe(false);
  });

  it("proposes the first criterion still without current evidence", () => {
    const checks = [{ id: "C1" }, { id: "C2" }];
    expect(firstUncovered(checks, [check("C1", head)], head)).toBe("C2");
    expect(firstUncovered(checks, [check("C1", "b".repeat(40))], head)).toBe("C1");
    expect(firstUncovered([], [], head)).toBe("result");
  });
});
