// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { acceptanceBlock, mergeGuard, type ResultReceipt } from "./ResultFlow";
import type { ProjectTask, Review } from "./board";

const task = { id: "task", acceptance_state: "accepted", branch: null, merge_state: "" } as ProjectTask;
const contract = { task_id: "task", contract_revision: 2, entity_revision: 7 };
const result = {
  result_id: "result", current_result_id: "result", task_id: "task", contract_revision: 2,
  outcome: "complete", verification: "verified", verdict_id: "verdict", verdict_accepted: true,
  verdict_head: null, verdict_base: null,
} as ResultReceipt;

describe("result acceptance", () => {
  it("requires the current exact result and an accepted verdict", () => {
    expect(acceptanceBlock(task, result, contract, null, false)).toBeNull();
    expect(acceptanceBlock(task, { ...result, current_result_id: "newer" }, contract, null, false)).toBe("changedResult");
    expect(acceptanceBlock(task, { ...result, verification: "stale" }, contract, null, false)).toBe("unverified");
    expect(acceptanceBlock(task, result, { ...contract, contract_revision: 3 }, null, false)).toBe("changedContract");
    expect(acceptanceBlock({ ...task, acceptance_state: "handed_in" }, result, contract, null, false)).toBe("coordinator");
    expect(acceptanceBlock(task, result, contract, null, true)).toBe("unconfirmed");
  });

  it("requires a merged branch with the same reviewed commits", () => {
    const branch = { ...task, branch: "change", merge_state: "merged" } as ProjectTask;
    const reviewed = { ...result, verdict_head: "head", verdict_base: "base" };
    const review = { merged: true, current_sha: "merged-head", merge_receipt: { id: "merge", state: "merged", error: null, result_id: "result", verdict_id: "verdict", head_sha: "head", base_sha: "base", merge_sha: "merged-head" } } as Review;
    expect(acceptanceBlock(branch, reviewed, contract, review, false)).toBeNull();
    expect(acceptanceBlock(branch, reviewed, contract, { ...review, merge_receipt: { ...review.merge_receipt!, head_sha: "changed" } }, false)).toBe("changedBranch");
    expect(acceptanceBlock(branch, reviewed, contract, { ...review, current_sha: "moved" }, false)).toBe("changedBranch");
    expect(acceptanceBlock({ ...branch, merge_state: "proposed" }, reviewed, contract, review, false)).toBeNull();
    expect(acceptanceBlock(branch, reviewed, contract, { ...review, merge_receipt: null }, false)).toBe("branch");
  });

  it("only offers merge for the exact verified branch version", () => {
    const branch = { ...task, branch: "change", merge_state: "proposed" } as ProjectTask;
    const reviewed = { ...result, verdict_head: "head", verdict_base: "base" };
    const review = { head_sha: "head", base_sha: "base", can_merge: true, blockers: [] } as unknown as Review;
    expect(mergeGuard(branch, reviewed, contract, review, false)).toBeNull();
    expect(mergeGuard(branch, reviewed, contract, { ...review, head_sha: "changed" }, false)).toBe("changedBranch");
    expect(mergeGuard(branch, reviewed, contract, { ...review, blockers: [{ code: "dirty", text: "dirty" }] }, false)).toBe("branch");
    expect(mergeGuard(branch, reviewed, contract, review, true)).toBe("unconfirmed");
  });
});
